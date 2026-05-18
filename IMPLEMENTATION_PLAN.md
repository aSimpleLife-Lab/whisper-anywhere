# Whisper Anywhere Implementation Plan

## Goal

Build a native Windows 11 desktop app that lets the user click into any text field, hold a global shortcut, speak, release, and have locally transcribed Whisper text inserted into the focused app.

Version 1 remains focused on the reliable core: global shortcut, microphone recording, local Whisper transcription, typing into the active cursor location, model selection, shortcut editing, settings, tray behavior, and beginner-friendly model/hardware controls.

## Chosen Stack

- UI: Python + PySide6 / Qt for Python
- Global hotkeys: Win32 low-level keyboard hook through `ctypes`
- Microphone capture: `sounddevice` plus `numpy`, saved as temporary WAV with Python `wave`
- Transcription: `faster-whisper`
- Runtime detection: `ctranslate2` helpers when available through faster-whisper
- Text insertion: Win32 `SendInput` plus clipboard APIs through `pywin32` / `ctypes`
- Tray: PySide6 `QSystemTrayIcon`
- Settings: readable JSON under `%APPDATA%\Whisper Anywhere\settings.json`
- Model files: `%LOCALAPPDATA%\Whisper Anywhere\models`
- Packaging: PyInstaller `--onedir` for Windows

## Current V1 File Structure

```text
whisper-app/
  README.md
  IMPLEMENTATION_PLAN.md
  requirements.txt
  scripts/
    build_exe.ps1
    test_hotkeys.ps1
  src/
    main.py
    core/
      __init__.py
      audio_recorder.py
      hotkey_listener.py
      model_manager.py
      settings_manager.py
      text_inserter.py
      transcriber.py
      tray_manager.py
    ui/
      __init__.py
      main_window.py
```

## Exact Windows APIs

Global shortcut and hold-to-talk:

- `SetWindowsHookExW(WH_KEYBOARD_LL, ...)`
- `CallNextHookEx`
- `UnhookWindowsHookEx`
- `GetMessageW`, `TranslateMessage`, `DispatchMessageW`
- Virtual-key tracking for `VK_CONTROL`, `VK_LCONTROL`, `VK_RCONTROL`, `VK_LWIN`, `VK_RWIN`, `VK_ESCAPE`, plus user-selected keys

Focused app and insertion:

- `GetForegroundWindow` to remember the target window at listening start
- `SetForegroundWindow` only as a best-effort fallback before insertion
- `SendInput` for Ctrl+V paste and simulated Unicode typing
- Clipboard APIs through pywin32 for paste mode: `OpenClipboard`, `EmptyClipboard`, `GetClipboardData`, `SetClipboardText`, `CloseClipboard`, `CF_UNICODETEXT`

## Core Architecture

UI layer:

- Displays status, model cards, microphone picker, shortcut reminder, and settings.
- Adds a beginner-friendly `Performance / Hardware` section.
- Uses Qt signals to receive state updates from background workers.

Hotkey listener:

- Runs a Win32 low-level keyboard hook on a dedicated thread.
- Supports hold-to-talk, toggle mode, and Esc cancel.
- Uses `Ctrl+Win+Space` as the default because it includes a non-modifier trigger key.
- Keeps legacy `Ctrl+Win` available for comparison, but treats modifier-only shortcuts as less reliable on Windows.
- Writes hook install, hook shutdown, shortcut updates, and shortcut key transitions to `%APPDATA%\Whisper Anywhere\hotkey.log`.
- Surfaces current hotkey status and hook errors in the UI.

Audio recorder:

- Enumerates microphones with `sounddevice.query_devices()`.
- Captures mono 16 kHz PCM with `sounddevice.InputStream` callback.
- Computes RMS input level for the UI meter.
- Writes a temporary WAV file and deletes it after transcription by default.

Model manager:

- Lists `tiny`, `base`, `small`, `medium`, `large`, `large-v2`, `large-v3`, and `turbo`.
- Accepts `large-v3-turbo` as an alias for `turbo`.
- Tracks per-model readiness with local marker files.
- Model cards show Installed/Download, size, speed, accuracy, resource estimate, and recommended use.

Transcriber:

- Uses `faster_whisper.WhisperModel`.
- Resolves device, compute precision, and CPU thread options from settings.
- Auto uses CUDA when compatible GPU runtime is available, otherwise CPU.
- CPU only forces CPU.
- GPU preferred tries CUDA and falls back to CPU when enabled.
- Unsupported compute precision switches to a safe compatible option and reports a friendly message.

Text insertion engine:

- Clipboard paste is the default for reliability across browsers, terminals, Word, Discord, ChatGPT, and search boxes.
- Simulated Unicode typing remains available.
- Restores previous plain-text clipboard when enabled.

Tray manager:

- Uses `QSystemTrayIcon` with Start listening, Stop listening, model submenu, Open app, Settings, and Exit.

## Performance / Hardware Controls

Performance Mode:

