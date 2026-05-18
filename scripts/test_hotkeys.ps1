param(
    [ValidateSet("Source", "Exe")]
    [string]$Mode = "Exe",

    [ValidateSet("Ctrl+Win", "Ctrl+Win+Space")]
    [string]$Shortcut = "Ctrl+Win+Space",

    [string]$ExePath = "",

    [int]$Seconds = 60
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$appDataDir = Join-Path $env:APPDATA "Whisper Anywhere"
$settingsPath = Join-Path $appDataDir "settings.json"
$logPath = Join-Path $appDataDir "hotkey.log"

New-Item -ItemType Directory -Force -Path $appDataDir | Out-Null

$settings = [ordered]@{}
if (Test-Path $settingsPath) {
    $backupPath = "$settingsPath.hotkey-test-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $settingsPath -Destination $backupPath -Force
    $loaded = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    foreach ($property in $loaded.PSObject.Properties) {
        $settings[$property.Name] = $property.Value
    }
}

if (-not $settings.Contains("selected_model")) { $settings["selected_model"] = "base" }
if (-not $settings.Contains("microphone_device")) { $settings["microphone_device"] = "default" }
if (-not $settings.Contains("insert_method")) { $settings["insert_method"] = "clipboard_paste" }
if (-not $settings.Contains("restore_clipboard")) { $settings["restore_clipboard"] = $true }

$settings["shortcut"] = $Shortcut
$settings["shortcut_mode"] = "hold"
$settings["hotkey_default_version"] = 2

$settings | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $settingsPath -Encoding UTF8

if (Test-Path $logPath) {
    Remove-Item -LiteralPath $logPath -Force
}

if ($Mode -eq "Source") {
    $mainPath = Join-Path $repoRoot "src\main.py"
    Write-Host "Starting source app with $Shortcut..."
    $process = Start-Process -FilePath "py" -ArgumentList @("-3.11", $mainPath) -WorkingDirectory $repoRoot -PassThru
}
else {
    if (-not $ExePath) {
        $ExePath = Join-Path $repoRoot "dist\Whisper Anywhere\Whisper Anywhere.exe"
    }
    if (-not (Test-Path $ExePath)) {
        throw "EXE not found: $ExePath"
    }
    Write-Host "Starting packaged EXE with $Shortcut..."
    $process = Start-Process -FilePath $ExePath -PassThru
}

Start-Process notepad.exe | Out-Null

Write-Host ""
Write-Host "Manual test window: $Seconds seconds"
Write-Host "1. Click inside Notepad."
Write-Host "2. Hold $Shortcut."
Write-Host "3. Speak a short phrase."
Write-Host "4. Release $Shortcut."
Write-Host "5. Watch this console and the app status for hook/key transition logs."
Write-Host ""
Write-Host "Hotkey log: $logPath"
Write-Host "Process id: $($process.Id)"
Write-Host ""

$deadline = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $deadline) {
    if (Test-Path $logPath) {
        Write-Host "---- hotkey.log tail $(Get-Date -Format 'HH:mm:ss') ----"
        Get-Content -LiteralPath $logPath -Tail 20
    }
    else {
        Write-Host "Waiting for hotkey.log..."
    }
    Start-Sleep -Seconds 5
}

Write-Host ""
Write-Host "Test window complete. Leave the app open for more testing, or exit it from the tray."
