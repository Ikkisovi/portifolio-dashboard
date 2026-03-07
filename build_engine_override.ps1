param(
    [string]$Image = "mcr.microsoft.com/dotnet/sdk:10.0",
    [string]$TargetFramework = "net10.0"
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$leanSource = Join-Path $root "Lean-full"
$engineProject = "Engine/QuantConnect.Lean.Engine.csproj"
$outputPath = Join-Path $root "build\engine_override"
$modulesPath = Join-Path $root "modules\ibkr"

if (-not (Test-Path $leanSource)) {
    Write-Error "Missing Lean source snapshot at $leanSource"
    exit 1
}

if (-not (Test-Path (Join-Path $leanSource $engineProject))) {
    Write-Error "Missing engine project: $(Join-Path $leanSource $engineProject)"
    exit 1
}

if (-not (Test-Path $outputPath)) {
    New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
}
if (-not (Test-Path $modulesPath)) {
    New-Item -ItemType Directory -Path $modulesPath -Force | Out-Null
}

Write-Host "Building QuantConnect.Lean.Engine.dll override in container..."
Write-Host "Source: $leanSource"
Write-Host "Output: $outputPath"
Write-Host "Image : $Image"
Write-Host "Target: $TargetFramework"

$containerScript = @'
set -euo pipefail

WORKDIR=/tmp/lean_engine_override
SRC_DIR=/workspace/Lean-full
BUILD_DIR="$WORKDIR/Lean-full"
OUT_DIR=/workspace/build/engine_override
TARGET_FRAMEWORK="${TARGET_FRAMEWORK:-net10.0}"

rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"
cp -R "$SRC_DIR" "$BUILD_DIR"

if [ "$TARGET_FRAMEWORK" = "net10.0" ]; then
  find "$BUILD_DIR" -name "*.csproj" -type f -print0 | xargs -0 sed -i 's/net9.0/net10.0/g'
fi

dotnet restore "$BUILD_DIR/Engine/QuantConnect.Lean.Engine.csproj" -p:TargetFramework="$TARGET_FRAMEWORK" -p:NuGetAudit=false
dotnet build "$BUILD_DIR/Engine/QuantConnect.Lean.Engine.csproj" -c Release -f "$TARGET_FRAMEWORK" --no-restore -p:RunAnalyzers=false -p:EnableNETAnalyzers=false -v minimal

ENGINE_DLL=$(find "$BUILD_DIR/Engine/bin/Release" -type f -name "QuantConnect.Lean.Engine.dll" | head -n 1 || true)
if [ -z "$ENGINE_DLL" ]; then
  echo "Could not locate QuantConnect.Lean.Engine.dll under Engine/bin/Release" >&2
  exit 1
fi
ENGINE_OUT=$(dirname "$ENGINE_DLL")
mkdir -p "$OUT_DIR"
cp "$ENGINE_DLL" "$OUT_DIR/QuantConnect.Lean.Engine.dll"
if [ -f "$ENGINE_OUT/QuantConnect.Lean.Engine.pdb" ]; then
  cp "$ENGINE_OUT/QuantConnect.Lean.Engine.pdb" "$OUT_DIR/"
fi
if [ -f "$ENGINE_OUT/QuantConnect.Lean.Engine.deps.json" ]; then
  cp "$ENGINE_OUT/QuantConnect.Lean.Engine.deps.json" "$OUT_DIR/"
fi

for project in Logging Configuration Compression Common Api Algorithm Brokerages Indicators Algorithm.Framework AlgorithmFactory Algorithm.CSharp; do
  PROJECT_OUT="$BUILD_DIR/$project/bin/Release"
  if [ -d "$PROJECT_OUT" ]; then
    find "$PROJECT_OUT" -maxdepth 1 -type f \( -name "QuantConnect.*.dll" -o -name "QuantConnect.*.pdb" -o -name "QuantConnect.*.deps.json" \) -exec cp {} "$OUT_DIR/" \;
  fi
done

PY_RUNTIME_DIR=$(find "$BUILD_DIR" -type f -path "*/pythonnet/runtime/Python.Runtime.dll" -printf '%h\n' | head -n 1 || true)
if [ -n "$PY_RUNTIME_DIR" ]; then
  if [ -f "$PY_RUNTIME_DIR/Python.Runtime.dll" ]; then
    cp "$PY_RUNTIME_DIR/Python.Runtime.dll" "$OUT_DIR/"
  fi
  if [ -f "$PY_RUNTIME_DIR/Python.Runtime.pdb" ]; then
    cp "$PY_RUNTIME_DIR/Python.Runtime.pdb" "$OUT_DIR/"
  fi
  if [ -f "$PY_RUNTIME_DIR/Python.Runtime.deps.json" ]; then
    cp "$PY_RUNTIME_DIR/Python.Runtime.deps.json" "$OUT_DIR/"
  fi
fi
'@

$cursorDir = Join-Path $root ".cursor"
if (-not (Test-Path $cursorDir)) {
    New-Item -ItemType Directory -Path $cursorDir -Force | Out-Null
}
$containerScriptPath = Join-Path $cursorDir "build_engine_override.sh"
$containerScriptUnix = $containerScript -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($containerScriptPath, $containerScriptUnix)

$dockerArgs = @(
    "run",
    "--rm",
    "-v", "${root}:/workspace",
    "-e", "TARGET_FRAMEWORK=$TargetFramework",
    $Image,
    "/bin/bash",
    "/workspace/.cursor/build_engine_override.sh"
)
docker @dockerArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Container build failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

$builtDll = Join-Path $outputPath "QuantConnect.Lean.Engine.dll"
$builtPdb = Join-Path $outputPath "QuantConnect.Lean.Engine.pdb"
$builtDeps = Join-Path $outputPath "QuantConnect.Lean.Engine.deps.json"
if (-not (Test-Path $builtDll)) {
    Write-Error "Expected artifact not found: $builtDll"
    exit 1
}

$moduleDll = Join-Path $modulesPath "QuantConnect.Lean.Engine.dll"
$modulePdb = Join-Path $modulesPath "QuantConnect.Lean.Engine.pdb"
$moduleDeps = Join-Path $modulesPath "QuantConnect.Lean.Engine.deps.json"

Copy-Item -Path $builtDll -Destination $moduleDll -Force
if (Test-Path $builtPdb) {
    Copy-Item -Path $builtPdb -Destination $modulePdb -Force
}
if (Test-Path $builtDeps) {
    Copy-Item -Path $builtDeps -Destination $moduleDeps -Force
}

$overrideArtifacts = Get-ChildItem -Path $outputPath -File -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -like "QuantConnect.*.dll" -or
    $_.Name -like "QuantConnect.*.pdb" -or
    $_.Name -like "QuantConnect.*.deps.json" -or
    $_.Name -like "Python.Runtime.dll" -or
    $_.Name -like "Python.Runtime.pdb" -or
    $_.Name -like "Python.Runtime.deps.json"
}
foreach ($artifact in $overrideArtifacts) {
    Copy-Item -Path $artifact.FullName -Destination (Join-Path $modulesPath $artifact.Name) -Force
}

$hash = (Get-FileHash -Path $builtDll -Algorithm SHA256).Hash
$timestamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
$manifest = [ordered]@{
    built_at          = $timestamp
    image             = $Image
    source            = $leanSource
    output_dll        = $builtDll
    module_dll        = $moduleDll
    sha256            = $hash
    override_files    = @($overrideArtifacts | ForEach-Object { $_.Name })
}
$manifestPath = Join-Path $outputPath "engine_override_manifest.json"
$manifest | ConvertTo-Json -Depth 6 | Set-Content -Path $manifestPath -Encoding UTF8

Write-Host "Build complete."
Write-Host "DLL SHA256 : $hash"
Write-Host "Timestamp  : $timestamp"
Write-Host "Module DLL : $moduleDll"
Write-Host "Manifest   : $manifestPath"