- Fast: favors `base`, automatic hardware, automatic compute.
- Balanced: default, favors `base`, automatic hardware, automatic compute.
- Accurate: favors `medium`, automatic hardware, automatic compute.
- Low RAM Mode: favors `base`, CPU only, `int8` compute.
- Low VRAM Mode: favors `small`, automatic hardware, `int8_float16` compute.

Hardware:

- Auto: chooses GPU if compatible CUDA is available, otherwise CPU.
- CPU Only: forces faster-whisper to use CPU.
- GPU Preferred: tries CUDA first, then falls back to CPU if `fallback_to_cpu` is enabled.

Memory and fallback checkboxes:

- Low RAM Mode
- Low VRAM Mode
- Fall back to CPU if GPU fails
- Warn before loading large models
- Auto-download missing models
- Use GPU if available

Advanced:

- Compute precision: Auto, int8, int8_float16, float16, float32
- CPU threads: Auto or manual thread count

## Settings JSON Shape

```json
{
  "selected_model": "base",
  "microphone_device": "default",
  "shortcut": "Ctrl+Win+Space",
  "shortcut_mode": "hold",
  "hotkey_default_version": 2,
  "cancel_shortcut": "Esc",
  "insert_method": "clipboard_paste",
  "restore_clipboard": true,
  "device": "auto",
  "compute_type": "auto",
  "performance_preset": "balanced",
  "use_gpu_if_available": true,
  "fallback_to_cpu": true,
  "low_ram_mode": false,
  "low_vram_mode": false,
  "warn_before_large_models": true,
  "cpu_threads": "auto",
  "auto_download_models": true,
  "auto_punctuation": false,
  "auto_capitalization": false,
  "add_space_after_text": false,
  "press_enter_after_text": false,
  "typing_delay_ms": 80,
  "start_with_windows": false,
  "minimize_to_tray": true,
  "show_overlay": false,
  "save_history": false,
  "model_path": "%LOCALAPPDATA%\\Whisper Anywhere\\models",
  "language_mode": "auto",
  "forced_language": "",
  "translate_to_english": false,
  "delete_temp_audio": true,
  "debug_logs": false
}
```

## V1 Scope

Included now:

- Global Ctrl+Win+Space hold-to-talk
- Toggle mode option
- Change shortcut UI with conflict warnings
- Microphone selection and level meter
- Local faster-whisper transcription
- Model cards and current model indicator
- Performance / Hardware section
- Clipboard paste and simulated typing insertion
- System tray with Open, Settings, model submenu, Exit
- Settings JSON
- Automatic local folders and model preparation/download path
- README, setup, build instructions, troubleshooting
- Hotkey diagnostic test helper for source and EXE comparisons

Still later:

- Floating overlay
- History tab
- File transcription tab
- SRT output
- Configurable command replacement UI
- Full rich clipboard preservation
- Better VAD with Silero/webrtcvad

## Key Risks

Ctrl+Win as only modifiers:

- It is possible with a low-level keyboard hook, but it is less standard and less reliable than Ctrl+Win+Space.
- Windows shell behavior can still be sensitive to key order and can open Start before a clean hold/release sequence reaches the app.
- The Settings UI keeps Ctrl+Win available for testing, but Ctrl+Win+Space is the default and recommended shortcut.
- Hotkey diagnostics record whether the hook installed and which shortcut key transitions were observed.

Typing into elevated apps:

- Windows UIPI may block a normal app from typing into elevated/admin windows.
- The app should suggest running Whisper Anywhere at the same permission level as the target app.

Clipboard restore:

- V1 restores plain text clipboard contents only.
- Full rich clipboard restore is later.

GPU support:

- faster-whisper GPU requires compatible CUDA/cuBLAS/cuDNN/ctranslate2 runtime.
- Auto and fallback settings should keep voice typing usable on CPU.

Large models:

- Large models need significant RAM/VRAM and disk space.
- V1 warns before loading large models when enabled.

## Testing Plan

Manual Windows 11 flows:

- Notepad: hold shortcut, speak, release, paste text.
- PowerShell: hold shortcut, speak, release, paste text.
- Browser/ChatGPT: paste into focused message box.
- Word: paste into document.
- Discord: paste into chat box.
- System tray: minimize, start/stop, model selection, exit.
- Settings: change shortcut, model, hardware mode, compute precision, restart app, verify persistence.
- Hardware: Auto CPU path, CPU only path, GPU preferred with CPU fallback.
- Microphone: default device, changed device, no microphone error.

## Build Plan

Development run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python .\src\main.py
```

EXE build:

```powershell
.\scripts\build_exe.ps1
```

Expected output:

```text
dist\Whisper Anywhere\Whisper Anywhere.exe
```

Use `--onedir` first because faster-whisper, ctranslate2, and Qt dependencies are large. Whisper model files are downloaded/prepared by the app on first use instead of bundled into the EXE.

## References

- faster-whisper: https://github.com/SYSTRAN/faster-whisper
- PySide6 QSystemTrayIcon: https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QSystemTrayIcon.html
- Microsoft RegisterHotKey: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey
- Microsoft SendInput: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
- Python sounddevice: https://python-sounddevice.readthedocs.io/
- PyInstaller: https://pyinstaller.org/
