from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_run_strategy_engine_override_is_explicit_and_minimal():
    text = _read("run_strategy.ps1")
    assert '[string]$Image = "lean-alpaca-proxy:net10"' in text
    assert '[string]$StrategyRepoRoot = ""' in text
    assert '$engineOverrideDllCandidate = Join-Path $modulesPath "QuantConnect.Lean.Engine.dll"' in text
    assert '$leanEngineOverrideRequested = Test-TruthyValue -Value $env:LEAN_ENGINE_OVERRIDE -Default $false' in text
    assert "$leanEngineOverrideEnabled = $leanEngineOverrideRequested" in text
    assert "function Get-DotNetRuntimeMajor" in text
    assert "function Get-EngineDepsTargetMajor" in text
    assert "$maxMajor = $null" in text
    assert "return $maxMajor" in text
    assert '"DOTNET_ROLL_FORWARD=LatestMajor"' in text
    assert '$dockerArgs += @("-v", "${engineOverrideDll}:/Lean/Launcher/bin/Debug/QuantConnect.Lean.Engine.dll")' in text
    assert '$overrideFiles = Get-ChildItem -Path $modulesPath -File -ErrorAction SilentlyContinue' not in text
    assert "Python.Runtime.dll" not in text
    assert "CSharpAPI.dll" not in text
    assert "Write-Warning $runtimeMismatchWarning" in text
    assert "Write-Warning (" not in text


def test_run_backtest_engine_override_is_explicit_and_minimal():
    text = _read("run_backtest.ps1")
    assert '[string]$Image = "lean-alpaca-proxy:net10"' in text
    assert '[string]$StrategyRepoRoot = ""' in text
    assert '$engineOverrideDllCandidate = Join-Path $modulesPath "QuantConnect.Lean.Engine.dll"' in text
    assert '$leanEngineOverrideRequested = Test-TruthyValue -Value $env:LEAN_ENGINE_OVERRIDE -Default $false' in text
    assert "$leanEngineOverrideEnabled = $leanEngineOverrideRequested" in text
    assert "function Get-DotNetRuntimeMajor" in text
    assert "function Get-EngineDepsTargetMajor" in text
    assert "$maxMajor = $null" in text
    assert "return $maxMajor" in text
    assert '"DOTNET_ROLL_FORWARD=LatestMajor"' in text
    assert '$dockerArgs += @("-v", "${engineOverrideDll}:/Lean/Launcher/bin/Debug/QuantConnect.Lean.Engine.dll")' in text
    assert '$overrideFiles = Get-ChildItem -Path $modulesPath -File -ErrorAction SilentlyContinue' not in text
    assert "Python.Runtime.dll" not in text
    assert "CSharpAPI.dll" not in text
    assert "Write-Warning $runtimeMismatchWarning" in text
    assert "Write-Warning (" not in text


def test_run_backtest_checks_docker_daemon_before_launch():
    text = _read("run_backtest.ps1")
    assert "function Ensure-DockerDaemonReady" in text
    assert 'Write-Warning "Docker daemon is not reachable via current context."' in text
    assert 'Start-Process -FilePath $dockerDesktopExe' in text
    assert "if (-not (Ensure-DockerDaemonReady)) {" in text
    assert 'Write-Error "Docker daemon unavailable. Start Docker Desktop and rerun."' in text


def test_run_strategy_engine_override_mounts_matching_core_companions():
    text = _read("run_strategy.ps1")
    assert '$engineOverrideArtifactsPath = Join-Path $root "build\\engine_override"' in text
    assert '$engineOverrideCompanionFiles = @(' in text
    assert '"QuantConnect.Common.dll"' in text
    assert '"QuantConnect.Brokerages.dll"' in text
    assert '"QuantConnect.Configuration.dll"' in text
    assert '"QuantConnect.Logging.dll"' in text
    assert '"QuantConnect.Algorithm.dll"' in text
    assert '"QuantConnect.Algorithm.Framework.dll"' in text
    assert '"QuantConnect.AlgorithmFactory.dll"' in text
    assert '"QuantConnect.Algorithm.CSharp.dll"' in text
    assert '"QuantConnect.Indicators.dll"' in text
    assert '"QuantConnect.Compression.dll"' in text
    assert '$engineOverrideSourcePath = $modulesPath' in text
    assert '$engineOverrideSourcePath = $engineOverrideArtifactsPath' in text
    assert '$dockerArgs += @("-v", "${overridePath}:/Lean/Launcher/bin/Debug/$overrideName")' in text
    assert 'matching companion override artifacts were not found' in text


def test_run_backtest_engine_override_mounts_matching_core_companions():
    text = _read("run_backtest.ps1")
    assert '$engineOverrideArtifactsPath = Join-Path $root "build\\engine_override"' in text
    assert '$engineOverrideCompanionFiles = @(' in text
    assert '"QuantConnect.Common.dll"' in text
    assert '"QuantConnect.Brokerages.dll"' in text
    assert '"QuantConnect.Configuration.dll"' in text
    assert '"QuantConnect.Logging.dll"' in text
    assert '"QuantConnect.Algorithm.dll"' in text
    assert '"QuantConnect.Algorithm.Framework.dll"' in text
    assert '"QuantConnect.AlgorithmFactory.dll"' in text
    assert '"QuantConnect.Algorithm.CSharp.dll"' in text
    assert '"QuantConnect.Indicators.dll"' in text
    assert '"QuantConnect.Compression.dll"' in text
    assert '$engineOverrideSourcePath = $modulesPath' in text
    assert '$engineOverrideSourcePath = $engineOverrideArtifactsPath' in text
    assert '$dockerArgs += @("-v", "${overridePath}:/Lean/Launcher/bin/Debug/$overrideName")' in text
    assert 'matching companion override artifacts were not found' in text


def test_build_engine_override_defaults_to_net10():
    text = _read("build_engine_override.ps1")
    assert '[string]$Image = "mcr.microsoft.com/dotnet/sdk:10.0"' in text
    assert '[string]$TargetFramework = "net10.0"' in text


def test_build_image_defaults_to_net10_launcher_tag():
    text = _read("build_image.ps1")
    assert '[string]$BaseImage = "lean-alpaca-proxy:latest"' in text
    assert '[string]$TargetImage = "lean-alpaca-proxy:net10"' in text


def test_run_strategy_resolves_external_strategy_source_with_fallback():
    text = _read("run_strategy.ps1")
    assert "function Resolve-StrategySourceRoot" in text
    assert 'Join-Path (Split-Path -Parent $root) "my_custom_lean"' in text
    assert '$strategySourceRoot = Resolve-StrategySourceRoot -CurrentRoot $root -ExplicitRoot $StrategyRepoRoot' in text
    assert '$strategyDirs = Get-ChildItem -Path $strategySourceRoot -Directory -Filter "S_*" | Sort-Object Name' in text
    assert "Copy-StrategySource -SourceDir $strategyDir.FullName -DestinationDir $sourceSnapshotDir" in text


def test_run_backtest_resolves_external_strategy_source_with_fallback():
    text = _read("run_backtest.ps1")
    assert "function Resolve-StrategySourceRoot" in text
    assert 'Join-Path (Split-Path -Parent $root) "my_custom_lean"' in text
    assert '$strategySourceRoot = Resolve-StrategySourceRoot -CurrentRoot $root -ExplicitRoot $StrategyRepoRoot' in text
    assert '$strategyDirs = Get-ChildItem -Path $strategySourceRoot -Directory -Filter "S_*" | Sort-Object Name' in text
    assert "Copy-StrategySource -SourceDir $strategyDir.FullName -DestinationDir $sourceSnapshotDir" in text
