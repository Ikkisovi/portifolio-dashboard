using System;
using System.Collections.Generic;
using System.ComponentModel.Composition;
using System.Globalization;
using System.IO;
using System.Collections;
using System.Linq;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using MessagePack;
using MessagePack.Resolvers;
using QuantConnect;
using QuantConnect.Configuration;
using QuantConnect.Data;
using QuantConnect.Data.Market;
using QuantConnect.Interfaces;
using QuantConnect.Logging;
using QuantConnect.Packets;
using QuantConnect.Securities;
using QuantConnect.Util;

namespace QuantConnect.AlpacaProxy
{
    [Export(typeof(IDataQueueHandler))]
    [PartCreationPolicy(CreationPolicy.Shared)]
    public class AlpacaProxyDataQueueHandler : IDataQueueHandler
    {
        private static readonly MessagePackSerializerOptions MsgPackOptions = MessagePackSerializerOptions.Standard
            .WithResolver(ContractlessStandardResolver.Instance)
            .WithSecurity(MessagePackSecurity.UntrustedData);

        private readonly object _subscriptionLock = new();
        private readonly Dictionary<SubscriptionDataConfig, SubscriptionRequirement> _subscriptions =
            new(new SubscriptionDataConfigComparer());

        private IReadOnlyDictionary<string, Symbol> _brokerageSymbolLookup = new Dictionary<string, Symbol>(StringComparer.Ordinal);
        private HashSet<string> _tradeSymbols = new(StringComparer.Ordinal);
        private HashSet<string> _quoteSymbols = new(StringComparer.Ordinal);
        private HashSet<string> _optionTradeSymbols = new(StringComparer.Ordinal);
        private HashSet<string> _optionQuoteSymbols = new(StringComparer.Ordinal);

        private readonly IDataAggregator _aggregator;
        private readonly MarketHoursDatabase _marketHoursDatabase;
        private readonly Dictionary<Symbol, TimeZoneOffsetProvider> _timeZoneProviders = new();

        private readonly string _proxyUrl;
        private readonly string _optionsProxyUrl;
        private readonly string _proxyToken;
        private readonly string _alpacaKey;
        private readonly string _alpacaSecret;

        private ClientWebSocket _ws;
        private ClientWebSocket _optionsWs;
        private readonly CancellationTokenSource _cts;
        private readonly Task _connectionTask;
        private readonly Task _optionsConnectionTask;
        private readonly Task _watchdogTask;
        private readonly SemaphoreSlim _sendLock = new(1, 1);
        private readonly SemaphoreSlim _optionsSendLock = new(1, 1);
        private readonly HashSet<string> _stockLookupMissesLogged = new(StringComparer.Ordinal);

        private volatile bool _isConnected;
        private volatile bool _isAuthenticated;
        private volatile bool _pendingSubscriptionUpdate;
        private volatile bool _isOptionsConnected;
        private volatile bool _isOptionsAuthenticated;
        private volatile bool _pendingOptionsSubscriptionUpdate;
        private volatile bool _hasOptionSubscriptions;
        private readonly bool _watchdogEnabled;
        private readonly TimeSpan _staleDataWindow;
        private readonly TimeSpan _watchdogCheckInterval;
        private long _lastStockMessageTicks;

        private static bool SupportsConfig(SubscriptionDataConfig dataConfig)
        {
            return dataConfig != null && (dataConfig.SecurityType == SecurityType.Equity || dataConfig.SecurityType == SecurityType.Option);
        }

