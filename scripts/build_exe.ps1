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
    # FunASR and PyTorch, and what only they or other packages' tools and tests
    # import: the app never loads any of it. PyAV (FFmpeg) only decodes files,
    # and Whisper gets samples (see _faster_whisper in transcriber.py).
    foreach ($Package in @(
        "funasr", "torch", "torchaudio", "modelscope", "transformers",
        "av", "numba", "llvmlite", "scipy", "sympy", "mpmath", "pytest", "_pytest",
        "PIL", "tensorboardX", "soundfile", "cryptography", "win32ui"
    )) {
        $OptionalPyInstallerArgs += "--exclude-module"
        $OptionalPyInstallerArgs += $Package
    }
} else {
    $OptionalPyInstallerArgs += @("--collect-all", "av")  # FunASR decodes files
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
# ONNX Runtime and onnx are found from the app's own imports. Collecting all
# of them took their tools and tests along, and with those numba, llvmlite,
# sympy, scipy, pytest and Pillow: about 190 MB the app never imports.
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
    --collect-all truststore `
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

# Qt files that a widgets app playing four WAV cues never loads (from the
# DLLs' import tables, 2026-11-09): software OpenGL; the on-screen keyboard,
# the only user of Quick, QML and Qt's OpenGL; the PDF image plugin; the FFmpeg
# media backend (main.py picks Windows' own); translations of Qt's own dialogs.
$Bundle = Join-Path $Root "$DistPath\Whisper Anywhere"
foreach ($Unused in @(
    "PySide6\opengl32sw.dll",
    "PySide6\plugins\platforminputcontexts",
    "PySide6\Qt6VirtualKeyboard.dll",
    "PySide6\Qt6Quick.dll",
    "PySide6\Qt6Qml*.dll",
    "PySide6\Qt6OpenGL.dll",
    "PySide6\plugins\imageformats\qpdf.dll",
    "PySide6\Qt6Pdf.dll",
    "PySide6\plugins\multimedia\ffmpegmediaplugin.dll",
    "PySide6\avcodec-*.dll",
    "PySide6\avformat-*.dll",
    "PySide6\avutil-*.dll",
    "PySide6\swscale-*.dll",
    "PySide6\swresample-*.dll",
    "PySide6\translations"
)) {
    Get-Item (Join-Path $Bundle "_internal\$Unused") -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
}
# A file left behind that needs one of those would fail only on a user's PC.
& $Python (Join-Path $PSScriptRoot "check_bundle.py") $Bundle
if ($LASTEXITCODE -ne 0) { throw "The bundle check failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "Build complete: $DistPath\Whisper Anywhere\Whisper Anywhere.exe"
Write-Host "The EXE bundles Python and dependencies. Speech models (Voz, Whisper, FunASR) are downloaded by the app on first use."
