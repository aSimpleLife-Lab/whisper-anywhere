# Whisper Anywhere Implementation Plan

## Goal

Build a Windows 11 desktop app that lets the user click into any text field, hold a global shortcut, speak, release, and have locally transcribed Whisper text inserted into the focused app.

Version 1 focuses on the reliable core: global shortcut, microphone recording, local Whisper transcription, typing into the active cursor location, model selection, shortcut editing, settings, and tray behavior.

## Chosen Stack

Use Python for the first Windows version.

- UI: PySide6 / Qt for Python
- Global hotkeys: Win32 low-level keyboard hook through `ctypes` plus selected `pywin32` helpers
- Microphone capture: `sounddevice` plus `numpy`, saved as temporary WAV with Python `wave`
- Transcription: `faster-whisper`
- Text insertion: Win32 `SendInput` plus clipboard APIs through `pywin32` / `ctypes`
- Tray: PySide6 `QSystemTrayIcon`
- Settings/history/logs: local JSON and SQLite/plain log files under `%APPDATA%` / `%LOCALAPPDATA%`
- Packaging: PyInstaller on Windows

This stack is the fastest practical path for a beginner-friendly Windows app while keeping local Whisper, tray support, and future backend swapping possible.

## Exact Windows APIs

Global shortcut and hold-to-talk:

- `SetWindowsHookExW(WH_KEYBOARD_LL, ...)`
- `CallNextHookEx`
- `UnhookWindowsHookEx`
- `GetMessageW`, `TranslateMessage`, `DispatchMessageW`
- Virtual-key tracking for `VK_CONTROL`, `VK_LCONTROL`, `VK_RCONTROL`, `VK_LWIN`, `VK_RWIN`, `VK_ESCAPE`, plus user-selected keys
- Optional conflict probe for non-modifier shortcuts with `RegisterHotKey` / `UnregisterHotKey`

Focused app and insertion:

- `GetForegroundWindow` to remember the target window at listening start
- `GetWindowThreadProcessId` for diagnostics
- `SetForegroundWindow` only as a fallback when the target loses focus
- `SendInput` for simulated Unicode typing and Ctrl+V paste
- Clipboard APIs for paste mode: `OpenClipboard`, `EmptyClipboard`, `GetClipboardData`, `SetClipboardData`, `CloseClipboard`, `GlobalAlloc`, `GlobalLock`, `GlobalUnlock`, `CF_UNICODETEXT`

Start with Windows:

- Python `winreg` writing HKCU `Software\Microsoft\Windows\CurrentVersion\Run`

## Python Dependencies

Core runtime:

- `PySide6`
- `faster-whisper`
- `sounddevice`
- `numpy`
- `pywin32`
- `platformdirs`
- `pydantic` or dataclasses plus manual JSON validation

Build/dev:

- `pyinstaller`
- `pytest`
- `ruff`

Optional later:

- `webrtcvad` or Silero VAD for stronger voice activity detection
- `onnxruntime` for a local VAD model
- `pynput` only as a fallback hotkey backend if the custom hook fails on a machine

## Proposed File Structure

```text
whisper-app/
  README.md
  requirements.txt
  requirements-dev.txt
  pyproject.toml
  whisper_anywhere.spec
  example_config.json
  src/
    whisper_anywhere/
      __init__.py
      __main__.py
      app.py
      constants.py
      paths.py
      models.py
      ui/
        main_window.py
        settings_window.py
        model_card.py
        shortcut_editor.py
        overlay.py
        tray.py
        styles.qss
      core/
        app_state.py
        events.py
        hotkeys.py
        audio_recorder.py
        transcriber.py
        text_inserter.py
        command_replacements.py
        model_manager.py
        settings_manager.py
        history_manager.py
        startup_manager.py
        logger.py
      windows/
        keyboard_hook.py
        send_input.py
        clipboard.py
        foreground.py
      assets/
        icon.ico
        sounds/
          start.wav
          complete.wav
  tests/
    test_command_replacements.py
    test_settings_manager.py
    test_text_formatting.py
    test_shortcut_parser.py
```

## Core Architecture

UI layer:

- Displays status, model cards, microphone picker, shortcut reminder, and settings.
- Never steals focus during hotkey recording.
- Uses Qt signals to receive state updates from background workers.

Hotkey manager:

- Runs a Win32 low-level keyboard hook on a dedicated thread.
- Tracks pressed keys for hold-to-talk.
- Supports toggle mode and Esc cancel.
- Emits `start_listening`, `stop_listening`, and `cancel_listening` events.

Audio recorder:

- Enumerates microphones with `sounddevice.query_devices()`.
- Captures mono 16 kHz PCM with `sounddevice.InputStream` callback.
- Computes RMS level for meter and silence detection.
- Writes a temporary WAV file, then deletes it after transcription unless debug mode keeps it.

Transcriber:

- Uses a swappable `TranscriptionEngine` interface.
- V1 engine is `faster_whisper.WhisperModel`.
- Loads selected model lazily and reuses it.
- Default model: `medium`, with user-selectable `tiny`, `base`, `small`, `medium`, `large`, `large-v2`, `large-v3`, `turbo`.
- Uses CPU by default with int8 compute; GPU mode can use CUDA when available.

Text insertion engine:

- Applies optional command replacements and formatting.
- Clipboard paste is default for reliability across browsers, terminals, Word, Discord, and ChatGPT.
- Simulated Unicode typing is available for users who do not want clipboard changes.
- Restores previous plain-text clipboard when enabled.