        public AlpacaProxyDataQueueHandler()
        {
            _aggregator = Composer.Instance.GetExportedValueByTypeName<IDataAggregator>(
                Config.Get("data-aggregator", "QuantConnect.Lean.Engine.DataFeeds.AggregationManager"),
                false);

            _proxyUrl = Environment.GetEnvironmentVariable("ALPACA_PROXY_URL")
                ?? Config.Get("alpaca-proxy-url", "ws://host.docker.internal:8765");
            _optionsProxyUrl = Environment.GetEnvironmentVariable("ALPACA_OPTIONS_PROXY_URL")
                ?? Config.Get("alpaca-options-proxy-url", string.Empty);
            if (string.IsNullOrWhiteSpace(_optionsProxyUrl))
            {
                _optionsProxyUrl = BuildOptionsProxyUrl(_proxyUrl);
            }

            _proxyToken = Environment.GetEnvironmentVariable("ALPACA_PROXY_TOKEN")
                ?? Config.Get("alpaca-proxy-token", string.Empty);

            _alpacaKey = Environment.GetEnvironmentVariable("ALPACA_API_KEY")
                ?? Environment.GetEnvironmentVariable("APCA_API_KEY_ID")
                ?? Config.Get("alpaca-api-key", string.Empty);
            _alpacaSecret = Environment.GetEnvironmentVariable("ALPACA_API_SECRET")
                ?? Environment.GetEnvironmentVariable("APCA_API_SECRET_KEY")
                ?? Config.Get("alpaca-api-secret", string.Empty);

            _marketHoursDatabase = MarketHoursDatabase.FromDataFolder();

            _cts = new CancellationTokenSource();
            _watchdogEnabled = Config.GetBool("alpaca-proxy-watchdog-enabled", true);
            _staleDataWindow = TimeSpan.FromSeconds(Math.Max(15, Config.GetInt("alpaca-proxy-stale-seconds", 120)));
            _watchdogCheckInterval = TimeSpan.FromSeconds(Math.Max(5, Config.GetInt("alpaca-proxy-watchdog-check-seconds", 15)));
            _lastStockMessageTicks = DateTime.UtcNow.Ticks;
            _connectionTask = Task.Run(() => ConnectionLoop(_cts.Token));
            _optionsConnectionTask = Task.Run(() => OptionsConnectionLoop(_cts.Token));
            _watchdogTask = Task.Run(() => WatchdogLoop(_cts.Token));
        }

        public bool IsConnected =>
            (_isConnected && _ws != null && _ws.State == WebSocketState.Open) ||
            (_isOptionsConnected && _optionsWs != null && _optionsWs.State == WebSocketState.Open);

        public IEnumerator<BaseData> Subscribe(SubscriptionDataConfig dataConfig, EventHandler newDataAvailableHandler)
        {
            if (!SupportsConfig(dataConfig))
            {
                Log.Trace($"AlpacaProxy.Subscribe(): Unsupported subscription {dataConfig.Symbol} ({dataConfig.SecurityType}).");
                return null;
            }

            if (_aggregator == null)
            {
                Log.Error("AlpacaProxy.Subscribe(): Data aggregator not available.");
                return null;
            }

            var enumerator = _aggregator.Add(dataConfig, newDataAvailableHandler);
            lock (_subscriptionLock)
            {
                _subscriptions[dataConfig] = SubscriptionRequirement.FromConfig(dataConfig);
            }
            UpdateSubscriptions();
            return enumerator;
        }

        public void Unsubscribe(SubscriptionDataConfig dataConfig)
        {
            if (!SupportsConfig(dataConfig))
            {
                return;
            }

            lock (_subscriptionLock)
            {
                _subscriptions.Remove(dataConfig);
            }
            _aggregator.Remove(dataConfig);
            UpdateSubscriptions();
        }

        public void SetJob(LiveNodePacket job)
        {
        }

        public void Dispose()
        {
            _cts.Cancel();
            try { _connectionTask.Wait(2000); } catch { }
            try { _optionsConnectionTask.Wait(2000); } catch { }
            try { _watchdogTask.Wait(2000); } catch { }
            try { _ws?.Abort(); } catch { }
            try { _ws?.Dispose(); } catch { }
            try { _optionsWs?.Abort(); } catch { }
            try { _optionsWs?.Dispose(); } catch { }
            _sendLock.Dispose();
            _optionsSendLock.Dispose();
        }

        private async Task ConnectionLoop(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                _isConnected = false;
                _isAuthenticated = false;
                _pendingSubscriptionUpdate = true;

                try
                {
                    _ws = new ClientWebSocket();
                    Log.Trace($"AlpacaProxy: Connecting to {_proxyUrl}...");
                    await _ws.ConnectAsync(new Uri(_proxyUrl), token);
                    _isConnected = true;
                    Interlocked.Exchange(ref _lastStockMessageTicks, DateTime.UtcNow.Ticks);
                    Log.Trace("AlpacaProxy: Connected.");

                    await SendAuth(token, isOptions: false);
                    await ReceiveLoop(token, isOptions: false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    Log.Error($"AlpacaProxy: Connection error: {ex.Message}. Retrying in 5s...");
                }
                finally
                {
                    _isConnected = false;
                    _isAuthenticated = false;
                    if (_ws != null)
                    {
                        try { _ws.Dispose(); } catch { }
                        _ws = null;
                    }
                }

                try { await Task.Delay(5000, token); } catch (OperationCanceledException) { }
            }
        }

