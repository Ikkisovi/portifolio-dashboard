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
            # Escape embedded quotes/backslashes for CreateProcess command-line parsing.
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
        $port = if ($uri.Port -eq 8765) { 8766 } else { $uri.Port }
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

function Ensure-ProxyAgent {
    param(
        [string]$Name,
        [string]$Image,
        [bool]$IsLive,
        [bool]$IsPro,
        [int]$Port,
        [string]$Mode = "direct",
        [string]$CloudProxyUrl,
        [string]$CloudHistoryUrl,
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
            $answer = Read-Host "Proxy agent $Name is running MODE=$currentMode but $Mode requested. Restart? (Y/n)"
            if ($answer -match '^[Nn]') {
                Write-Host "Keeping existing proxy agent. Mode mismatch may cause errors."
                return
            }
            docker rm -f $Name | Out-Null
        }
        else {
            Write-Host "Proxy agent $Name is already running; skipping start."
            return
        }
    }

    $runningOnPort = docker ps --filter "publish=$Port" --format "{{.Names}}"
    if ($runningOnPort) {
        Write-Host "Port ${Port} is already in use by: $runningOnPort. Skipping proxy start."
        return
    }

    $existing = docker ps -a --filter "name=^${Name}$" --format "{{.ID}}"
    if ($existing) {
        Write-Host "Starting existing proxy agent container $Name..."
        docker start $Name | Out-Null
        return
    }

    $envLive = if ($IsLive) { "true" } else { "false" }
    $envPro = if ($IsPro) { "true" } else { "false" }
    Write-Host "Starting proxy agent $Name ($Image) IS_LIVE=$envLive IS_PRO=$envPro..."
    $httpPort = $Port + 1
    $runArgs = @(
        "run",
        "-d",
        "--name", $Name,
        "-p", "${Port}:8765",
        "-p", "${httpPort}:8766",
        "-e", "IS_LIVE=$envLive",
        "-e", "IS_PRO=$envPro",
        "-e", "MODE=$Mode"
    )
    if ($CloudProxyUrl) {
        $runArgs += @("-e", "CLOUD_PROXY_URL=$CloudProxyUrl")
    }
    if ($CloudHistoryUrl) {
        $runArgs += @("-e", "CLOUD_HISTORY_URL=$CloudHistoryUrl")
    }
    if ($ProxyToken) {
        $runArgs += @("-e", "ALPACA_PROXY_TOKEN=$ProxyToken")
    }
    $runArgs += $Image
    docker @runArgs | Out-Null
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

function Test-DockerDaemonAvailable {
    try {
        $null = docker version --format "{{.Server.Version}}" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
    }
    catch {}
    return $false
}

function Ensure-DockerDaemonReady {
    param([int]$StartupTimeoutSeconds = 90)

    if (Test-DockerDaemonAvailable) {
        return $true
    }

    Write-Warning "Docker daemon is not reachable via current context."
    $contextName = $null
    try {
        $contextName = (docker context show 2>$null)
    }
    catch {}
    if (-not [string]::IsNullOrWhiteSpace($contextName)) {
        Write-Host "Current docker context: $contextName"
    }

    $dockerDesktopExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerDesktopExe) {
        Write-Host "Attempting to start Docker Desktop..."
        try {
            Start-Process -FilePath $dockerDesktopExe | Out-Null
        }
        catch {}
    }

    $waitSeconds = [Math]::Max(5, $StartupTimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (Test-DockerDaemonAvailable) {
            return $true
        }
    }

    return $false
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

function Remove-JsonValue {
    param(
        [object]$Object,
        [string]$Name
    )
    if ($null -eq $Object) {
        return
    }
    if ($Object -is [System.Collections.IDictionary]) {
        $Object.Remove($Name) | Out-Null
        return
    }
    if ($Object.PSObject.Properties[$Name]) {
        $Object.PSObject.Properties.Remove($Name)
    }
}

