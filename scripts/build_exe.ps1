param(
    # Lite: leaves out FunASR and PyTorch (about 5 GB) so the build is small enough to share.
    # Voz and every Whisper model work; SenseVoice and Fun-ASR then say their engine is missing.
    [switch]$Lite
)
$ErrorActionPreference = "Stop"  # covers cmdlets only; native exit codes are checked below

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

Set-Location $Root

if (!(Test-Path $Python)) {
    Write-Host "Creating local build environment..."
    py -3.11 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Creating the build environment failed with exit code $LASTEXITCODE" }
}

Write-Host "Installing build dependencies..."
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Upgrading pip failed with exit code $LASTEXITCODE" }
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Installing requirements failed with exit code $LASTEXITCODE" }

$OptionalPyInstallerArgs = @()
$DistPath = "dist"
if ($Lite) {
    $DistPath = "dist-lite"
    $OptionalPyInstallerArgs += @("--distpath", $DistPath, "--workpath", "build-lite")
    foreach ($Package in @("funasr", "torch", "torchaudio", "modelscope", "transformers")) {
        $OptionalPyInstallerArgs += "--exclude-module"
        $OptionalPyInstallerArgs += $Package
    }
} else {
    # find_spec prints nothing: under Windows PowerShell 5.1 a failed import's
    # redirected traceback was a terminating error.
    & $Python -c "import importlib.util, sys; sys.exit(importlib.util.find_spec('funasr') is None)"
}
if (!$Lite -and $LASTEXITCODE -eq 0) {
    Write-Host "FunASR detected; adding optional FunASR packaging hooks..."
    foreach ($Package in @(
        "torch",
        "torchaudio",
        "funasr",
        "modelscope",
        "transformers",
        "tokenizers",
        "sentencepiece",
        "safetensors"
    )) {
        $OptionalPyInstallerArgs += "--collect-all"
        $OptionalPyInstallerArgs += $Package
    }
}

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
    --add-data "assets\sounds;assets\sounds" `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all onnxruntime `
    --collect-all onnx `
    --collect-all truststore `
    --collect-all av `
    --collect-all sounddevice `
    --collect-all PySide6.QtMultimedia `
    $OptionalPyInstallerArgs `
    --hidden-import win32timezone `
    --hidden-import pynput `
    --hidden-import pynput.keyboard._win32 `
    --hidden-import pynput.mouse._win32 `
    "src\main.py"
# Without this a failed build still said complete, and the installer packaged
# the previous dist-lite under the new version number.
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "Build complete: $DistPath\Whisper Anywhere\Whisper Anywhere.exe"
Write-Host "The EXE bundles Python and dependencies. Speech models (Voz, Whisper, FunASR) are downloaded by the app on first use."