        private async Task OptionsConnectionLoop(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                if (!_hasOptionSubscriptions || string.IsNullOrWhiteSpace(_optionsProxyUrl))
                {
                    try { await Task.Delay(1000, token); } catch (OperationCanceledException) { }
                    continue;
                }

                _isOptionsConnected = false;
                _isOptionsAuthenticated = false;
                _pendingOptionsSubscriptionUpdate = true;

                try
                {
                    _optionsWs = new ClientWebSocket();
                    Log.Trace($"AlpacaProxy: Connecting to options {_optionsProxyUrl}...");
                    await _optionsWs.ConnectAsync(new Uri(_optionsProxyUrl), token);
                    _isOptionsConnected = true;
                    Log.Trace("AlpacaProxy: Options connected.");

                    await SendAuth(token, isOptions: true);
                    await ReceiveLoop(token, isOptions: true);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    Log.Error($"AlpacaProxy: Options connection error: {ex.Message}. Retrying in 5s...");
                }
                finally
                {
                    _isOptionsConnected = false;
                    _isOptionsAuthenticated = false;
                    if (_optionsWs != null)
                    {
                        try { _optionsWs.Dispose(); } catch { }
                        _optionsWs = null;
                    }
                }

                try { await Task.Delay(5000, token); } catch (OperationCanceledException) { }
            }
        }

        private async Task SendAuth(CancellationToken token, bool isOptions)
        {
            if (!string.IsNullOrWhiteSpace(_proxyToken))
            {
                var authMsg = new Dictionary<string, object>
                {
                    { "action", "auth" },
                    { "token", _proxyToken }
                };
                await SendMessage(authMsg, token, isOptions);
                return;
            }

            if (string.IsNullOrWhiteSpace(_alpacaKey) || string.IsNullOrWhiteSpace(_alpacaSecret))
            {
                Log.Error("AlpacaProxy: Missing proxy token or Alpaca credentials (alpaca-proxy-token or alpaca-api-key/alpaca-api-secret).");
                return;
            }

            var alpacaAuthMsg = new Dictionary<string, object>
            {
                { "action", "auth" },
                { "key", _alpacaKey },
                { "secret", _alpacaSecret }
            };

            await SendMessage(alpacaAuthMsg, token, isOptions);
        }

        private void UpdateSubscriptions()
        {
            HashSet<string> trades;
            HashSet<string> quotes;
            HashSet<string> optionTrades;
            HashSet<string> optionQuotes;
            Dictionary<string, Symbol> symbolLookup;
            bool changed;
            bool optionsChanged;

            lock (_subscriptionLock)
            {
                trades = new HashSet<string>(StringComparer.Ordinal);
                quotes = new HashSet<string>(StringComparer.Ordinal);
                optionTrades = new HashSet<string>(StringComparer.Ordinal);
                optionQuotes = new HashSet<string>(StringComparer.Ordinal);
                symbolLookup = new Dictionary<string, Symbol>(StringComparer.Ordinal);

                foreach (var kvp in _subscriptions)
                {
                    var config = kvp.Key;
                    var requirement = kvp.Value;
                    var brokerageSymbol = GetBrokerageSymbol(config.Symbol);
                    symbolLookup[brokerageSymbol] = config.Symbol;

                    if (config.SecurityType == SecurityType.Option)
                    {
                        if (requirement.RequiresTrades)
                        {
                            optionTrades.Add(brokerageSymbol);
                        }

                        if (requirement.RequiresQuotes)
                        {
                            optionQuotes.Add(brokerageSymbol);
                        }
                    }
                    else
                    {
                        if (requirement.RequiresTrades)
                        {
                            trades.Add(brokerageSymbol);
                        }

                        if (requirement.RequiresQuotes)
                        {
                            quotes.Add(brokerageSymbol);
                        }
                    }
                }

                changed = !_tradeSymbols.SetEquals(trades) || !_quoteSymbols.SetEquals(quotes);
                optionsChanged = !_optionTradeSymbols.SetEquals(optionTrades) || !_optionQuoteSymbols.SetEquals(optionQuotes);
                if (!changed && !optionsChanged)
                {
                    return;
                }

                _tradeSymbols = trades;
                _quoteSymbols = quotes;
                _optionTradeSymbols = optionTrades;
                _optionQuoteSymbols = optionQuotes;
                _brokerageSymbolLookup = symbolLookup;
                _hasOptionSubscriptions = _optionTradeSymbols.Count > 0 || _optionQuoteSymbols.Count > 0;
            }

            if (changed)
            {
                if (!_isAuthenticated)
                {
                    _pendingSubscriptionUpdate = true;
                }
                else
                {
                    _ = SendSubscriptionUpdate(trades, quotes, CancellationToken.None, isOptions: false);
                }
            }

            if (optionsChanged)
            {
                if (!_isOptionsAuthenticated)
                {
                    _pendingOptionsSubscriptionUpdate = true;
                }
                else
                {
                    _ = SendSubscriptionUpdate(optionTrades, optionQuotes, CancellationToken.None, isOptions: true);
                }
            }
        }

