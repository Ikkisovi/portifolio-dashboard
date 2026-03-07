param(
    [string]$BaseImage = "lean-alpaca-proxy:latest",
    [string]$TargetImage = "lean-alpaca-proxy:net10"
)

# Build a launcher image that includes .NET 10 runtime and keeps existing proxy behavior.
docker build -f Dockerfile.custom -t $TargetImage --build-arg BASE_IMAGE=$BaseImage .
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker build failed."
    exit $LASTEXITCODE
}

# Instructions
Write-Host "Build complete."
Write-Host "To run the container:"
Write-Host "docker run --name lean_proxy_test --rm -it -e DOTNET_ROLL_FORWARD=LatestMajor -e ALPACA_PROXY_URL=ws://host.docker.internal:8765 -v ${PWD}/proxy/config/config.json:/Lean/Launcher/config.json $TargetImage"