function Read-Date {
    param([string]$Prompt)
    while ($true) {
        $value = Read-Host $Prompt
        if (-not $value) {
            Write-Host "Date is required (YYYY-MM-DD)."
            continue
        }
        $formats = @("yyyy-MM-dd", "yyyyMMdd")
        foreach ($format in $formats) {
            try {
                return [DateTime]::ParseExact(
                    $value,
                    $format,
                    [System.Globalization.CultureInfo]::InvariantCulture
                ).Date
            }
            catch {
            }
        }
        Write-Host "Invalid date. Use YYYY-MM-DD or YYYYMMDD."
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
$configFiles = Get-ChildItem -Path $strategyDir.FullName -Filter "launcher_config*.json" | Sort-Object Name
if (-not $configFiles) {
    Write-Error "No launcher_config*.json found in $($strategyDir.FullName)."
    exit 1
}

$configFile = $null
if ($configFiles.Count -eq 1) {
    $configFile = $configFiles[0]
}
else {
    Write-Host ""
    Write-Host "Select config:"
    for ($i = 0; $i -lt $configFiles.Count; $i++) {
        Write-Host ("[{0}] {1}" -f ($i + 1), $configFiles[$i].Name)
    }
    $configIndex = Read-Choice -Prompt "Enter number" -MaxIndex $configFiles.Count
    if ($null -eq $configIndex) {
        Write-Error "Invalid selection."
        exit 1
    }
    $configFile = $configFiles[$configIndex]
}

Write-Host ""
$startDate = Read-Date -Prompt "Backtest start date (YYYY-MM-DD)"
$endDate = Read-Date -Prompt "Backtest end date (YYYY-MM-DD)"
if ($endDate -lt $startDate) {
    Write-Error "End date must be >= start date."
    exit 1
}

Write-Host ""
$skipExisting = $true
$skipAnswer = Read-Host "Skip already downloaded data? (Y/n)"
if ($skipAnswer -match '^[Nn]') {
    $skipExisting = $false
}

$downloadQuotes = $true
$quoteAnswer = Read-Host "Download quote data (minute)? (Y/n)"
if ($quoteAnswer -match '^[Nn]') {
    $downloadQuotes = $false
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
$brokerageConfig = Get-JsonValue -Object $configJson -Name "brokerage"
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
if (-not $ibUserName) { $ibUserName = Get-JsonValue -Object $configJson -Name "ib-user-name" }
if (-not $ibUserName -and $rootLauncherConfig) { $ibUserName = Get-JsonValue -Object $rootLauncherConfig -Name "ib-user-name" }
$ibAccount = $env:IB_ACCOUNT
if (-not $ibAccount) { $ibAccount = Get-JsonValue -Object $configJson -Name "ib-account" }
if (-not $ibAccount -and $rootLauncherConfig) { $ibAccount = Get-JsonValue -Object $rootLauncherConfig -Name "ib-account" }
$ibPassword = $env:IB_PASSWORD
if (-not $ibPassword) { $ibPassword = Get-JsonValue -Object $configJson -Name "ib-password" }
if (-not $ibPassword -and $rootLauncherConfig) { $ibPassword = Get-JsonValue -Object $rootLauncherConfig -Name "ib-password" }
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

if (-not $env:ALPACA_API_KEY) {
    $env:ALPACA_API_KEY = Read-Host "ALPACA_API_KEY"
}
if (-not $env:ALPACA_API_SECRET) {
    $env:ALPACA_API_SECRET = Read-Host "ALPACA_API_SECRET"
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

if (-not (Ensure-DockerDaemonReady)) {
    Write-Error "Docker daemon unavailable. Start Docker Desktop and rerun."
    exit 1
}

if ($historyModeEffective -eq "proxy") {
    if (-not $env:ALPACA_PROXY_URL) {
        $env:ALPACA_PROXY_URL = "ws://host.docker.internal:8765/stream"
    }
    $env:ALPACA_HISTORY_URL = "http://host.docker.internal:8766/v1/history/bars"
    $env:ALPACA_HISTORY_URL_FORCE = "1"
    $env:ALPACA_PROXY_MODE = "direct"
    $proxyImage = if ($env:ALPACA_PROXY_IMAGE) { $env:ALPACA_PROXY_IMAGE } else { "alpaca-proxy-agent:livefix" }
    $proxyContainerName = if ($env:ALPACA_PROXY_CONTAINER) { $env:ALPACA_PROXY_CONTAINER } else { "alpaca-proxy-agent" }
    $proxyPort = Get-ProxyPort -ProxyUrl $env:ALPACA_PROXY_URL
    if ($proxyPort -le 0) {
        $proxyPort = 8765
    }
    $proxyMode = Get-ProxyMode -Brokerage $brokerageConfig -DataFeed $env:ALPACA_DATA_FEED
    Ensure-ProxyAgent -Name $proxyContainerName -Image $proxyImage -IsLive $proxyMode.IsLive -IsPro $proxyMode.IsPro -Port $proxyPort -Mode "direct" -CloudProxyUrl $null -CloudHistoryUrl $null -ProxyToken $env:ALPACA_PROXY_TOKEN
}
else {
    Remove-Item Env:ALPACA_HISTORY_URL -ErrorAction SilentlyContinue
    Remove-Item Env:ALPACA_HISTORY_URL_FORCE -ErrorAction SilentlyContinue
    Write-Host "Using direct Alpaca history provider; no local history proxy startup required."
}

$downloadScript = Join-Path $root "alpaca_data_download.py"
if (-not (Test-Path $downloadScript)) {
    Write-Error "Missing alpaca_data_download.py at $downloadScript"
    exit 1
}

$projectRoot = Split-Path $root -Parent
$dataPath = Join-Path $projectRoot "data"
if (-not (Test-Path $dataPath)) {
    New-Item -ItemType Directory -Path $dataPath | Out-Null
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$pythonArgs = @()
if (-not $pythonCmd) {
    $pythonCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $pythonArgs += "-3"
    }
}
if (-not $pythonCmd) {
    Write-Error "Python not found. Install Python or ensure it is on PATH."
    exit 1
}

$startStr = $startDate.ToString("yyyy-MM-dd")
$endStr = $endDate.ToString("yyyy-MM-dd")

Write-Host ""
Write-Host "Downloading data to $dataPath..."
$pythonArgs += @(
    $downloadScript,
    "--strategy-dir", $strategyDir.FullName,
    "--start", $startStr,
    "--end", $endStr,
    "--data-dir", $dataPath
)
if ($env:ALPACA_DATA_FEED) {
    $pythonArgs += @("--feed", $env:ALPACA_DATA_FEED)
}
if ($downloadQuotes) {
    $pythonArgs += "--quotes"
}
if ($skipExisting) {
    $pythonArgs += "--skip-existing"
}
& $pythonCmd @pythonArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Data download failed."
    exit $LASTEXITCODE
}

$timestamp = Get-Date -Format "MMddHHmmss"
$containerName = ("lean_bt_{0}_{1}" -f $strategyDir.Name.ToLower(), $timestamp)
$containerName = $containerName -replace "[^a-z0-9_.-]", "_"

$sessionTimestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$resultsRoot = Join-Path $strategyDir.FullName "backtests"
if (-not (Test-Path $resultsRoot)) {
    New-Item -ItemType Directory -Path $resultsRoot | Out-Null
}
$sessionDir = Join-Path $resultsRoot $sessionTimestamp
if (-not (Test-Path $sessionDir)) {
    New-Item -ItemType Directory -Path $sessionDir | Out-Null
}

$sourceSnapshotDir = Join-Path $sessionDir "strategy_source"
Copy-StrategySource -SourceDir $strategyDir.FullName -DestinationDir $sourceSnapshotDir

$jobId = Get-Random -Minimum 1000000000 -Maximum 9999999999
$resultsPathInContainer = "/Lean/Launcher/algos/$($strategyDir.Name)/backtests/$sessionTimestamp"

Set-JsonValue -Object $configJson -Name "live-mode" -Value $false
Set-JsonValue -Object $configJson -Name "brokerage" -Value "BacktestingBrokerage"
Remove-JsonValue -Object $configJson -Name "live-mode-brokerage"
Remove-JsonValue -Object $configJson -Name "data-queue-handler"
Remove-JsonValue -Object $configJson -Name "environment"
Remove-JsonValue -Object $configJson -Name "environments"

Set-JsonValue -Object $configJson -Name "setup-handler" -Value "QuantConnect.Lean.Engine.Setup.BacktestingSetupHandler"
Set-JsonValue -Object $configJson -Name "result-handler" -Value "QuantConnect.Lean.Engine.Results.BacktestingResultHandler"
Set-JsonValue -Object $configJson -Name "data-feed-handler" -Value "QuantConnect.Lean.Engine.DataFeeds.FileSystemDataFeed"
Set-JsonValue -Object $configJson -Name "real-time-handler" -Value "QuantConnect.Lean.Engine.RealTime.BacktestingRealTimeHandler"
Set-JsonValue -Object $configJson -Name "transaction-handler" -Value "QuantConnect.Lean.Engine.TransactionHandlers.BacktestingTransactionHandler"
if ($overrideHistoryProvider) {
    Write-Host "Overriding History Provider to: $overrideHistoryProvider"
    Set-JsonValue -Object $configJson -Name "history-provider" -Value @($overrideHistoryProvider)
}

Set-JsonValue -Object $configJson -Name "start-date" -Value $startStr
Set-JsonValue -Object $configJson -Name "end-date" -Value $endStr
Set-JsonValue -Object $configJson -Name "results-destination-folder" -Value $resultsPathInContainer
Normalize-OptionalIntConfig -Object $configJson -Name "job-user-id"
Normalize-OptionalIntConfig -Object $configJson -Name "project-id"
if ($leanLocalNoAuth) {
    Normalize-OptionalStringConfig -Object $configJson -Name "api-access-token"
    Normalize-OptionalStringConfig -Object $configJson -Name "job-organization-id"
    Write-Host "QC auth skipped: LEAN_LOCAL_NOAUTH=1"
}
Set-JsonValue -Object $configJson -Name "job-id" -Value $jobId
Set-JsonValue -Object $configJson -Name "algorithm-id" -Value "B-$jobId"

$parameters = Get-JsonValue -Object $configJson -Name "parameters"
if (-not $parameters) {
    $parameters = @{}
}
Set-JsonValue -Object $parameters -Name "backtest_start" -Value $startStr
Set-JsonValue -Object $parameters -Name "backtest_end" -Value $endStr
Set-JsonValue -Object $configJson -Name "parameters" -Value $parameters

$maxRuntimeMinutes = 15
$maxRuntimeParam = $env:BACKTEST_MAX_RUNTIME_MINUTES
try {
    if ($parameters -and $parameters.PSObject.Properties.Name -contains "backtest_max_runtime_minutes") {
        $maxRuntimeParam = [string]$parameters.backtest_max_runtime_minutes
    }
}
catch {}
if (-not [string]::IsNullOrWhiteSpace($maxRuntimeParam)) {
    try {
        $parsedRuntime = [int]$maxRuntimeParam
        if ($parsedRuntime -gt 0) {
            $maxRuntimeMinutes = $parsedRuntime
        }
    }
    catch {}
}
$runtimeHours = [int][Math]::Floor($maxRuntimeMinutes / 60)
$runtimeMins = $maxRuntimeMinutes % 60
$maxRuntimeValue = ("0.{0:00}:{1:00}:00" -f $runtimeHours, $runtimeMins)
Set-JsonValue -Object $configJson -Name "maximum-runtime" -Value $maxRuntimeValue
Write-Host "Backtest maximum-runtime set to $maxRuntimeValue"

$tempConfigPath = Join-Path $sessionDir "config.json"
$configJson | ConvertTo-Json -Depth 50 | Set-Content -Path $tempConfigPath -Encoding UTF8

$sessionMeta = [ordered]@{
    id        = $jobId
    container = $containerName
    strategy  = $strategyDir.Name
}
$sessionMeta | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $sessionDir "config") -Encoding UTF8

$modulesPath = Join-Path $root "modules\\ibkr"
$storagePath = Join-Path $root "storage"
if (-not (Test-Path $storagePath)) {
    New-Item -ItemType Directory -Path $storagePath | Out-Null
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
    "-e", "DOTNET_ROLL_FORWARD=LatestMajor"
)
if (-not $dockerNoTty) {
    $dockerArgs += "-it"
}
if ($env:ALPACA_API_KEY) {
    $dockerArgs += @("-e", "ALPACA_API_KEY=$env:ALPACA_API_KEY")
}
if ($env:ALPACA_API_SECRET) {
    $dockerArgs += @("-e", "ALPACA_API_SECRET=$env:ALPACA_API_SECRET")
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
if ($env:ALPACA_PROXY_TOKEN) {
    $dockerArgs += @("-e", "ALPACA_PROXY_TOKEN=$env:ALPACA_PROXY_TOKEN")
}
if ($env:ALPACA_PROXY_URL) {
    $dockerArgs += @("-e", "ALPACA_PROXY_URL=$env:ALPACA_PROXY_URL")
}
if ($env:ALPACA_HISTORY_URL) {
    $dockerArgs += @("-e", "ALPACA_HISTORY_URL=$env:ALPACA_HISTORY_URL")
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
$dockerArgs += @(
    "-v", "$($tempConfigPath):/Lean/Launcher/config.json",
    "-v", "$($tempConfigPath):/Lean/Launcher/bin/Debug/config.json",
    "-v", "${root}:/Lean/Launcher/algos",
    "-v", "${dataPath}:/Lean/Data",
    "-v", "${storagePath}:/Lean/Launcher/bin/Debug/storage",
    "-v", "${modulesPath}:/Lean/Launcher/modules"
)

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
$dockerArgString = ConvertTo-ProcessArgumentString -InputArgs $dockerArgs
$safeDockerArgString = Get-RedactedDockerArgumentString -InputArgs $dockerArgs
Write-Host "Running: docker $safeDockerArgString"
$dockerProcess = Start-Process -FilePath "docker" -ArgumentList $dockerArgString -NoNewWindow -PassThru
$timeoutMs = [int]([Math]::Max(1, $maxRuntimeMinutes) * 60 * 1000)
$timedOut = -not $dockerProcess.WaitForExit($timeoutMs)
if ($timedOut) {
    Write-Warning "Backtest exceeded ${maxRuntimeMinutes} minutes. Force stopping container $containerName ..."
    try {
        docker rm -f $containerName | Out-Null
    }
    catch {}
    try {
        if (-not $dockerProcess.HasExited) {
            $dockerProcess.Kill()
        }
    }
    catch {}
    $dockerExitCode = 124
}
else {
    $dockerExitCode = $dockerProcess.ExitCode
}

if ($dockerExitCode -ne 0 -and (Test-Path $sourceSnapshotDir)) {
    Remove-Item -Recurse -Force $sourceSnapshotDir
}
