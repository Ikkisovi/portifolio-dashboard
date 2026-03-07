param(
    [string]$Image = "lean-alpaca-proxy:net10"
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Read-Choice {
    param(
        [string]$Prompt,
        [int]$MaxIndex
    )
    $selection = Read-Host $Prompt
    if (-not ($selection -as [int])) {
        return $null
    }
    $index = [int]$selection
    if ($index -lt 1 -or $index -gt $MaxIndex) {
        return $null
    }
    return $index - 1
}

function ConvertTo-ProcessArgumentString {
    param(
        [string[]]$InputArgs
    )

    $parts = @()
    foreach ($arg in $InputArgs) {
        if ($null -eq $arg) {
            continue
        }
        $text = [string]$arg
        if ($text -match '[\s"`]') {
            $escaped = $text -replace '(\\*)"', '$1$1\"'
            $escaped = $escaped -replace '(\\+)$', '$1$1'
            $parts += ('"' + $escaped + '"')
        }
        else {
            $parts += $text
        }
    }
    return ($parts -join " ")
}

function Get-RedactedDockerArgumentString {
    param(
        [string[]]$InputArgs
    )

    $sensitiveEnvNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    @(
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "ALPACA_PROXY_TOKEN",
        "IB_ACCOUNT",
        "IB_PASSWORD",
        "IB_USER_NAME",
        "QC_API_ACCESS_TOKEN",
        "QC_JOB_ORGANIZATION_ID",
        "QC_JOB_USER_ID"
    ) | ForEach-Object { [void]$sensitiveEnvNames.Add($_) }

    $redactedArgs = @()
    for ($i = 0; $i -lt $InputArgs.Count; $i++) {
        $arg = [string]$InputArgs[$i]
        if ($arg -eq "-e" -and ($i + 1) -lt $InputArgs.Count) {
            $assignment = [string]$InputArgs[$i + 1]
            $parts = $assignment -split "=", 2
            if ($parts.Count -eq 2 -and $sensitiveEnvNames.Contains($parts[0])) {
                $redactedArgs += $arg
                $redactedArgs += ($parts[0] + "=***REDACTED***")
                $i++
                continue
            }
        }
        $redactedArgs += $arg
    }

    return ConvertTo-ProcessArgumentString -InputArgs $redactedArgs
}

function Get-BrokerageLabel {
    param([string]$ConfigPath)
    try {
        $config = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
        if ($config.brokerage) {
            return [string]$config.brokerage
        }
    }
    catch {}
    return "Unknown"
}

function Get-BrokerageDisplayName {
    param([string]$Brokerage)
    switch ($Brokerage) {
        "InteractiveBrokersBrokerage" { return "IBKR" }
        "PaperBrokerage" { return "Paper" }
        default { return $Brokerage }
    }
}

function Get-ProxyMode {
    param(
        [string]$Brokerage,
        [string]$DataFeed
    )
    $isLive = $true
    $isPro = $true
    if ($Brokerage -eq "PaperBrokerage") {
        $isLive = $false
    }
    if ($DataFeed) {
        switch ($DataFeed.Trim().ToLower()) {
            "iex" { $isPro = $false }
            "sip" { $isPro = $true }
        }
    }
    return @{
        IsLive = $isLive
        IsPro  = $isPro
    }
}

function Get-ProxyPort {
    param([string]$ProxyUrl)
    $port = 8765
    if (-not $ProxyUrl) {
        return $port
    }
    try {
        $uri = [Uri]$ProxyUrl
        if ($uri.Port -gt 0) {
            $port = $uri.Port
        }
    }
    catch {}
    return $port
}

function Get-HistoryUrlFromProxy {
    param([string]$ProxyUrl)
    if (-not $ProxyUrl) {
        return $null
    }
    try {
        $uri = [Uri]$ProxyUrl
        $scheme = if ($uri.Scheme -eq "wss") { "https" } else { "http" }
        $port = if ($uri.Port -eq 8765) { 8766 } elseif ($uri.Port -eq 8767) { 8768 } else { $uri.Port }
        $builder = New-Object System.UriBuilder($uri)
        $builder.Scheme = $scheme
        $builder.Port = $port
        $builder.Path = "/v1/history/bars"
        $builder.Query = ""
        return $builder.Uri.AbsoluteUri
    }
    catch {
        return $null
    }
}

function Test-ProxyIsLocal {
    param([string]$ProxyUrl)
    if (-not $ProxyUrl) {
        return $true
    }
    try {
        $uri = [Uri]$ProxyUrl
        $proxyHost = $uri.Host.ToLowerInvariant()
        if ($proxyHost -in @("localhost", "127.0.0.1", "::1", "host.docker.internal")) {
            return $true
        }
    }
    catch {}
    return $false
}

function Test-TruthyValue {
    param(
        [string]$Value,
        [bool]$Default = $false
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $Default
    }
    $normalized = $Value.Trim().ToLowerInvariant()
    return $normalized -in @("1", "true", "yes", "y", "on")
}

function Set-EnvDefault {
    param(
        [string]$Name,
        [string]$Value
    )
    $current = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($current)) {
        Set-Item -Path ("Env:{0}" -f $Name) -Value $Value
    }
}

function Get-DotNetRuntimeMajor {
    param([string]$Image)
    try {
        $lines = docker run --rm --entrypoint dotnet $Image --list-runtimes 2>$null
        if (-not $lines) {
            return $null
        }
        $maxMajor = $null
        foreach ($line in $lines) {
            if ($line -match '^Microsoft\.NETCore\.App\s+([0-9]+)\.') {
                $major = [int]$Matches[1]
                if (($maxMajor -eq $null) -or ($major -gt $maxMajor)) {
                    $maxMajor = $major
                }
            }
        }
        if ($maxMajor -ne $null) {
            return $maxMajor
        }
    }
    catch {}
    return $null
}

function Get-EngineDepsTargetMajor {
    param([string]$DepsPath)
    if (-not (Test-Path $DepsPath)) {
        return $null
    }
    try {
        $deps = Get-Content -Raw -Path $DepsPath | ConvertFrom-Json
        $runtimeTargetName = [string]$deps.runtimeTarget.name
        if ($runtimeTargetName -match 'Version=v([0-9]+)\.') {
            return [int]$Matches[1]
        }
    }
    catch {}
    return $null
}

function Get-CryptoStreamUrl {
    param(
        [string]$ProxyUrl,
        [string]$DefaultUrl
    )
    if (-not $ProxyUrl) {
        return $DefaultUrl
    }
    try {
        $uri = [Uri]$ProxyUrl
        $builder = New-Object System.UriBuilder($uri)
        $builder.Path = "/stream/crypto"
        $builder.Query = ""
        return $builder.Uri.AbsoluteUri
    }
    catch {
        return $DefaultUrl
    }
}

function Wait-ProxyHealth {
    param(
        [int]$HttpPort,
        [int]$Retries = 10,
        [int]$DelayMs = 500
    )
    $healthUrl = "http://127.0.0.1:$HttpPort/health"
    for ($i = 0; $i -lt $Retries; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                return $true
            }
        }
        catch {
            Start-Sleep -Milliseconds $DelayMs
        }
    }
    return $false
}