Settings manager:

- Stores readable JSON at `%APPDATA%\Whisper Anywhere\settings.json`.
- Stores models under `%LOCALAPPDATA%\Whisper Anywhere\models` unless changed.
- Validates settings and falls back to safe defaults.

Tray manager:

- Uses `QSystemTrayIcon` with menu items for Start listening, Stop listening, model submenu, Open app, Settings, and Exit.

## Settings JSON Shape

```json
{
  "selected_model": "medium",
  "microphone_device": "default",
  "shortcut": "Ctrl+Win",
  "shortcut_mode": "hold",
  "cancel_shortcut": "Esc",
  "insert_method": "clipboard_paste",
  "restore_clipboard": true,
  "auto_punctuation": true,
  "auto_capitalization": true,
  "add_space_after_text": true,
  "press_enter_after_text": false,
  "typing_delay_ms": 80,
  "start_with_windows": false,
  "minimize_to_tray": true,
  "show_overlay": false,
  "save_history": false,
  "model_path": "%LOCALAPPDATA%\\Whisper Anywhere\\models",
  "use_gpu": false,
  "language_mode": "auto",
  "forced_language": "",
  "translate_to_english": false,
  "delete_temp_audio": true,
  "debug_logs": false,
  "command_replacements_enabled": true
}
```

## UI Plan

Main window:

- Dark Windows utility style.
- Header: Whisper Anywhere, Speech to Text - Type Anywhere, Settings, Minimize to tray.
- Main status panel: big microphone button, status text, input meter/waveform, shortcut reminder.
- Model cards: tiny, base, small, medium, large, large-v2, large-v3, turbo.
- How it works section with the four beginner steps.
- Bottom status bar: Ready, current model, current microphone, current shortcut.

Settings window:

- General, Shortcut, Whisper, Typing, Privacy, Advanced sections.
- V1 implements the core toggles and stores placeholders for later settings.

Floating overlay:

- Optional after the core is stable.
- Must be frameless/topmost/tool window and avoid activation so it does not steal focus.

## V1 Scope

Build now:

- Global Ctrl+Win hold-to-talk
- Toggle mode option
- Change shortcut UI with conflict warnings
- Microphone selection and level meter
- Local faster-whisper transcription
- Model cards and current model indicator
- Clipboard paste and simulated typing insertion
- Formatting options: auto-capitalize, punctuation, space, Enter, typing delay
- System tray with Open, Settings, model submenu, Exit
- Settings JSON
- Start with Windows toggle
- README, setup, build instructions, troubleshooting

Later:

- Floating overlay
- History tab
- File transcription tab
- SRT output
- Configurable command replacement UI
- Full rich clipboard preservation
- Better VAD with Silero/webrtcvad

## Key Risks

Ctrl+Win as only modifiers:

- It is possible with a low-level keyboard hook, but it is less standard than Ctrl+Win+Space.
- The Windows key can open the Start menu if not suppressed correctly.
- The app should warn and allow changing the shortcut immediately.

Typing into elevated apps:

- Windows UIPI may block a normal app from typing into elevated/admin windows.
- The app should show a friendly message: run Whisper Anywhere as administrator or type into a non-elevated target.

Clipboard restore:

- V1 can safely restore plain text clipboard contents.
- Full rich clipboard restore is more complex and should be later.

GPU support:

- faster-whisper GPU requires compatible CUDA/cuBLAS/cuDNN. CPU int8 should be the default.

Large models:

- Large models need significant RAM/VRAM and disk space.
- The app must show model download/loading errors clearly.

Focus loss:

- The overlay and main app must not take focus during recording.
- The app should remember the foreground window at recording start and warn if focus changed before insertion.

Antivirus/SmartScreen:

- Global keyboard hooks and input simulation can look suspicious to security software.
- Keep code transparent, local-only by default, and document why these APIs are used.

## Testing Plan

Manual Windows 11 flows:

- Notepad: hold shortcut, speak, release, paste text.
- PowerShell: say `ipconfig slash all`, verify command replacement becomes `ipconfig /all`.
- Browser/ChatGPT: paste into focused message box.
- Word: paste into document.
- Discord: paste into chat box.
- System tray: minimize, start/stop, exit.
- Settings: change shortcut, restart app, verify persistence.
- Microphone: default device, changed device, no microphone error.

Automated tests:

- Settings load/save/default migration.
- Shortcut parser and conflict warning list.
- Command replacements.
- Text formatting.
- Model metadata.

## Build Plan

Development run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m whisper_anywhere
```

EXE build:

```powershell
pip install -r requirements-dev.txt
pyinstaller whisper_anywhere.spec
```

Expected output:

```text
dist/Whisper Anywhere/Whisper Anywhere.exe
```

Use `--onedir` first because faster-whisper and Qt dependencies are large. Consider `--onefile` later only after startup time and antivirus behavior are acceptable.

## Documentation Deliverables

- README.md with beginner setup and run steps
- INSTALL_WINDOWS.md
- BUILD_EXE.md
- TROUBLESHOOTING.md
- Example config file
- Privacy note explaining local-only default

## References

- faster-whisper: https://github.com/SYSTRAN/faster-whisper
- PySide6 QSystemTrayIcon: https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QSystemTrayIcon.html
- Microsoft RegisterHotKey: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey
- Microsoft SendInput: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
- Python sounddevice: https://python-sounddevice.readthedocs.io/
- PyInstaller: https://pyinstaller.org/
- Python winreg: https://docs.python.org/3/library/winreg.html