        private async Task SendSubscriptionUpdate(HashSet<string> trades, HashSet<string> quotes, CancellationToken token, bool isOptions)
        {
            Log.Trace(
                $"AlpacaProxy: Sending {(isOptions ? "options" : "stock")} subscribe " +
                $"trades={trades.Count} quotes={quotes.Count}"
            );
            var msg = new Dictionary<string, object>
            {
                { "action", "subscribe" },
                { "trades", trades.ToArray() },
                { "quotes", quotes.ToArray() },
                { "bars", Array.Empty<string>() }
            };
            await SendMessage(msg, token, isOptions);
        }

        private async Task SendMessage(object msg, CancellationToken token, bool isOptions)
        {
            var ws = isOptions ? _optionsWs : _ws;
            var sendLock = isOptions ? _optionsSendLock : _sendLock;
            if (ws == null || ws.State != WebSocketState.Open)
            {
                return;
            }

            await sendLock.WaitAsync(token);
            try
            {
                if (ws == null || ws.State != WebSocketState.Open)
                {
                    return;
                }

                var bytes = MessagePackSerializer.Serialize(msg, MsgPackOptions);
                await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Binary, true, token);
            }
            catch (OperationCanceledException)
            {
            }
            catch (Exception ex)
            {
                Log.Error($"AlpacaProxy: Send error: {ex.Message}");
            }
            finally
            {
                sendLock.Release();
            }
        }

        private async Task ReceiveLoop(CancellationToken token, bool isOptions)
        {
            var ws = isOptions ? _optionsWs : _ws;
            while (ws != null && ws.State == WebSocketState.Open && !token.IsCancellationRequested)
            {
                var payload = await ReceiveMessage(token, ws);
                if (payload == null)
                {
                    break;
                }

                try
                {
                    if (TryDeserializeMessages(payload, out var messages))
                    {
                        foreach (var message in messages)
                        {
                            ProcessMessage(message, isOptions);
                        }
                    }
                }
                catch (Exception ex)
                {
                    Log.Error($"AlpacaProxy: Deserialize error: {ex.Message}");
                }
            }
        }

        private async Task<byte[]> ReceiveMessage(CancellationToken token, ClientWebSocket ws)
        {
            var buffer = new byte[8192];
            using var stream = new MemoryStream();

            while (true)
            {
                WebSocketReceiveResult result;
                try
                {
                    result = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), token);
                }
                catch (OperationCanceledException)
                {
                    return null;
                }

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    return null;
                }

                stream.Write(buffer, 0, result.Count);

                if (result.EndOfMessage)
                {
                    return stream.ToArray();
                }
            }
        }

        private async Task WatchdogLoop(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    if (_watchdogEnabled && ShouldForceStockReconnect())
                    {
                        ForceStockReconnect("stale stock stream");
                    }
                }
                catch (Exception ex)
                {
                    Log.Error($"AlpacaProxy: Watchdog error: {ex.Message}");
                }

                try
                {
                    await Task.Delay(_watchdogCheckInterval, token);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }

        private bool ShouldForceStockReconnect()
        {
            if (!_isAuthenticated || _ws == null || _ws.State != WebSocketState.Open)
            {
                return false;
            }

            if (_tradeSymbols.Count == 0 && _quoteSymbols.Count == 0)
            {
                return false;
            }

            var nowUtc = DateTime.UtcNow;
            if (!IsUsEquityMarketOpen(nowUtc))
            {
                return false;
            }

            var lastMessageUtc = new DateTime(Interlocked.Read(ref _lastStockMessageTicks), DateTimeKind.Utc);
            return nowUtc - lastMessageUtc > _staleDataWindow;
        }

        private static bool IsUsEquityMarketOpen(DateTime utcTime)
        {
            var easternTime = utcTime.ConvertFromUtc(TimeZones.NewYork);
            if (easternTime.DayOfWeek == DayOfWeek.Saturday || easternTime.DayOfWeek == DayOfWeek.Sunday)
            {
                return false;
            }

            var tod = easternTime.TimeOfDay;
            return tod >= new TimeSpan(9, 30, 0) && tod <= new TimeSpan(16, 0, 0);
        }

        private void ForceStockReconnect(string reason)
        {
            var ws = _ws;
            if (ws == null)
            {
                return;
            }

            Log.Error(
                $"AlpacaProxy: Forcing stock reconnect ({reason}). " +
                $"last_message_utc={new DateTime(Interlocked.Read(ref _lastStockMessageTicks), DateTimeKind.Utc):O} " +
                $"trades={_tradeSymbols.Count} quotes={_quoteSymbols.Count}"
            );

            _isConnected = false;
            _isAuthenticated = false;
            _pendingSubscriptionUpdate = true;

            try { ws.Abort(); } catch { }
            try { ws.Dispose(); } catch { }
            _ws = null;
        }

        private static bool TryDeserializeMessages(byte[] payload, out List<Dictionary<string, object>> messages)
        {
            messages = null;
            try
            {
                messages = MessagePackSerializer.Deserialize<List<Dictionary<string, object>>>(payload, MsgPackOptions);
                return messages != null;
            }
            catch
            {
                try
                {
                    var single = MessagePackSerializer.Deserialize<Dictionary<string, object>>(payload, MsgPackOptions);
                    if (single != null)
                    {
                        messages = new List<Dictionary<string, object>> { single };
                        return true;
                    }
                }
                catch
                {
                }
            }

            return TryDeserializeJsonMessages(payload, out messages);
        }

        private static bool TryDeserializeJsonMessages(byte[] payload, out List<Dictionary<string, object>> messages)
        {
            messages = null;
            try
            {
                using var doc = JsonDocument.Parse(payload);
                if (doc.RootElement.ValueKind == JsonValueKind.Array)
                {
                    var parsed = new List<Dictionary<string, object>>();
                    foreach (var item in doc.RootElement.EnumerateArray())
                    {
                        if (item.ValueKind != JsonValueKind.Object)
                        {
                            continue;
                        }

                        parsed.Add(ConvertJsonObject(item));
                    }

                    if (parsed.Count > 0)
                    {
                        messages = parsed;
                        return true;
                    }

                    return false;
                }

                if (doc.RootElement.ValueKind == JsonValueKind.Object)
                {
                    messages = new List<Dictionary<string, object>> { ConvertJsonObject(doc.RootElement) };
                    return true;
                }
            }
            catch
            {
            }

            return false;
        }

        private static Dictionary<string, object> ConvertJsonObject(JsonElement element)
        {
            var dict = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (var property in element.EnumerateObject())
            {
                dict[property.Name] = ConvertJsonValue(property.Value);
            }

            return dict;
        }

        private static object ConvertJsonValue(JsonElement element)
        {
            switch (element.ValueKind)
            {
                case JsonValueKind.String:
                    return element.GetString() ?? string.Empty;
                case JsonValueKind.Number:
                    if (element.TryGetInt64(out var intValue))
                    {
                        return intValue;
                    }

                    if (element.TryGetDouble(out var doubleValue))
                    {
                        return doubleValue;
                    }

                    return element.GetRawText();
                case JsonValueKind.True:
                    return true;
                case JsonValueKind.False:
                    return false;
                case JsonValueKind.Object:
                    return ConvertJsonObject(element);
                case JsonValueKind.Array:
                    var list = new List<object>();
                    foreach (var item in element.EnumerateArray())
                    {
                        list.Add(ConvertJsonValue(item));
                    }

                    return list;
                case JsonValueKind.Null:
                case JsonValueKind.Undefined:
                    return null;
                default:
                    return element.GetRawText();
            }
        }

        private void ProcessMessage(Dictionary<string, object> msg, bool isOptions)
        {
            var type = GetString(msg, "T");
            if (string.IsNullOrEmpty(type))
            {
                return;
            }

            if (type == "success")
            {
                var status = GetString(msg, "msg");
                if (string.Equals(status, "authenticated", StringComparison.OrdinalIgnoreCase))
                {
                    if (isOptions)
                    {
                        _isOptionsAuthenticated = true;
                        Log.Trace(
                            $"AlpacaProxy: Options stream authenticated. pendingSub={_pendingOptionsSubscriptionUpdate} " +
                            $"trades={_optionTradeSymbols.Count} quotes={_optionQuoteSymbols.Count}"
                        );
                        if (_pendingOptionsSubscriptionUpdate)
                        {
                            _pendingOptionsSubscriptionUpdate = false;
                            _ = SendSubscriptionUpdate(_optionTradeSymbols, _optionQuoteSymbols, CancellationToken.None, isOptions: true);
                        }
                    }
                    else
                    {
                        _isAuthenticated = true;
                        Interlocked.Exchange(ref _lastStockMessageTicks, DateTime.UtcNow.Ticks);
                        Log.Trace(
                            $"AlpacaProxy: Stock stream authenticated. pendingSub={_pendingSubscriptionUpdate} " +
                            $"trades={_tradeSymbols.Count} quotes={_quoteSymbols.Count}"
                        );
                        if (_pendingSubscriptionUpdate)
                        {
                            _pendingSubscriptionUpdate = false;
                            _ = SendSubscriptionUpdate(_tradeSymbols, _quoteSymbols, CancellationToken.None, isOptions: false);
                        }
                    }
                }
                return;
            }

            if (type == "error")
            {
                var error = GetString(msg, "msg");
                if (!string.IsNullOrWhiteSpace(error))
                {
                    Log.Error($"AlpacaProxy: Proxy error: {error}");
                }
                return;
            }

            if (type == "subscription")
            {
                var summary = DescribeSubscriptionMessage(msg);
                if (summary.Contains("invalid_trades=", StringComparison.Ordinal) &&
                    !summary.EndsWith("invalid_trades=none invalid_quotes=none", StringComparison.Ordinal))
                {
                    var stream = isOptions ? "Options" : "Stock";
                    Log.Error($"AlpacaProxy: {stream} subscription ack {summary}");
                }
                return;
            }

            var symbolValue = GetString(msg, "S");
            if (string.IsNullOrEmpty(symbolValue))
            {
                return;
            }

            if (!_brokerageSymbolLookup.TryGetValue(symbolValue, out var symbol))
            {
                MaybeLogStockLookupMiss(type, symbolValue);
                return;
            }

            var utcTime = ParseTimestamp(msg.TryGetValue("t", out var timeObj) ? timeObj : null);
            var exchangeTime = ConvertToExchangeTime(symbol, utcTime);

            switch (type)
            {
                case "t":
                    EmitTradeTick(symbol, exchangeTime, msg);
                    Interlocked.Exchange(ref _lastStockMessageTicks, DateTime.UtcNow.Ticks);
                    break;
                case "q":
                    EmitQuoteTick(symbol, exchangeTime, msg);
                    Interlocked.Exchange(ref _lastStockMessageTicks, DateTime.UtcNow.Ticks);
                    break;
            }
        }

        private void MaybeLogStockLookupMiss(string type, string symbolValue)
        {
            lock (_stockLookupMissesLogged)
            {
                if (_stockLookupMissesLogged.Count >= 10 || !_stockLookupMissesLogged.Add(symbolValue))
                {
                    return;
                }
            }

            Log.Error(
                $"AlpacaProxy: Stock payload dropped due to missing symbol lookup type={type} symbol={symbolValue} " +
                $"known={_brokerageSymbolLookup.Count}"
            );
        }

        private static string DescribeSubscriptionMessage(Dictionary<string, object> msg)
        {
            var trades = ExtractStringValues(msg.TryGetValue("trades", out var tradesValue) ? tradesValue : null);
            var quotes = ExtractStringValues(msg.TryGetValue("quotes", out var quotesValue) ? quotesValue : null);
            var invalidTrades = ExtractStringValues(msg.TryGetValue("invalid_trades", out var invalidTradesValue) ? invalidTradesValue : null);
            var invalidQuotes = ExtractStringValues(msg.TryGetValue("invalid_quotes", out var invalidQuotesValue) ? invalidQuotesValue : null);

            return
                $"trades={trades.Count} quotes={quotes.Count} " +
                $"invalid_trades={FormatSymbolList(invalidTrades)} invalid_quotes={FormatSymbolList(invalidQuotes)}";
        }

        private static List<string> ExtractStringValues(object? value)
        {
            var values = new List<string>();
            if (value == null)
            {
                return values;
            }

            if (value is string scalar)
            {
                if (!string.IsNullOrWhiteSpace(scalar))
                {
                    values.Add(scalar);
                }
                return values;
            }

            if (value is IEnumerable enumerable)
            {
                foreach (var item in enumerable)
                {
                    if (item == null)
                    {
                        continue;
                    }

                    var text = item as string ?? item.ToString();
                    if (!string.IsNullOrWhiteSpace(text))
                    {
                        values.Add(text);
                    }
                }
            }

            return values;
        }

        private static string FormatSymbolList(List<string> values)
        {
            return values.Count == 0 ? "none" : string.Join(",", values);
        }

        private static string GetBrokerageSymbol(Symbol symbol)
        {
            if (symbol.SecurityType == SecurityType.Option)
            {
                return GenerateBrokerageOptionSymbol(symbol);
            }
            return symbol.Value;
        }

        private static string GenerateBrokerageOptionSymbol(Symbol symbol)
        {
            var strikePriceString = (Convert.ToInt32(symbol.ID.StrikePrice * 1000)).ToStringInvariant("D8");
            return $"{symbol.Underlying.Value}{symbol.ID.Date:yyMMdd}{symbol.ID.OptionRight.ToString()[0]}{strikePriceString}";
        }

        private static string BuildOptionsProxyUrl(string proxyUrl)
        {
            if (string.IsNullOrWhiteSpace(proxyUrl))
            {
                return proxyUrl;
            }

            if (!Uri.TryCreate(proxyUrl, UriKind.Absolute, out var uri))
            {
                return proxyUrl;
            }

            var path = uri.AbsolutePath;
            if (path.EndsWith("/stream", StringComparison.OrdinalIgnoreCase))
            {
                path = path + "/options";
            }
            else if (path.EndsWith("/stream/", StringComparison.OrdinalIgnoreCase))
            {
                path = path + "options";
            }
            else
            {
                path = path.TrimEnd('/') + "/stream/options";
            }

            var builder = new UriBuilder(uri) { Path = path };
            return builder.Uri.ToString();
        }

        private void EmitTradeTick(Symbol symbol, DateTime time, Dictionary<string, object> msg)
        {
            var price = GetDecimal(msg, "p");
            if (!price.HasValue)
            {
                return;
            }

            var size = GetDecimal(msg, "s") ?? 0m;
            var tick = new Tick
            {
                Symbol = symbol,
                Time = time,
                Value = price.Value,
                TickType = TickType.Trade,
                Quantity = size
            };

            _aggregator.Update(tick);
        }

        private void EmitQuoteTick(Symbol symbol, DateTime time, Dictionary<string, object> msg)
        {
            var bid = GetDecimal(msg, "bp");
            var ask = GetDecimal(msg, "ap");
            if (!bid.HasValue && !ask.HasValue)
            {
                return;
            }

            var bidSize = GetDecimal(msg, "bs") ?? 0m;
            var askSize = GetDecimal(msg, "as") ?? 0m;
            var tick = new Tick(time, symbol, string.Empty, string.Empty,
                bidSize: bidSize,
                bidPrice: bid ?? 0m,
                askPrice: ask ?? 0m,
                askSize: askSize)
            {
                TickType = TickType.Quote
            };

            _aggregator.Update(tick);
        }

        private DateTime ConvertToExchangeTime(Symbol symbol, DateTime utcTime)
        {
            if (utcTime.Kind != DateTimeKind.Utc)
            {
                utcTime = DateTime.SpecifyKind(utcTime, DateTimeKind.Utc);
            }

            if (!_timeZoneProviders.TryGetValue(symbol, out var provider))
            {
                var exchangeHours = _marketHoursDatabase.GetExchangeHours(symbol.ID.Market, symbol, symbol.SecurityType);
                provider = new TimeZoneOffsetProvider(exchangeHours.TimeZone, utcTime, Time.EndOfTime);
                _timeZoneProviders[symbol] = provider;
            }

            return provider.ConvertFromUtc(utcTime);
        }

        private static DateTime ParseTimestamp(object value)
        {
            if (value == null)
            {
                return DateTime.UtcNow;
            }

            switch (value)
            {
                case DateTime dt:
                    return dt.Kind == DateTimeKind.Utc ? dt : dt.ToUniversalTime();
                case string text:
                    if (DateTime.TryParse(text, CultureInfo.InvariantCulture,
                        DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var parsed))
                    {
                        return parsed;
                    }
                    break;
                case byte[] bytes:
                    var decoded = Encoding.UTF8.GetString(bytes);
                    if (DateTime.TryParse(decoded, CultureInfo.InvariantCulture,
                        DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var parsedBytes))
                    {
                        return parsedBytes;
                    }
                    break;
                default:
                    try
                    {
                        var longValue = Convert.ToInt64(value, CultureInfo.InvariantCulture);
                        return UnixTimeToDateTime(longValue);
                    }
                    catch
                    {
                    }
                    break;
            }

            return DateTime.UtcNow;
        }

        private static DateTime UnixTimeToDateTime(long value)
        {
            if (value > 1_000_000_000_000_000L)
            {
                return DateTimeOffset.FromUnixTimeMilliseconds(value / 1_000_000L).UtcDateTime;
            }

            if (value > 1_000_000_000_000L)
            {
                return DateTimeOffset.FromUnixTimeMilliseconds(value).UtcDateTime;
            }

            return DateTimeOffset.FromUnixTimeSeconds(value).UtcDateTime;
        }

        private static string GetString(Dictionary<string, object> msg, string key)
        {
            if (!msg.TryGetValue(key, out var value) || value == null)
            {
                return null;
            }

            return ConvertToString(value);
        }

        private static string ConvertToString(object value)
        {
            if (value is string text)
            {
                return text;
            }

            if (value is byte[] bytes)
            {
                return Encoding.UTF8.GetString(bytes);
            }

            return value.ToString();
        }

        private static decimal? GetDecimal(Dictionary<string, object> msg, string key)
        {
            if (!msg.TryGetValue(key, out var value) || value == null)
            {
                return null;
            }

            try
            {
                if (value is string text && decimal.TryParse(text, NumberStyles.Any, CultureInfo.InvariantCulture, out var parsed))
                {
                    return parsed;
                }

                if (value is byte[] bytes && decimal.TryParse(Encoding.UTF8.GetString(bytes), NumberStyles.Any,
                    CultureInfo.InvariantCulture, out var parsedBytes))
                {
                    return parsedBytes;
                }

                return Convert.ToDecimal(value, CultureInfo.InvariantCulture);
            }
            catch
            {
                return null;
            }
        }

        private sealed class SubscriptionRequirement
        {
            public bool RequiresTrades { get; init; }
            public bool RequiresQuotes { get; init; }

            public static SubscriptionRequirement FromConfig(SubscriptionDataConfig config)
            {
                var requiresTrades = false;
                var requiresQuotes = false;

                if (config.Type == typeof(Tick))
                {
                    requiresTrades = config.TickType == TickType.Trade;
                    requiresQuotes = config.TickType == TickType.Quote;
                }
                else if (config.Type == typeof(TradeBar))
                {
                    requiresTrades = true;
                }
                else if (config.Type == typeof(QuoteBar))
                {
                    requiresQuotes = true;
                }

                return new SubscriptionRequirement
                {
                    RequiresTrades = requiresTrades,
                    RequiresQuotes = requiresQuotes
                };
            }
        }

        private sealed class SubscriptionDataConfigComparer : IEqualityComparer<SubscriptionDataConfig>
        {
            public bool Equals(SubscriptionDataConfig? x, SubscriptionDataConfig? y)
            {
                if (ReferenceEquals(x, y))
                {
                    return true;
                }

                if (x == null || y == null)
                {
                    return false;
                }

                return x.Symbol == y.Symbol
                    && x.Type == y.Type
                    && x.Resolution == y.Resolution
                    && x.TickType == y.TickType
                    && x.Market == y.Market;
            }

            public int GetHashCode(SubscriptionDataConfig obj)
            {
                if (obj == null)
                {
                    return 0;
                }

                return HashCode.Combine(obj.Symbol, obj.Type, obj.Resolution, obj.TickType, obj.Market);
            }
        }
    }
}
