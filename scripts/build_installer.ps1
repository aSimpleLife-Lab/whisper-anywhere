# Builds the shareable installer: the lite app (no FunASR/PyTorch) wrapped by Inno Setup.
# Output: dist-installer\WhisperAnywhere-Setup-<version>.exe
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

& (Join-Path $PSScriptRoot "build_exe.ps1") -Lite

$Version = (Select-String -Path "src\core\app_version.py" -Pattern 'APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
$Iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (!$Iscc) { throw "Inno Setup 6 is not installed: winget install JRSoftware.InnoSetup" }

& $Iscc "/DAppVersion=$Version" "installer\WhisperAnywhere.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
Write-Host "Installer: dist-installer\WhisperAnywhere-Setup-$Version.exe"