function Ensure-ProxyAgent {
    param(
        [string]$Name,
        [string]$Image,
        [bool]$IsLive,
        [bool]$IsPro,
        [int]$Port,
        [string]$Mode = "direct",
        [string]$ProxyToken
    )
    $runningByName = docker ps --filter "name=^${Name}$" --format "{{.Names}}"
    if ($runningByName) {
        $currentMode = "direct"
        try {
            $envLine = docker inspect --format "{{range .Config.Env}}{{println .}}{{end}}" $Name `
                | Where-Object { $_ -like "MODE=*" } | Select-Object -First 1
            if ($envLine) {
                $currentMode = ($envLine -split "=", 2)[1].ToLower()
            }
        }
        catch {}
        if ($currentMode -ne $Mode) {
            Write-Warning "Proxy agent $Name is running MODE=$currentMode but $Mode requested. Restarting automatically."
            docker rm -f $Name 2>$null | Out-Null
        }
        else {
            Write-Host "Proxy agent $Name is already running; skipping start."
            return
        }
    }

    $runningOnPort = docker ps --filter "publish=$Port" --format "{{.Names}}"
    if ($runningOnPort) {
        if ($runningOnPort -ne $Name) {
            Write-Error "Port ${Port} is already in use by: $runningOnPort. Stop it first so $Name can launch."
            throw "Proxy port conflict"
        }
    }

    $envLive = if ($IsLive) { "true" } else { "false" }
    $envPro = if ($IsPro) { "true" } else { "false" }
    Write-Host "Starting proxy agent $Name ($Image) IS_LIVE=$envLive IS_PRO=$envPro..."
    $httpPort = $Port + 1
    $cursorHostPath = Join-Path $root ".cursor"
    if (-not (Test-Path $cursorHostPath)) {
        New-Item -ItemType Directory -Path $cursorHostPath | Out-Null
    }
    $composeFile = Join-Path $root "proxy\\agent\\docker-compose.proxy-agent.yml"
    if (-not (Test-Path $composeFile)) {
        Write-Error "Missing compose file: $composeFile"
        throw "Proxy compose missing"
    }
    $proxyEnvPath = Join-Path $cursorHostPath "proxy-agent.env"
    $envLines = @(
        "ALPACA_PROXY_IMAGE=$Image",
        "ALPACA_PROXY_CONTAINER=$Name",
        "ALPACA_PROXY_IS_LIVE=$envLive",
        "ALPACA_PROXY_IS_PRO=$envPro",
        "ALPACA_PROXY_MODE=$Mode",
        "PROXY_PORT=$Port",
        "PROXY_HTTP_PORT=$httpPort",
        "CURSOR_HOST_PATH=$cursorHostPath",
        "PROJECT_ROOT=$root"
    )
    if ($ProxyToken) {
        $envLines += "ALPACA_PROXY_TOKEN=$ProxyToken"
    }
    if ($env:ALPACA_API_KEY) {
        $envLines += "ALPACA_API_KEY=$env:ALPACA_API_KEY"
    }
    if ($env:ALPACA_API_SECRET) {
        $envLines += "ALPACA_API_SECRET=$env:ALPACA_API_SECRET"
    }
    $envLines | Set-Content -Path $proxyEnvPath -Encoding UTF8
    Remove-Item Env:PROXY_PORT -ErrorAction SilentlyContinue
    Remove-Item Env:PROXY_HTTP_PORT -ErrorAction SilentlyContinue
    docker rm -f $Name 2>$null | Out-Null
    docker compose --env-file $proxyEnvPath -f $composeFile up -d --remove-orphans | Out-Null
    docker compose --env-file $proxyEnvPath -f $composeFile ps
    $running = docker ps --filter "name=^${Name}$" --format "{{.Names}}"
    if (-not $running) {
        Write-Error "Proxy agent container failed to start: $Name"
        docker compose --env-file $proxyEnvPath -f $composeFile ps
        throw "Proxy container not running"
    }
    Write-Host "Proxy agent container started: $Name (ports ${Port}->8765, ${httpPort}->8766)"
    docker logs --tail 50 $Name
}

function Import-EnvFromFile {
    param(
        [string]$Path,
        [switch]$OnlyIfUnset
    )
    $lines = Get-Content -Path $Path
    foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if (-not $trimmed) { continue }
        if ($trimmed.StartsWith("#")) { continue }
        $name = $null
        $value = $null
        if ($trimmed -match '^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $name = $Matches[1]
            $value = $Matches[2]
        }
        elseif ($trimmed -match '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $name = $Matches[1]
            $value = $Matches[2]
        }
        if (-not $name) {
            continue
        }
        if ($value.Length -ge 2) {
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        if ($OnlyIfUnset -and -not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            continue
        }
        Set-Item -Path "Env:$name" -Value $value
    }
}

function Test-AlpacaDirectHistoryPreflight {
    param(
        [string]$ApiKey,
        [string]$ApiSecret,
        [string]$DataUrl,
        [string]$DataFeed
    )

    if ([string]::IsNullOrWhiteSpace($ApiKey) -or [string]::IsNullOrWhiteSpace($ApiSecret)) {
        return @{
            Success    = $false
            Error      = "Missing Alpaca API credentials."
            RequestUrl = $null
            Bars       = 0
        }
    }

    if ([string]::IsNullOrWhiteSpace($DataUrl)) {
        $DataUrl = "https://data.alpaca.markets"
    }

    $requestUrl = $null
    try {
        $endUtc = [DateTime]::UtcNow
        $startUtc = $endUtc.AddDays(-7)
        $query = [ordered]@{
            symbols    = "SPY"
            timeframe  = "1Day"
            start      = $startUtc.ToString("yyyy-MM-ddTHH:mm:ssZ")
            end        = $endUtc.ToString("yyyy-MM-ddTHH:mm:ssZ")
            limit      = "2"
            adjustment = "raw"
        }
        if (-not [string]::IsNullOrWhiteSpace($DataFeed)) {
            $query["feed"] = $DataFeed
        }

        $queryParts = @()
        foreach ($pair in $query.GetEnumerator()) {
            $queryParts += ("{0}={1}" -f [Uri]::EscapeDataString([string]$pair.Key), [Uri]::EscapeDataString([string]$pair.Value))
        }

        $requestUrl = "{0}/v2/stocks/bars?{1}" -f $DataUrl.TrimEnd("/"), ($queryParts -join "&")
        $headers = @{
            "APCA-API-KEY-ID"     = $ApiKey
            "APCA-API-SECRET-KEY" = $ApiSecret
        }
        $response = Invoke-RestMethod -Uri $requestUrl -Headers $headers -Method Get -TimeoutSec 15

        $barsCount = 0
        if ($response -and $response.PSObject.Properties["bars"]) {
            $barsBySymbol = $response.bars
            if ($barsBySymbol -and $barsBySymbol.PSObject.Properties["SPY"]) {
                $spyBars = $barsBySymbol.SPY
                if ($spyBars -is [System.Array]) {
                    $barsCount = $spyBars.Count
                }
                elseif ($null -ne $spyBars) {
                    $barsCount = 1
                }
            }
        }

        return @{
            Success    = $true
            Error      = $null
            RequestUrl = $requestUrl
            Bars       = $barsCount
        }
    }
    catch {
        $errorText = $_.Exception.Message
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $errorText = "$errorText | $($_.ErrorDetails.Message)"
        }
        return @{
            Success    = $false
            Error      = $errorText
            RequestUrl = $requestUrl
            Bars       = 0
        }
    }
}

function Get-JsonValue {
    param(
        [object]$Object,
        [string]$Name
    )
    if ($null -eq $Object) {
        return $null
    }
    if ($Object -is [System.Collections.IDictionary]) {
        return $Object[$Name]
    }
    return $Object.$Name
}

function Set-JsonValue {
    param(
        [object]$Object,
        [string]$Name,
        [object]$Value
    )
    if ($null -eq $Object) {
        return
    }
    if ($Object -is [System.Collections.IDictionary]) {
        $Object[$Name] = $Value
        return
    }
    if ($Object.PSObject.Properties[$Name]) {
        $Object.$Name = $Value
    }
    else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Normalize-OptionalIntConfig {
    param(
        [object]$Object,
        [string]$Name,
        [int]$DefaultValue = 0
    )
    $rawValue = Get-JsonValue -Object $Object -Name $Name
    if ($null -eq $rawValue) {
        Set-JsonValue -Object $Object -Name $Name -Value $DefaultValue
        return
    }
    if ($rawValue -is [string] -and [string]::IsNullOrWhiteSpace($rawValue)) {
        Set-JsonValue -Object $Object -Name $Name -Value $DefaultValue
        return
    }
    try {
        $parsedValue = [int]$rawValue
        Set-JsonValue -Object $Object -Name $Name -Value $parsedValue
    }
    catch {
        Set-JsonValue -Object $Object -Name $Name -Value $DefaultValue
    }
}

function Normalize-OptionalStringConfig {
    param(
        [object]$Object,
        [string]$Name,
        [string]$DefaultValue = ""
    )
    $rawValue = Get-JsonValue -Object $Object -Name $Name
    if ($null -eq $rawValue) {
        Set-JsonValue -Object $Object -Name $Name -Value $DefaultValue
        return
    }
    $normalizedValue = [string]$rawValue
    if ([string]::IsNullOrWhiteSpace($normalizedValue)) {
        Set-JsonValue -Object $Object -Name $Name -Value $DefaultValue
        return
    }
    Set-JsonValue -Object $Object -Name $Name -Value $normalizedValue
}

function Remove-JsonArrayValue {
    param(
        [object]$Object,
        [string]$Name,
        [string]$ValueToRemove
    )
    if ($null -eq $Object) {
        return $false
    }

    $rawValue = Get-JsonValue -Object $Object -Name $Name
    if ($null -eq $rawValue) {
        return $false
    }

    $updated = @()
    $removed = $false
    foreach ($entry in @($rawValue)) {
        if ([string]$entry -eq $ValueToRemove) {
            $removed = $true
            continue
        }
        $updated += $entry
    }

    if ($removed) {
        Set-JsonValue -Object $Object -Name $Name -Value @($updated)
    }

    return $removed
}

function Remove-InteractiveBrokersDataQueueHandler {
    param([object]$ConfigObject)

    $ibHandler = "QuantConnect.Brokerages.InteractiveBrokers.InteractiveBrokersBrokerage"
    $removedAny = Remove-JsonArrayValue -Object $ConfigObject -Name "data-queue-handler" -ValueToRemove $ibHandler

    $environments = Get-JsonValue -Object $ConfigObject -Name "environments"
    if ($null -ne $environments) {
        if ($environments -is [System.Collections.IDictionary]) {
            foreach ($key in $environments.Keys) {
                if (Remove-JsonArrayValue -Object $environments[$key] -Name "data-queue-handler" -ValueToRemove $ibHandler) {
                    $removedAny = $true
                }
            }
        }
        else {
            foreach ($property in $environments.PSObject.Properties) {
                if (Remove-JsonArrayValue -Object $property.Value -Name "data-queue-handler" -ValueToRemove $ibHandler) {
                    $removedAny = $true
                }
            }
        }
    }

    return $removedAny
}

function Copy-StrategySource {
    param(
        [string]$SourceDir,
        [string]$DestinationDir
    )
    if (-not (Test-Path $DestinationDir)) {
        New-Item -ItemType Directory -Path $DestinationDir | Out-Null
    }
    $excludeDirs = @("live", "backtests", "__pycache__")
    $sourceRoot = (Resolve-Path $SourceDir).Path
    $files = Get-ChildItem -Path $SourceDir -Recurse -File
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($sourceRoot.Length)
        if ($relative.StartsWith("\") -or $relative.StartsWith("/")) {
            $relative = $relative.Substring(1)
        }
        $parts = $relative -split '[\\/]'
        if ($parts | Where-Object { $excludeDirs -contains $_ }) {
            continue
        }
        $destPath = Join-Path $DestinationDir $relative
        $destParent = Split-Path -Parent $destPath
        if (-not (Test-Path $destParent)) {
            New-Item -ItemType Directory -Path $destParent | Out-Null
        }
        Copy-Item -Path $file.FullName -Destination $destPath -Force
    }
}

$secretsPs1Path = Join-Path $root "secrets.local.ps1"
$secretsTxtPath = Join-Path $root "secrets.txt"
$secretsLocalTxtPath = Join-Path $root "secrets.local.txt"
if (Test-Path $secretsPs1Path) {
    . $secretsPs1Path
}
if (Test-Path $secretsTxtPath) {
    Import-EnvFromFile -Path $secretsTxtPath
}
if (Test-Path $secretsLocalTxtPath) {
    Import-EnvFromFile -Path $secretsLocalTxtPath -OnlyIfUnset
}
$leanLocalNoAuth = Test-TruthyValue -Value $env:LEAN_LOCAL_NOAUTH -Default $false

$strategyDirs = Get-ChildItem -Path $root -Directory -Filter "S_*" | Sort-Object Name
if (-not $strategyDirs) {
    Write-Error "No strategy folders found (S_*)."
    exit 1
}

Write-Host "Select strategy:"
for ($i = 0; $i -lt $strategyDirs.Count; $i++) {
    Write-Host ("[{0}] {1}" -f ($i + 1), $strategyDirs[$i].Name)
}

$strategyIndex = Read-Choice -Prompt "Enter number" -MaxIndex $strategyDirs.Count
if ($null -eq $strategyIndex) {
    Write-Error "Invalid selection."
    exit 1
}

$strategyDir = $strategyDirs[$strategyIndex]
$isCrypto = $strategyDir.Name.ToLowerInvariant().Contains("crypto")
if ($isCrypto -and -not $env:ALPACA_CRYPTO_REST_URL) {
    $env:ALPACA_CRYPTO_REST_URL = "http://host.docker.internal:8766/v1/crypto/us/latest/orderbooks"
}
$configFiles = Get-ChildItem -Path $strategyDir.FullName -Filter "launcher_config*.json" | Sort-Object Name
if (-not $configFiles) {
    Write-Error "No launcher_config*.json found in $($strategyDir.FullName)."
    exit 1
}

$brokerageMap = @{}
foreach ($file in $configFiles) {
    $brokerage = Get-BrokerageLabel -ConfigPath $file.FullName
    if (-not $brokerageMap.ContainsKey($brokerage)) {
        $brokerageMap[$brokerage] = @()
    }
    $brokerageMap[$brokerage] += $file
}

$brokerageKeys = @($brokerageMap.Keys | Sort-Object)
$selectedBrokerage = $null

if ($brokerageKeys.Count -gt 1) {
    Write-Host ""
    Write-Host "Select brokerage:"
    for ($i = 0; $i -lt $brokerageKeys.Count; $i++) {
        $label = Get-BrokerageDisplayName -Brokerage $brokerageKeys[$i]
        Write-Host ("[{0}] {1}" -f ($i + 1), $label)
    }

    $brokerageIndex = Read-Choice -Prompt "Enter number" -MaxIndex $brokerageKeys.Count
    if ($null -eq $brokerageIndex) {
        Write-Error "Invalid selection."
        exit 1
    }
    $selectedBrokerage = $brokerageKeys[$brokerageIndex]
}
else {
    $selectedBrokerage = $brokerageKeys[0]
}

$candidateConfigs = @($brokerageMap[$selectedBrokerage])
$configFile = $null
if ($candidateConfigs.Count -eq 1) {
    # If there is only one config, pick it automatically
    $configFile = $candidateConfigs[0]
}
else {
    Write-Host ""
    Write-Host "Select config:"
    for ($i = 0; $i -lt $candidateConfigs.Count; $i++) {
        $brokerage = Get-BrokerageLabel -ConfigPath $candidateConfigs[$i].FullName
        Write-Host ("[{0}] {1} ({2})" -f ($i + 1), $candidateConfigs[$i].Name, $brokerage)
    }

    $configIndex = Read-Choice -Prompt "Enter number" -MaxIndex $candidateConfigs.Count
    if ($null -eq $configIndex) {
        Write-Error "Invalid selection."
        exit 1
    }
    $configFile = $candidateConfigs[$configIndex]
}

$configJson = Get-Content -Raw -Path $configFile.FullName | ConvertFrom-Json
$rootLauncherConfigPath = Join-Path $root "launcher_config.json"
$rootLauncherConfig = $null
if (Test-Path $rootLauncherConfigPath) {
    try {
        $rootLauncherConfig = Get-Content -Raw -Path $rootLauncherConfigPath | ConvertFrom-Json
    }
    catch {}
}

$feedConfig = Get-JsonValue -Object $configJson -Name "alpaca-history-feed"
if (-not $feedConfig) {
    $feedConfig = Get-JsonValue -Object $configJson -Name "alpaca-data-feed"
}
if (-not $env:ALPACA_DATA_FEED -and $feedConfig) {
    $env:ALPACA_DATA_FEED = [string]$feedConfig
}
if (-not $env:ALPACA_DATA_FEED -and $rootLauncherConfig) {
    $rootFeedConfig = Get-JsonValue -Object $rootLauncherConfig -Name "alpaca-history-feed"
    if (-not $rootFeedConfig) {
        $rootFeedConfig = Get-JsonValue -Object $rootLauncherConfig -Name "alpaca-data-feed"
    }
    if ($rootFeedConfig) {
        $env:ALPACA_DATA_FEED = [string]$rootFeedConfig
    }
}
$dataUrlConfig = Get-JsonValue -Object $configJson -Name "alpaca-data-url"
if (-not $env:ALPACA_DATA_URL -and $dataUrlConfig) {
    $env:ALPACA_DATA_URL = [string]$dataUrlConfig
}
if (-not $env:ALPACA_DATA_URL -and $rootLauncherConfig) {
    $rootDataUrl = Get-JsonValue -Object $rootLauncherConfig -Name "alpaca-data-url"
    if ($rootDataUrl) {
        $env:ALPACA_DATA_URL = [string]$rootDataUrl
    }
}
if (-not $env:ALPACA_DATA_URL) {
    $env:ALPACA_DATA_URL = "https://data.alpaca.markets"
}

if (-not $env:ALPACA_API_KEY) {
    $alpacaApiKey = Get-JsonValue -Object $configJson -Name "alpaca-api-key"
    if (-not $alpacaApiKey -and $rootLauncherConfig) {
        $alpacaApiKey = Get-JsonValue -Object $rootLauncherConfig -Name "alpaca-api-key"
    }
    if ($alpacaApiKey) {
        $env:ALPACA_API_KEY = [string]$alpacaApiKey
    }
}
if (-not $env:ALPACA_API_SECRET) {
    $alpacaApiSecret = Get-JsonValue -Object $configJson -Name "alpaca-api-secret"
    if (-not $alpacaApiSecret -and $rootLauncherConfig) {
        $alpacaApiSecret = Get-JsonValue -Object $rootLauncherConfig -Name "alpaca-api-secret"
    }
    if ($alpacaApiSecret) {
        $env:ALPACA_API_SECRET = [string]$alpacaApiSecret
    }
}

$ibUserName = $env:IB_USER_NAME
if (-not $ibUserName) {
    $ibUserName = Get-JsonValue -Object $configJson -Name "ib-user-name"
}
if (-not $ibUserName -and $rootLauncherConfig) {
    $ibUserName = Get-JsonValue -Object $rootLauncherConfig -Name "ib-user-name"
}
$ibAccount = $env:IB_ACCOUNT
if (-not $ibAccount) {
    $ibAccount = Get-JsonValue -Object $configJson -Name "ib-account"
}
if (-not $ibAccount -and $rootLauncherConfig) {
    $ibAccount = Get-JsonValue -Object $rootLauncherConfig -Name "ib-account"
}
$ibPassword = $env:IB_PASSWORD
if (-not $ibPassword) {
    $ibPassword = Get-JsonValue -Object $configJson -Name "ib-password"
}
if (-not $ibPassword -and $rootLauncherConfig) {
    $ibPassword = Get-JsonValue -Object $rootLauncherConfig -Name "ib-password"
}
$requiresIbCredentials = $selectedBrokerage -eq "InteractiveBrokersBrokerage"
if ($requiresIbCredentials) {
    if (-not $ibUserName) { $ibUserName = Read-Host "IB_USER_NAME" }
    if (-not $ibAccount) { $ibAccount = Read-Host "IB_ACCOUNT" }
    if (-not $ibPassword) { $ibPassword = Read-Host "IB_PASSWORD" }
    if (-not $ibUserName -or -not $ibAccount -or -not $ibPassword) {
        Write-Error "Missing IB credentials (IB_USER_NAME/IB_ACCOUNT/IB_PASSWORD)."
        exit 1
    }
}
if ($ibUserName) {
    $env:IB_USER_NAME = [string]$ibUserName
    Set-JsonValue -Object $configJson -Name "ib-user-name" -Value $env:IB_USER_NAME
}
if ($ibAccount) {
    $env:IB_ACCOUNT = [string]$ibAccount
    Set-JsonValue -Object $configJson -Name "ib-account" -Value $env:IB_ACCOUNT
}
if ($ibPassword) {
    $env:IB_PASSWORD = [string]$ibPassword
    Set-JsonValue -Object $configJson -Name "ib-password" -Value $env:IB_PASSWORD
}

if ($leanLocalNoAuth) {
    Normalize-OptionalIntConfig -Object $configJson -Name "job-user-id"
    Normalize-OptionalIntConfig -Object $configJson -Name "project-id"
    Normalize-OptionalStringConfig -Object $configJson -Name "api-access-token"
    Normalize-OptionalStringConfig -Object $configJson -Name "job-organization-id"
    if ($selectedBrokerage -eq "PaperBrokerage") {
        if (Remove-InteractiveBrokersDataQueueHandler -ConfigObject $configJson) {
            Write-Host "Removed InteractiveBrokers data queue handler for LEAN_LOCAL_NOAUTH paper live."
        }
    }
    Write-Host "QC auth skipped: LEAN_LOCAL_NOAUTH=1"
}
else {
    # Ensure required QuantConnect subscription credentials exist in runtime config.
    $qcUserId = $env:QC_JOB_USER_ID
    if (-not $qcUserId -and $env:QC_USER_ID) { $qcUserId = $env:QC_USER_ID }
    $qcApiToken = $env:QC_API_ACCESS_TOKEN
    if (-not $qcApiToken -and $env:QC_API_TOKEN) { $qcApiToken = $env:QC_API_TOKEN }
    $qcOrgId = $env:QC_JOB_ORGANIZATION_ID
    if (-not $qcOrgId -and $env:QC_ORGANIZATION_ID) { $qcOrgId = $env:QC_ORGANIZATION_ID }

    if (-not $qcUserId) {
        $qcUserId = Get-JsonValue -Object $configJson -Name "job-user-id"
    }
    if (-not $qcApiToken) {
        $qcApiToken = Get-JsonValue -Object $configJson -Name "api-access-token"
    }
    if (-not $qcOrgId) {
        $qcOrgId = Get-JsonValue -Object $configJson -Name "job-organization-id"
    }
    if (-not $qcUserId -and $rootLauncherConfig) {
        $qcUserId = Get-JsonValue -Object $rootLauncherConfig -Name "job-user-id"
    }
    if (-not $qcApiToken -and $rootLauncherConfig) {
        $qcApiToken = Get-JsonValue -Object $rootLauncherConfig -Name "api-access-token"
    }
    if (-not $qcOrgId -and $rootLauncherConfig) {
        $qcOrgId = Get-JsonValue -Object $rootLauncherConfig -Name "job-organization-id"
    }
    if (-not $qcUserId) { $qcUserId = Read-Host "QC job-user-id" }
    if (-not $qcApiToken) { $qcApiToken = Read-Host "QC api-access-token" }
    if (-not $qcOrgId) { $qcOrgId = Read-Host "QC job-organization-id" }
    if (-not $qcUserId -or -not $qcApiToken -or -not $qcOrgId) {
        Write-Error "Missing QuantConnect auth fields (job-user-id/api-access-token/job-organization-id)."
        exit 1
    }

    Set-JsonValue -Object $configJson -Name "job-user-id" -Value $qcUserId
    Set-JsonValue -Object $configJson -Name "api-access-token" -Value $qcApiToken
    Set-JsonValue -Object $configJson -Name "job-organization-id" -Value $qcOrgId
    Write-Host "QC auth ready in runtime config (job-user-id/api-access-token/job-organization-id)."
}

$algorithmName = Get-JsonValue -Object $configJson -Name "algorithm-type-name"
$historyOnly = $false
if ($strategyDir.Name -match "history") {
    $historyOnly = $true
}
if ($algorithmName -and $algorithmName.ToString().ToLower().Contains("history")) {
    $historyOnly = $true
}

$directHistoryProvider = "QuantConnect.AlpacaHistoryProvider.AlpacaHistoryProvider"
$proxyHistoryProvider = "QuantConnect.AlpacaHistoryProvider.AlpacaProxyHistoryProvider"
$historyModeRequested = if ($env:ALPACA_HISTORY_MODE) { $env:ALPACA_HISTORY_MODE.Trim().ToLowerInvariant() } else { "direct" }
if ($historyModeRequested -notin @("direct", "proxy")) {
    Write-Warning "Invalid ALPACA_HISTORY_MODE '$historyModeRequested'. Defaulting to 'direct'."
    $historyModeRequested = "direct"
}
$historyAutoFallback = Test-TruthyValue -Value $env:ALPACA_HISTORY_AUTO_FALLBACK -Default $true
$env:ALPACA_HISTORY_MODE = $historyModeRequested
$env:ALPACA_HISTORY_AUTO_FALLBACK = if ($historyAutoFallback) { "1" } else { "0" }

# History performance defaults (env override supported)
Set-EnvDefault -Name "ALPACA_HISTORY_CHUNK_CONCURRENCY" -Value "4"
Set-EnvDefault -Name "ALPACA_HISTORY_BATCH_SIZE" -Value "20"
Set-EnvDefault -Name "ALPACA_HISTORY_MAX_PAGES" -Value "120"
Set-EnvDefault -Name "ALPACA_HISTORY_MAX_PAGES_HARD_CAP" -Value "8000"
Set-EnvDefault -Name "ALPACA_HISTORY_HTTP_MAX_RETRIES" -Value "4"
Set-EnvDefault -Name "ALPACA_HISTORY_HTTP_BASE_DELAY_MS" -Value "200"
Set-EnvDefault -Name "ALPACA_HISTORY_HTTP_MAX_DELAY_MS" -Value "4000"
Set-EnvDefault -Name "ALPACA_HISTORY_HTTP_TIMEOUT_SECONDS" -Value "75"
Set-EnvDefault -Name "ALPACA_HISTORY_CACHE_SECONDS" -Value "1200"
Set-EnvDefault -Name "ALPACA_HISTORY_CACHE_MAX_SPAN_DAYS" -Value "7"
Set-EnvDefault -Name "ALPACA_HISTORY_CACHE_MAX_ENTRIES" -Value "2048"

$historyModeEffective = $historyModeRequested
$overrideHistoryProvider = $directHistoryProvider
if ($historyModeRequested -eq "direct") {
    if (-not $env:ALPACA_API_KEY) {
        $env:ALPACA_API_KEY = Read-Host "ALPACA_API_KEY"
    }
    if (-not $env:ALPACA_API_SECRET) {
        $env:ALPACA_API_SECRET = Read-Host "ALPACA_API_SECRET"
    }
    $preflightResult = Test-AlpacaDirectHistoryPreflight `
        -ApiKey $env:ALPACA_API_KEY `
        -ApiSecret $env:ALPACA_API_SECRET `
        -DataUrl $env:ALPACA_DATA_URL `
        -DataFeed $env:ALPACA_DATA_FEED
    if ($preflightResult.Success) {
        Write-Host "Alpaca direct history preflight passed ($($preflightResult.Bars) SPY bars)."
    }
    else {
        Write-Warning "Alpaca direct history preflight failed: $($preflightResult.Error)"
        if ($historyAutoFallback) {
            Write-Warning "Auto fallback enabled. Switching to proxy history mode."
            $historyModeEffective = "proxy"
        }
        else {
            Write-Error "Direct history preflight failed and ALPACA_HISTORY_AUTO_FALLBACK=0."
            exit 1
        }
    }
}
if ($historyModeEffective -eq "proxy") {
    $overrideHistoryProvider = $proxyHistoryProvider
}
$env:ALPACA_HISTORY_MODE_EFFECTIVE = $historyModeEffective
Write-Host "History mode requested=$historyModeRequested effective=$historyModeEffective fallback=$($env:ALPACA_HISTORY_AUTO_FALLBACK)"

$defaultLocalProxyUrl = if ($isCrypto) { "ws://host.docker.internal:8765/stream/crypto" } else { "ws://host.docker.internal:8765/stream" }
$defaultCloudProxyUrl = if ($isCrypto) { "ws://35.88.155.223:8767/stream/crypto" } else { "ws://35.88.155.223:8767/stream" }
$defaultLocalHistoryUrl = "http://host.docker.internal:8766/v1/history/bars"
$proxyImage = if ($env:ALPACA_PROXY_IMAGE) { $env:ALPACA_PROXY_IMAGE } else { "alpaca-proxy-agent:livefix" }
$proxyContainerName = if ($env:ALPACA_PROXY_CONTAINER) { $env:ALPACA_PROXY_CONTAINER } else { "alpaca-proxy-agent" }

if (-not $historyOnly) {
    $proxyModeEnv = if ($env:ALPACA_PROXY_MODE) { $env:ALPACA_PROXY_MODE.ToLowerInvariant() } else { $null }
    $proxyOptions = @("Direct (Local Enhanced Proxy)", "Cloud (Remote Streaming)")
    Write-Host ""
    Write-Host "Select Proxy Mode:"
    if ($proxyModeEnv) {
        Write-Host "Current ALPACA_PROXY_MODE is '$proxyModeEnv' (press Enter to keep)."
    }
    for ($i = 0; $i -lt $proxyOptions.Count; $i++) {
        Write-Host ("[{0}] {1}" -f ($i + 1), $proxyOptions[$i])
    }
    $defaultIndex = if ($proxyModeEnv -eq "direct") { 0 } elseif ($proxyModeEnv -eq "cloud") { 1 } elseif ($proxyModeEnv -eq "relay") { 1 } else { 1 }
    $defaultPrompt = $defaultIndex + 1
    $pmIndex = Read-Choice -Prompt "Enter number (default $defaultPrompt)" -MaxIndex $proxyOptions.Count
    if ($null -eq $pmIndex) { $pmIndex = $defaultIndex }
    $proxyModeEnv = if ($pmIndex -eq 0) { "direct" } else { "cloud" }
    $env:ALPACA_PROXY_MODE = $proxyModeEnv

    if ($proxyModeEnv -eq "cloud") {
        if (-not $env:ALPACA_PROXY_URL -or (Test-ProxyIsLocal -ProxyUrl $env:ALPACA_PROXY_URL)) {
            $env:ALPACA_PROXY_URL = $defaultCloudProxyUrl
        }
        if ($isCrypto) {
            $normalizedCryptoUrl = Get-CryptoStreamUrl -ProxyUrl $env:ALPACA_PROXY_URL -DefaultUrl $defaultCloudProxyUrl
            if ($normalizedCryptoUrl -ne $env:ALPACA_PROXY_URL) {
                Write-Warning "Crypto strategy selected: forcing ALPACA_PROXY_URL path to /stream/crypto."
                $env:ALPACA_PROXY_URL = $normalizedCryptoUrl
            }
        }
    }
    else {
        if (-not $env:ALPACA_PROXY_URL -or -not (Test-ProxyIsLocal -ProxyUrl $env:ALPACA_PROXY_URL)) {
            $env:ALPACA_PROXY_URL = $defaultLocalProxyUrl
        }
        if ($isCrypto) {
            $normalizedCryptoUrl = Get-CryptoStreamUrl -ProxyUrl $env:ALPACA_PROXY_URL -DefaultUrl $defaultLocalProxyUrl
            if ($normalizedCryptoUrl -ne $env:ALPACA_PROXY_URL) {
                Write-Warning "Crypto strategy selected: forcing ALPACA_PROXY_URL path to /stream/crypto."
                $env:ALPACA_PROXY_URL = $normalizedCryptoUrl
            }
        }
    }

    $proxyIsLocal = Test-ProxyIsLocal -ProxyUrl $env:ALPACA_PROXY_URL
    $historyIsLocal = $historyModeEffective -eq "proxy"
    if ($historyIsLocal) {
        $env:ALPACA_HISTORY_URL = $defaultLocalHistoryUrl
        $env:ALPACA_HISTORY_URL_FORCE = "1"
    }
    else {
        Remove-Item Env:ALPACA_HISTORY_URL -ErrorAction SilentlyContinue
        Remove-Item Env:ALPACA_HISTORY_URL_FORCE -ErrorAction SilentlyContinue
    }

    $effectiveHistoryTarget = if ($historyIsLocal) { $env:ALPACA_HISTORY_URL } else { "direct-rest" }
    Write-Host "Effective proxy endpoints: mode=$proxyModeEnv stream=$env:ALPACA_PROXY_URL history=$effectiveHistoryTarget"
    if ($proxyModeEnv -eq "cloud" -and $historyIsLocal) {
        Write-Host "Hybrid routing active: cloud stream + local proxy history."
    }

    if ($proxyModeEnv -eq "cloud" -or -not $proxyIsLocal) {
        if (-not $env:ALPACA_PROXY_TOKEN) {
            $env:ALPACA_PROXY_TOKEN = Read-Host "ALPACA_PROXY_TOKEN"
        }
        if (-not $env:ALPACA_PROXY_TOKEN) {
            Write-Error "ALPACA_PROXY_TOKEN is required for cloud/remote proxy mode."
            exit 1
        }
    }
    else {
        $env:ALPACA_PROXY_TOKEN = ""
    }

    $proxyPort = if ($historyIsLocal) { 8765 } else { Get-ProxyPort -ProxyUrl $env:ALPACA_PROXY_URL }
    $proxyMode = Get-ProxyMode -Brokerage $selectedBrokerage -DataFeed $env:ALPACA_DATA_FEED
    $shouldStartProxy = $proxyIsLocal -or $historyIsLocal
    if ($shouldStartProxy) {
        if (-not $env:ALPACA_API_KEY) {
            $env:ALPACA_API_KEY = Read-Host "ALPACA_API_KEY"
        }
        if (-not $env:ALPACA_API_SECRET) {
            $env:ALPACA_API_SECRET = Read-Host "ALPACA_API_SECRET"
        }
        Ensure-ProxyAgent -Name $proxyContainerName -Image $proxyImage -IsLive $proxyMode.IsLive -IsPro $proxyMode.IsPro -Port $proxyPort -Mode "direct" -ProxyToken $env:ALPACA_PROXY_TOKEN
        $proxyHttpPort = $proxyPort + 1
        if (-not (Wait-ProxyHealth -HttpPort $proxyHttpPort)) {
            Write-Warning "Proxy agent did not become healthy on port $proxyHttpPort. Restarting..."
            docker restart $proxyContainerName | Out-Null
            if (-not (Wait-ProxyHealth -HttpPort $proxyHttpPort -Retries 15 -DelayMs 750)) {
                Write-Warning "Proxy agent still not healthy on port $proxyHttpPort. Recreating container..."
                docker rm -f $proxyContainerName | Out-Null
                Ensure-ProxyAgent -Name $proxyContainerName -Image $proxyImage -IsLive $proxyMode.IsLive -IsPro $proxyMode.IsPro -Port $proxyPort -Mode "direct" -ProxyToken $env:ALPACA_PROXY_TOKEN
                if (-not (Wait-ProxyHealth -HttpPort $proxyHttpPort -Retries 15 -DelayMs 750)) {
                    Write-Error "Proxy agent still not healthy on port $proxyHttpPort. Aborting before Lean launch."
                    exit 1
                }
            }
        }
        if ($historyIsLocal) {
            $testScript = Join-Path $root "scripts\test_proxy_health.ps1"
            if (Test-Path $testScript) {
                if ($isCrypto) {
                    & $testScript -HistoryUrl $env:ALPACA_HISTORY_URL -CheckHistory -AllowZeroBars
                }
                else {
                    & $testScript -HistoryUrl $env:ALPACA_HISTORY_URL -CheckHistory
                }
                if ($LASTEXITCODE -ne 0) {
                    Write-Error "Proxy history test failed; aborting before Lean launch."
                    exit 1
                }
            }
        }
    }
    else {
        Write-Host "Skipping local proxy agent; using remote proxy at $env:ALPACA_PROXY_URL"
    }
}
else {
    Write-Host "History-only strategy detected; skipping stream proxy setup."
    if ($historyModeEffective -eq "proxy") {
        if (-not $env:ALPACA_API_KEY) {
            $env:ALPACA_API_KEY = Read-Host "ALPACA_API_KEY"
        }
        if (-not $env:ALPACA_API_SECRET) {
            $env:ALPACA_API_SECRET = Read-Host "ALPACA_API_SECRET"
        }
        $env:ALPACA_PROXY_MODE = "direct"
        $env:ALPACA_PROXY_URL = $defaultLocalProxyUrl
        $env:ALPACA_HISTORY_URL = $defaultLocalHistoryUrl
        $env:ALPACA_HISTORY_URL_FORCE = "1"
        $proxyMode = Get-ProxyMode -Brokerage $selectedBrokerage -DataFeed $env:ALPACA_DATA_FEED
        Ensure-ProxyAgent -Name $proxyContainerName -Image $proxyImage -IsLive $proxyMode.IsLive -IsPro $proxyMode.IsPro -Port 8765 -Mode "direct" -ProxyToken $env:ALPACA_PROXY_TOKEN
        if (-not (Wait-ProxyHealth -HttpPort 8766)) {
            Write-Error "Proxy agent not healthy on 8766 for history-only proxy mode."
            exit 1
        }
    }
    else {
        Remove-Item Env:ALPACA_HISTORY_URL -ErrorAction SilentlyContinue
        Remove-Item Env:ALPACA_HISTORY_URL_FORCE -ErrorAction SilentlyContinue
        if (-not $env:ALPACA_API_KEY) {
            $env:ALPACA_API_KEY = Read-Host "ALPACA_API_KEY"
        }
        if (-not $env:ALPACA_API_SECRET) {
            $env:ALPACA_API_SECRET = Read-Host "ALPACA_API_SECRET"
        }
    }
}

$timestamp = Get-Date -Format "MMddHHmmss"
$containerName = ("lean_{0}_{1}" -f $strategyDir.Name.ToLower(), $timestamp)
$containerName = $containerName -replace "[^a-z0-9_.-]", "_"

$sessionTimestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$resultsRoot = Join-Path $strategyDir.FullName "live"
if (-not (Test-Path $resultsRoot)) {
    New-Item -ItemType Directory -Path $resultsRoot | Out-Null
}
$sessionDir = Join-Path $resultsRoot $sessionTimestamp
if (-not (Test-Path $sessionDir)) {
    New-Item -ItemType Directory -Path $sessionDir | Out-Null
}

$resultsRelativeInStrategy = "live/$sessionTimestamp"
if ($isCrypto) {
    $useSingleSource = Test-TruthyValue -Value $env:CRYPTO_MONITOR_SINGLE_SOURCE -Default $true
    if ($useSingleSource) {
        $resultsRelativeInStrategy = "live/single_source"
        $singleSourceDir = Join-Path $strategyDir.FullName "live\single_source"
        if (-not (Test-Path $singleSourceDir)) {
            New-Item -ItemType Directory -Path $singleSourceDir | Out-Null
        }
        Write-Host "Crypto single-source output enabled at $singleSourceDir"
        $env:CRYPTO_MONITOR_SINGLE_SOURCE = "1"
    }
}

$sourceSnapshotDir = Join-Path $sessionDir "strategy_source"
Copy-StrategySource -SourceDir $strategyDir.FullName -DestinationDir $sourceSnapshotDir

$jobId = Get-Random -Minimum 1000000000 -Maximum 9999999999
$resultsPathInContainer = "/Lean/Launcher/algos/$($strategyDir.Name)/$($resultsRelativeInStrategy -replace '\\', '/')"
Set-JsonValue -Object $configJson -Name "results-destination-folder" -Value $resultsPathInContainer
Set-JsonValue -Object $configJson -Name "job-id" -Value $jobId
Set-JsonValue -Object $configJson -Name "algorithm-id" -Value "L-$jobId"
$envName = Get-JsonValue -Object $configJson -Name "environment"
$envs = Get-JsonValue -Object $configJson -Name "environments"
if ($envName -and $envs) {
    $envConfig = $null
    if ($envs -is [System.Collections.IDictionary]) {
        $envConfig = $envs[$envName]
    }
    else {
        $envConfig = $envs.$envName
    }
    if ($envConfig) {
        Set-JsonValue -Object $envConfig -Name "results-destination-folder" -Value $resultsPathInContainer
        if ($env:IB_USER_NAME) {
            Set-JsonValue -Object $envConfig -Name "ib-user-name" -Value $env:IB_USER_NAME
        }
        if ($env:IB_ACCOUNT) {
            Set-JsonValue -Object $envConfig -Name "ib-account" -Value $env:IB_ACCOUNT
        }
        if ($env:IB_PASSWORD) {
            Set-JsonValue -Object $envConfig -Name "ib-password" -Value $env:IB_PASSWORD
        }
    }
}

if ($overrideHistoryProvider) {
    Write-Host "Overriding History Provider to: $overrideHistoryProvider"
    Set-JsonValue -Object $configJson -Name "history-provider" -Value @($overrideHistoryProvider)
    if ($envConfig) {
        Set-JsonValue -Object $envConfig -Name "history-provider" -Value @($overrideHistoryProvider)
        Write-Host "Applied History Provider override to environment '$envName'."
    }
}

$tempConfigPath = Join-Path $sessionDir "config.json"
$configJson | ConvertTo-Json -Depth 50 | Set-Content -Path $tempConfigPath -Encoding UTF8

$sessionMeta = [ordered]@{
    id        = $jobId
    container = $containerName
    brokerage = $selectedBrokerage
}
$sessionMeta | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $sessionDir "config") -Encoding UTF8

$modulesPath = Join-Path $root "modules\ibkr"
$storagePath = Join-Path $root "storage"
$projectRoot = Split-Path $root -Parent
$dataPath = Join-Path $projectRoot "data"
if (-not (Test-Path $storagePath)) {
    New-Item -ItemType Directory -Path $storagePath | Out-Null
}
if (-not (Test-Path $dataPath)) {
    Write-Warning "Local data folder not found at $dataPath; history will fall back to remote providers."
}
$engineOverrideDllCandidate = Join-Path $modulesPath "QuantConnect.Lean.Engine.dll"
$leanEngineOverrideRequested = Test-TruthyValue -Value $env:LEAN_ENGINE_OVERRIDE -Default $false
$leanEngineOverrideEnabled = $leanEngineOverrideRequested
if ($leanEngineOverrideEnabled -and -not (Test-Path $engineOverrideDllCandidate)) {
    Write-Warning "LEAN_ENGINE_OVERRIDE=1 but missing $engineOverrideDllCandidate; continuing without engine override."
    $leanEngineOverrideEnabled = $false
}
$engineOverrideDepsCandidate = Join-Path $modulesPath "QuantConnect.Lean.Engine.deps.json"
$forceIncompatibleOverride = Test-TruthyValue -Value $env:LEAN_ENGINE_OVERRIDE_FORCE -Default $false
if ($leanEngineOverrideEnabled) {
    $runtimeTfMajor = Get-DotNetRuntimeMajor -Image $Image
    $engineTfMajor = Get-EngineDepsTargetMajor -DepsPath $engineOverrideDepsCandidate
    if (
        $runtimeTfMajor -and
        $engineTfMajor -and
        ($engineTfMajor -gt $runtimeTfMajor) -and
        (-not $forceIncompatibleOverride)
    ) {
        $runtimeMismatchWarning = (
            "Engine override target net{0}.0 is newer than container runtime net{1}.0. " +
            "Disabling override to avoid startup crash. Set LEAN_ENGINE_OVERRIDE_FORCE=1 to force."
        ) -f $engineTfMajor, $runtimeTfMajor
        Write-Warning $runtimeMismatchWarning
        $leanEngineOverrideEnabled = $false
    }
}
$env:LEAN_ENGINE_OVERRIDE = if ($leanEngineOverrideEnabled) { "1" } else { "0" }
$dockerNoTty = Test-TruthyValue -Value $env:DOCKER_NO_TTY -Default $false

$dockerArgs = @(
    "run",
    "--name", $containerName,
    "--rm",
    "-e", "DOTNET_ROLL_FORWARD=LatestMajor",
    "-e", "ALPACA_API_KEY=$env:ALPACA_API_KEY",
    "-e", "ALPACA_API_SECRET=$env:ALPACA_API_SECRET",
    "-v", "$($tempConfigPath):/Lean/Launcher/config.json",
    "-v", "$($tempConfigPath):/Lean/Launcher/bin/Debug/config.json",
    "-v", "${root}:/Lean/Launcher/algos",
    "-v", "${dataPath}:/Lean/Data",
    "-v", "${modulesPath}:/Lean/Launcher/modules",
    "-v", "${storagePath}:/Lean/Launcher/bin/Debug/storage"
)
if (-not $dockerNoTty) {
    $dockerArgs += "-it"
}
if ($env:ALPACA_DATA_FEED) {
    $dockerArgs += @("-e", "ALPACA_DATA_FEED=$env:ALPACA_DATA_FEED")
}
if ($env:ALPACA_DATA_URL) {
    $dockerArgs += @("-e", "ALPACA_DATA_URL=$env:ALPACA_DATA_URL")
}
if ($env:ALPACA_HISTORY_MODE) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_MODE=$env:ALPACA_HISTORY_MODE")
}
if ($env:ALPACA_HISTORY_MODE_EFFECTIVE) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_MODE_EFFECTIVE=$env:ALPACA_HISTORY_MODE_EFFECTIVE")
}
if ($env:ALPACA_HISTORY_AUTO_FALLBACK) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_AUTO_FALLBACK=$env:ALPACA_HISTORY_AUTO_FALLBACK")
}
if ($env:ALPACA_HISTORY_CHUNK_CONCURRENCY) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_CHUNK_CONCURRENCY=$env:ALPACA_HISTORY_CHUNK_CONCURRENCY")
}
if ($env:ALPACA_HISTORY_HTTP_MAX_RETRIES) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_HTTP_MAX_RETRIES=$env:ALPACA_HISTORY_HTTP_MAX_RETRIES")
}
if ($env:ALPACA_HISTORY_HTTP_BASE_DELAY_MS) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_HTTP_BASE_DELAY_MS=$env:ALPACA_HISTORY_HTTP_BASE_DELAY_MS")
}
if ($env:ALPACA_HISTORY_HTTP_MAX_DELAY_MS) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_HTTP_MAX_DELAY_MS=$env:ALPACA_HISTORY_HTTP_MAX_DELAY_MS")
}
if ($env:ALPACA_HISTORY_HTTP_TIMEOUT_SECONDS) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_HTTP_TIMEOUT_SECONDS=$env:ALPACA_HISTORY_HTTP_TIMEOUT_SECONDS")
}
if ($env:ALPACA_HISTORY_CACHE_SECONDS) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_CACHE_SECONDS=$env:ALPACA_HISTORY_CACHE_SECONDS")
}
if ($env:ALPACA_HISTORY_CACHE_MAX_SPAN_DAYS) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_CACHE_MAX_SPAN_DAYS=$env:ALPACA_HISTORY_CACHE_MAX_SPAN_DAYS")
}
if ($env:ALPACA_HISTORY_CACHE_MAX_ENTRIES) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_CACHE_MAX_ENTRIES=$env:ALPACA_HISTORY_CACHE_MAX_ENTRIES")
}
if ($env:ALPACA_HISTORY_BATCH_SIZE) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_BATCH_SIZE=$env:ALPACA_HISTORY_BATCH_SIZE")
}
if ($env:ALPACA_HISTORY_MAX_PAGES) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_MAX_PAGES=$env:ALPACA_HISTORY_MAX_PAGES")
}
if ($env:ALPACA_HISTORY_LIMIT) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_LIMIT=$env:ALPACA_HISTORY_LIMIT")
}
if ($env:ALPACA_HISTORY_MAX_PAGES_HARD_CAP) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_MAX_PAGES_HARD_CAP=$env:ALPACA_HISTORY_MAX_PAGES_HARD_CAP")
}
if ($env:IB_USER_NAME) {
    $dockerArgs += @("-e", "IB_USER_NAME=$env:IB_USER_NAME")
}
if ($env:IB_ACCOUNT) {
    $dockerArgs += @("-e", "IB_ACCOUNT=$env:IB_ACCOUNT")
}
if ($env:IB_PASSWORD) {
    $dockerArgs += @("-e", "IB_PASSWORD=$env:IB_PASSWORD")
}
if ($leanLocalNoAuth) {
    $dockerArgs += @("-e", "LEAN_LOCAL_NOAUTH=$env:LEAN_LOCAL_NOAUTH")
}
if (-not $historyOnly) {
    if ($env:ALPACA_PROXY_URL) {
        $dockerArgs += @("-e", "ALPACA_PROXY_URL=$env:ALPACA_PROXY_URL")
    }
    if ($env:ALPACA_PROXY_TOKEN) {
        $dockerArgs += @("-e", "ALPACA_PROXY_TOKEN=$env:ALPACA_PROXY_TOKEN")
    }
    if ($isCrypto -and $env:ALPACA_CRYPTO_REST_URL) {
        $dockerArgs += @("-e", "ALPACA_CRYPTO_REST_URL=$env:ALPACA_CRYPTO_REST_URL")
    }
    if ($isCrypto) {
        $env:CRYPTO_MONITOR_RUN_ID = $sessionTimestamp
        $dockerArgs += @("-e", "CRYPTO_MONITOR_RUN_ID=$env:CRYPTO_MONITOR_RUN_ID")
        if ($env:CRYPTO_MONITOR_SINGLE_SOURCE) {
            $dockerArgs += @("-e", "CRYPTO_MONITOR_SINGLE_SOURCE=$env:CRYPTO_MONITOR_SINGLE_SOURCE")
        }
    }
}
if ($env:ALPACA_HISTORY_URL) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_URL=$env:ALPACA_HISTORY_URL")
}

$alpacaHistoryDll = Join-Path $modulesPath "Alpaca.History.Provider.dll"
$alpacaHistoryDeps = Join-Path $modulesPath "Alpaca.History.Provider.deps.json"
$alpacaHistoryPdb = Join-Path $modulesPath "Alpaca.History.Provider.pdb"
$alpacaProxyDll = Join-Path $modulesPath "QuantConnect.AlpacaProxy.dll"
$alpacaProxyDeps = Join-Path $modulesPath "QuantConnect.AlpacaProxy.deps.json"
$alpacaProxyPdb = Join-Path $modulesPath "QuantConnect.AlpacaProxy.pdb"
$proxyDependencyFiles = @(
    "MessagePack.dll",
    "MessagePack.Annotations.dll",
    "Microsoft.NET.StringTools.dll"
)

if (Test-Path $alpacaHistoryDll) {
    $dockerArgs += @("-v", "${alpacaHistoryDll}:/Lean/Launcher/bin/Debug/Alpaca.History.Provider.dll")
    $dockerArgs += @("-v", "${alpacaHistoryDll}:/Lean/Launcher/modules/Alpaca.History.Provider.dll")
    # Override official assembly name to avoid loading the old implementation
    $dockerArgs += @("-v", "${alpacaHistoryDll}:/Lean/Launcher/bin/Debug/QuantConnect.AlpacaHistoryProvider.dll")
    $dockerArgs += @("-v", "${alpacaHistoryDll}:/Lean/Launcher/modules/QuantConnect.AlpacaHistoryProvider.dll")
}
if (Test-Path $alpacaHistoryDeps) {
    $dockerArgs += @("-v", "${alpacaHistoryDeps}:/Lean/Launcher/bin/Debug/Alpaca.History.Provider.deps.json")
    $dockerArgs += @("-v", "${alpacaHistoryDeps}:/Lean/Launcher/modules/Alpaca.History.Provider.deps.json")
}
if (Test-Path $alpacaHistoryPdb) {
    $dockerArgs += @("-v", "${alpacaHistoryPdb}:/Lean/Launcher/bin/Debug/Alpaca.History.Provider.pdb")
    $dockerArgs += @("-v", "${alpacaHistoryPdb}:/Lean/Launcher/modules/Alpaca.History.Provider.pdb")
}
if (Test-Path $alpacaProxyDll) {
    $dockerArgs += @("-v", "${alpacaProxyDll}:/Lean/Launcher/bin/Debug/QuantConnect.AlpacaProxy.dll")
    $dockerArgs += @("-v", "${alpacaProxyDll}:/Lean/Launcher/modules/QuantConnect.AlpacaProxy.dll")
}
if (Test-Path $alpacaProxyDeps) {
    $dockerArgs += @("-v", "${alpacaProxyDeps}:/Lean/Launcher/bin/Debug/QuantConnect.AlpacaProxy.deps.json")
    $dockerArgs += @("-v", "${alpacaProxyDeps}:/Lean/Launcher/modules/QuantConnect.AlpacaProxy.deps.json")
}
if (Test-Path $alpacaProxyPdb) {
    $dockerArgs += @("-v", "${alpacaProxyPdb}:/Lean/Launcher/bin/Debug/QuantConnect.AlpacaProxy.pdb")
    $dockerArgs += @("-v", "${alpacaProxyPdb}:/Lean/Launcher/modules/QuantConnect.AlpacaProxy.pdb")
}
foreach ($dep in $proxyDependencyFiles) {
    $depPath = Join-Path $modulesPath $dep
    if (Test-Path $depPath) {
        $dockerArgs += @("-v", "${depPath}:/Lean/Launcher/bin/Debug/$dep")
        $dockerArgs += @("-v", "${depPath}:/Lean/Launcher/modules/$dep")
    }
}

$alpacaBrokerageOverrideFiles = @(
    "QuantConnect.Brokerages.Alpaca.dll",
    "QuantConnect.Brokerages.Alpaca.deps.json",
    "QuantConnect.Brokerages.Alpaca.pdb",
    "Alpaca.Markets.dll",
    "Alpaca.Markets.pdb"
)
foreach ($overrideName in $alpacaBrokerageOverrideFiles) {
    $overridePath = Join-Path $modulesPath $overrideName
    if (Test-Path $overridePath) {
        $dockerArgs += @("-v", "${overridePath}:/Lean/Launcher/bin/Debug/$overrideName")
        $dockerArgs += @("-v", "${overridePath}:/Lean/Launcher/modules/$overrideName")
    }
}

if ($leanLocalNoAuth) {
    $apiOverrideFiles = @(
        "QuantConnect.Api.dll",
        "QuantConnect.Api.deps.json",
        "QuantConnect.Api.pdb"
    )
    foreach ($overrideName in $apiOverrideFiles) {
        $overridePath = Join-Path $modulesPath $overrideName
        if (Test-Path $overridePath) {
            $dockerArgs += @("-v", "${overridePath}:/Lean/Launcher/bin/Debug/$overrideName")
        }
    }
}

if ($leanEngineOverrideEnabled) {
    $engineOverrideDll = $engineOverrideDllCandidate
    $engineOverridePdb = Join-Path $modulesPath "QuantConnect.Lean.Engine.pdb"
    $engineOverrideDeps = Join-Path $modulesPath "QuantConnect.Lean.Engine.deps.json"
    if (Test-Path $engineOverrideDll) {
        $dockerArgs += @("-v", "${engineOverrideDll}:/Lean/Launcher/bin/Debug/QuantConnect.Lean.Engine.dll")
        if (Test-Path $engineOverridePdb) {
            $dockerArgs += @("-v", "${engineOverridePdb}:/Lean/Launcher/bin/Debug/QuantConnect.Lean.Engine.pdb")
        }
        if (Test-Path $engineOverrideDeps) {
            $dockerArgs += @("-v", "${engineOverrideDeps}:/Lean/Launcher/bin/Debug/QuantConnect.Lean.Engine.deps.json")
        }
        Write-Host "LEAN_ENGINE_OVERRIDE=1 -> mounting QuantConnect.Lean.Engine.dll override."
    }
}

$dockerArgs += $Image

Write-Host ""
$safeDockerArgString = Get-RedactedDockerArgumentString -InputArgs $dockerArgs
Write-Host "Running: docker $safeDockerArgString"

docker @dockerArgs
$dockerExitCode = $LASTEXITCODE
if ($dockerExitCode -ne 0 -and (Test-Path $sourceSnapshotDir)) {
    Remove-Item -Recurse -Force $sourceSnapshotDir
}
