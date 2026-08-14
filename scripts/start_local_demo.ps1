param(
    [ValidateRange(1024, 65535)]
    [int] $Port = 8000
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "The project virtual environment was not found at $python"
}

if (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot ".env"))) {
    throw "Create .env from .env.example and add the local test credentials first."
}

Push-Location $repositoryRoot
try {
    # These overrides apply only to this process and its Python child process.
    # They do not modify .env or any Azure App Service setting.
    $env:RUNTIME_PROFILE = "local_demo"
    $env:PORT = [string] $Port

    & $python "scripts\verify_runtime_profile.py" --profile local_demo
    if ($LASTEXITCODE -ne 0) {
        throw "Local demo profile validation failed."
    }

    Write-Host "Starting the local SSP Licensing Agent on http://127.0.0.1:$Port"
    Write-Host "Keep this terminal open. Start ngrok in a second terminal."
    & $python "main.py"
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
