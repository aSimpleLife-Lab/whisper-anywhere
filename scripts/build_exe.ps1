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
    --icon "assets\icon.ico" `
    --paths "src" `
    --add-data "assets\icon.ico;assets" `
    --add-data "assets\icon.png;assets" `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all av `
    --collect-all sounddevice `
    --collect-all PySide6.QtMultimedia `
    --hidden-import win32timezone `
    --hidden-import pynput `
    --hidden-import pynput.keyboard._win32 `
    --hidden-import pynput.mouse._win32 `
    "src\main.py"

Write-Host ""
Write-Host "Build complete: dist\Whisper Anywhere\Whisper Anywhere.exe"
Write-Host "The EXE bundles Python and dependencies. Whisper models are downloaded by the app on first use."
