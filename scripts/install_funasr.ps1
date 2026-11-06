param(
    [switch]$CpuOnly
)

# "Stop" does not cover native commands, so each one checks its exit code.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

Set-Location $Root

if (!(Test-Path $Python)) {
    Write-Host "Creating local environment..."
    py -3.11 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Creating the environment failed with exit code $LASTEXITCODE" }
}

Write-Host "Updating pip..."
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Updating pip failed with exit code $LASTEXITCODE" }

Write-Host "Installing base Whisper Anywhere dependencies..."
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Installing requirements.txt failed with exit code $LASTEXITCODE" }

$UseCuda = -not $CpuOnly -and [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($UseCuda) {
    Write-Host "Installing CUDA PyTorch runtime for FunASR..."
    & $Python -m pip install --upgrade "torch==2.11.0+cu128" "torchaudio==2.11.0+cu128" --extra-index-url https://download.pytorch.org/whl/cu128
    if ($LASTEXITCODE -ne 0) { throw "Installing CUDA PyTorch failed with exit code $LASTEXITCODE" }
}
else {
    Write-Host "Installing CPU PyTorch runtime for FunASR..."
    & $Python -m pip install --upgrade "torch==2.11.0+cpu" "torchaudio==2.11.0+cpu" --extra-index-url https://download.pytorch.org/whl/cpu
    if ($LASTEXITCODE -ne 0) { throw "Installing CPU PyTorch failed with exit code $LASTEXITCODE" }
}

Write-Host "Installing FunASR dependencies..."
& $Python -m pip install --upgrade "funasr==1.3.9" "more-itertools>=11.1.0" "truststore>=0.10.4"
if ($LASTEXITCODE -ne 0) { throw "Installing FunASR failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "FunASR runtime installed. Run scripts\test_funasr_live.ps1 to verify local transcription."
