![Whisper Anywhere banner](assets/banner.png)

# Whisper Anywhere

Whisper Anywhere is a native Windows 11 desktop app for local Whisper voice typing.

Click in any app, hold `Ctrl + Alt + Q`, speak, release, and Whisper Anywhere transcribes your speech locally and pastes the text into the focused Windows app.

This is not a web app and it does not use a cloud API in V1.

## Download the Windows EXE

The easiest way to try Whisper Anywhere is to download the prebuilt Windows release:

**Download latest Windows build:** [Whisper-Anywhere-Windows.zip](https://github.com/aSimpleLife-Lab/whisper-anywhere/releases/download/v0.1.0/Whisper-Anywhere-Windows.zip)

After downloading:

1. Extract `Whisper-Anywhere-Windows.zip`.
2. Open the extracted `Whisper Anywhere` folder.
3. Run `Whisper Anywhere.exe`.

The app downloads Whisper models on first use, so the first launch/model setup can take a little while.
To launch automatically after reboot, open the app and enable **Start with Windows hidden in the tray** in Core Settings.

## Developer Update Log

<details>
<summary><strong>Current build</strong> - latest EXE in the download link above</summary>

- Proper native Windows app, taskbar, tray, and EXE icon
- Bundled custom start/stop feedback sounds
- Feature list dropdown inside the app
- Safer settings dropdowns that ignore mouse-wheel changes until opened
- Optional **Start with Windows hidden in the tray**

</details>

<details>
<summary><strong>v0.1.0</strong> - first public Windows release</summary>

- Reliable hold-to-talk voice typing with `Ctrl + Alt + Q`
- Local `faster-whisper` transcription with CPU-safe defaults
- Custom start and stop sounds
- System tray controls and downloadable Windows build

</details>

## V1 Features

- PySide6 Windows desktop window
- Proper app, tray, and EXE icon
- System tray support
- Global hold-to-talk shortcut: `Ctrl + Alt + Q`
- Optional toggle shortcut mode
- Shortcut settings UI
- Feature list dropdown inside the app
- Microphone selection
- Local settings file at `%APPDATA%\Whisper Anywhere\settings.json`
- Whisper model selector: `tiny`, `base`, `small`, `medium`, `large`, `large-v2`, `large-v3`, `turbo`
- Model cards with Installed/Download status, speed, accuracy, resource estimate, and recommended use
- Performance / Hardware controls for Auto, CPU Only, GPU Preferred, compute precision, presets, fallback, RAM/VRAM modes, and CPU threads
- Local transcription with `faster-whisper`
- Clipboard paste insertion into the focused Windows app
- Restore previous plain-text clipboard after paste
- Bundled custom start and stop feedback sounds
- Optional Windows startup launch hidden in the tray
- Safer dropdowns that do not change settings while you are just scrolling the page
- Automatic local folder creation
- Automatic selected-model preparation/download when enabled
- Clean shutdown of tray, keyboard hook, and microphone resources

## Normal User Workflow

After the app is built into an EXE, the user should not need to install Python, run pip, download models manually, configure environment variables, or start background scripts.

1. Open `Whisper Anywhere.exe`.
2. The app creates its local settings and model folders.
3. The app checks the selected Whisper model.
4. If the model is missing, the app prepares/downloads it automatically when enabled.
5. Click into any text field or app.
6. Hold `Ctrl + Alt + Q`.
7. Speak.
8. Release `Ctrl + Alt + Q`.
9. Text appears where the cursor was.

## Performance / Hardware

Beginner-friendly modes:

- `Fast`: favors the `base` model and quick automatic settings.
- `Balanced`: default mode, also starts with `base` for reliable V1 performance.
- `Accurate`: favors `medium` for better quality.
- `Low RAM Mode`: favors CPU + `int8` and warns before large models.
- `Low VRAM Mode`: favors smaller models and `int8_float16` for GPUs with less memory.

Hardware choices:

- `Auto`: uses CPU by default unless GPU use is enabled.
- `CPU Only`: forces local CPU transcription and is the safest mode.
- `GPU Preferred`: tries CUDA first, then falls back to CPU when fallback is enabled.

Advanced choices:

- Compute precision: `Auto`, `int8`, `int8_float16`, `float16`, `float32`
- CPU threads: `Auto` or a manual thread count

If GPU loading fails and CPU fallback is enabled, Whisper Anywhere shows a friendly message and continues on CPU.

## Developer Setup From Source

These steps are only for building or developing the app from the GitHub source code. End users should use the built EXE.

### 1. Clone the repo

```powershell
git clone https://github.com/aSimpleLife-Lab/whisper-anywhere.git
cd whisper-anywhere
```

### 2. Create a virtual environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

### 4. Run the app

```powershell
python .\src\main.py
```

## How To Test The Shortcut

1. Start Whisper Anywhere.
2. Wait until the selected model says it is ready. The first model preparation can take a while because it downloads model files.
3. Open Notepad or PowerShell.
4. Click where text should appear.
5. Hold `Ctrl + Alt + Q`.
6. Speak a short sentence.
7. Release `Ctrl + Alt + Q`.
8. The transcribed text should paste into the app you clicked.

If a Windows-key shortcut is flaky on your system, switch back to the default `Ctrl + Alt + Q`. You can change the shortcut in the app.

## Hotkey Diagnostics

Whisper Anywhere writes a hotkey diagnostic log whenever the global hook starts, stops, updates, or sees shortcut key transitions.

```text
%APPDATA%\Whisper Anywhere\hotkey.log
```

The Core Settings section also shows the current hotkey status and the log file path. For reliability testing, compare:

- Default: `Ctrl + Alt + Q`
- Legacy comparison only: `Ctrl + Win + Space`, `Ctrl + Win`

`Ctrl + Alt + Q` is the recommended default because it avoids Windows-shell shortcuts while still giving a clean hold/release sequence. Windows-key shortcuts are still available, but they are less reliable across machines.

Developer shortcut test helper:

```powershell
.\scripts\test_hotkeys.ps1 -Mode Source -Shortcut Ctrl+Alt+Q
.\scripts\test_hotkeys.ps1 -Mode Source -Shortcut Ctrl+Win
.\scripts\test_hotkeys.ps1 -Mode Exe -Shortcut Ctrl+Alt+Q
.\scripts\test_hotkeys.ps1 -Mode Exe -Shortcut Ctrl+Win
```

The helper updates the local shortcut setting, starts the app, opens Notepad, and tails `hotkey.log` while you press the shortcut.

## Build The EXE

Run this on Windows 11:

```powershell
.\scripts\build_exe.ps1
```

The output will be:

```text
dist\Whisper Anywhere\Whisper Anywhere.exe
```

The EXE bundles Python and Python dependencies. Whisper model files are not bundled because they are large; the app downloads/prepares the selected model automatically on first use and stores it locally.

## Local Files

Settings:

```text
%APPDATA%\Whisper Anywhere\settings.json
```

Models:

```text
%LOCALAPPDATA%\Whisper Anywhere\models
```

Temporary recordings:

```text
%TEMP%\Whisper Anywhere
```

Temporary recordings are deleted after transcription by default.

## Settings Defaults

```json
{
  "selected_model": "base",
  "shortcut": "Ctrl+Alt+Q",
  "shortcut_mode": "hold",
  "hotkey_default_version": 3,
  "start_sound_path": "",
  "stop_sound_path": "",
  "device": "cpu",
  "compute_type": "int8",
  "performance_preset": "balanced",
  "runtime_default_version": 2,
  "use_gpu_if_available": false,
  "fallback_to_cpu": true,
  "low_ram_mode": false,
  "low_vram_mode": false,
  "warn_before_large_models": true,
  "cpu_threads": "auto",
  "auto_download_models": true
}
```

## V1 Limitations

- No history page yet.
- No file transcription tab yet.
- No cloud API mode yet.
- No command replacements yet, so saying `slash` will transcribe as the word `slash` for now.
- Clipboard restore preserves previous plain text only in V1, not rich clipboard formats like images or Word formatting.
- Typing into elevated Administrator apps may require running Whisper Anywhere as Administrator too.
- The first model setup can take time and needs internet access.
- GPU mode needs a compatible CUDA setup; otherwise Auto/GPU Preferred can fall back to CPU.

## Troubleshooting

### No microphone found

Check Windows Settings > Privacy & security > Microphone and make sure desktop apps can access the microphone.

### Shortcut does not start recording

Restart the app. If it still fails, change the shortcut in the app. Some security software may block global keyboard hooks.

### Text does not appear in the target app

Try the default clipboard paste mode. If the target app is running as Administrator, run Whisper Anywhere as Administrator too.

### GPU does not work

Use `Auto` or `CPU Only`, or keep `Fall back to CPU if GPU fails` enabled. GPU transcription requires compatible CUDA libraries for faster-whisper/ctranslate2.

### Model download fails

Check internet access, available disk space, and permissions for `%LOCALAPPDATA%\Whisper Anywhere\models`.
