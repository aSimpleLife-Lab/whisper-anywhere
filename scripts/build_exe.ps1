$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

Set-Location $Root

if (!(Test-Path $Python)) {
    Write-Host "Creating local build environment..."
    py -3.11 -m venv $Venv
}

Write-Host "Installing build dependencies..."
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt

Write-Host "Building Whisper Anywhere.exe..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name "Whisper Anywhere" `
    --paths "src" `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all av `
    --collect-submodules sounddevice `
    "src\main.py"

Write-Host ""
Write-Host "Build complete: dist\Whisper Anywhere\Whisper Anywhere.exe"
Write-Host "The EXE bundles Python and dependencies. Whisper models are downloaded by the app on first use."
