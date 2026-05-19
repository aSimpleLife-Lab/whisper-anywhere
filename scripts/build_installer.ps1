$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$InnoScript = Join-Path $Root "installer\WhisperAnywhere.iss"
$Exe = Join-Path $Root "dist\Whisper Anywhere\Whisper Anywhere.exe"

Set-Location $Root

if (!(Test-Path $Exe)) {
    Write-Host "Packaged app was not found. Building onedir ZIP-compatible app first..."
    & (Join-Path $PSScriptRoot "build_exe.ps1")
}

$Candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }

if (!$Candidates) {
    throw "Inno Setup 6 compiler (ISCC.exe) was not found. Install Inno Setup, then rerun this script. The ZIP release flow is unchanged."
}

Write-Host "Building installer with Inno Setup..."
& $Candidates[0] $InnoScript
Write-Host "Installer complete: dist\Whisper-Anywhere-Setup.exe"
