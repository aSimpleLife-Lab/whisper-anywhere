# Whisper Anywhere

Whisper Anywhere is a native Windows 11 desktop app for local Whisper voice typing.

Click in any app, hold `Ctrl + Win`, speak, release, and Whisper Anywhere transcribes your speech locally and pastes the text into the focused Windows app.

This is not a web app and it does not use a cloud API in V1.

## V1 Features

- PySide6 Windows desktop window
- System tray support
- Global hold-to-talk shortcut: `Ctrl + Win`
- Optional toggle shortcut mode
- Shortcut settings UI
- Microphone selection
- Local settings file at `%APPDATA%\Whisper Anywhere\settings.json`
- Whisper model selector: `tiny`, `base`, `small`, `medium`, `large`, `large-v2`, `large-v3`, `turbo`
- Local transcription with `faster-whisper`
- Clipboard paste insertion into the focused Windows app
- Restore previous plain-text clipboard after paste
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
6. Hold `Ctrl + Win`.
7. Speak.
8. Release `Ctrl + Win`.
9. Text appears where the cursor was.

## Developer Setup From Source

These steps are only for building or developing the app from the GitHub source code. End users should use the built EXE.

### 1. Clone the repo

```powershell
git clone https://github.com/aSimpleLife-Lab/whisper-app.git
cd whisper-app
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
5. Hold `Ctrl` first, then hold `Win`.
6. Speak a short sentence.
7. Release `Ctrl + Win`.
8. The transcribed text should paste into the app you clicked.

If the Start menu opens, press `Ctrl` first and then `Win`. You can also change the shortcut in the app.

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

## V1 Limitations

- No history page yet.
- No file transcription tab yet.
- No cloud API mode yet.
- No command replacements yet, so saying `slash` will transcribe as the word `slash` for now.
- Clipboard restore preserves previous plain text only in V1, not rich clipboard formats like images or Word formatting.
- Typing into elevated Administrator apps may require running Whisper Anywhere as Administrator too.
- The first model setup can take time and needs internet access.
- GPU mode exists in settings storage but V1 defaults to CPU for reliability.

## Troubleshooting

### No microphone found

Check Windows Settings > Privacy & security > Microphone and make sure desktop apps can access the microphone.

### Shortcut does not start recording

Restart the app. If it still fails, change the shortcut in the app. Some security software may block global keyboard hooks.

### Text does not appear in the target app

Try the default clipboard paste mode. If the target app is running as Administrator, run Whisper Anywhere as Administrator too.

### Model download fails

Check internet access, available disk space, and permissions for `%LOCALAPPDATA%\Whisper Anywhere\models`.
