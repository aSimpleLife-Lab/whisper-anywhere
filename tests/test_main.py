from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock

import main
from core import app_log


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value) -> None:
        self[key] = value


def test_a_bad_saved_shortcut_falls_back_instead_of_crashing() -> None:
    # An imported 'Ctrl+F13' stopped the app at every sign-in.
    settings = FakeSettings(shortcut="Ctrl+F13")

    assert main.saved_shortcut(settings) == "Ctrl+Alt+Q"
    assert settings["shortcut"] == "Ctrl+Alt+Q"
    assert main.saved_shortcut(FakeSettings(shortcut="M3")) == "M3"


def test_the_retype_shortcut_is_checked_the_same_way_and_blank_stays_off() -> None:
    settings = FakeSettings(retype_shortcut="Ctrl+F13")

    assert main.saved_retype_shortcut(settings) == "Ctrl+Alt+V"
    assert settings["retype_shortcut"] == "Ctrl+Alt+V"
    assert main.saved_retype_shortcut(FakeSettings(retype_shortcut="")) == ""
    assert main.saved_retype_shortcut(FakeSettings(retype_shortcut="Ctrl+Shift+F9")) == "Ctrl+Shift+F9"


def test_a_saved_shortcut_that_cannot_work_falls_back_too() -> None:
    # B18: a saved Ctrl+Alt+Esc cancelled itself on every press, and a mouse
    # retype shortcut is refused by the listener.
    settings = FakeSettings(shortcut="Ctrl+Alt+Esc", retype_shortcut="M5")

    assert main.saved_shortcut(settings) == "Ctrl+Alt+Q"
    assert main.saved_retype_shortcut(settings) == "Ctrl+Alt+V"
    assert (settings["shortcut"], settings["retype_shortcut"]) == ("Ctrl+Alt+Q", "Ctrl+Alt+V")


def test_a_saved_tab_stays_the_dictation_shortcut_but_not_the_retype_one() -> None:
    settings = FakeSettings(shortcut="Tab", retype_shortcut="Tab")

    assert main.saved_shortcut(settings) == "Tab"
    assert main.saved_retype_shortcut(settings) == "Ctrl+Alt+V"


def test_only_one_copy_runs() -> None:
    # Two copies each recorded and pasted every dictation.
    name = f"WhisperAnywhereTest-{os.getpid()}"

    assert not main.another_copy_running(name)
    assert main.another_copy_running(name)


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def test_a_copy_that_may_not_open_the_mutex_still_counts_as_running() -> None:
    # B17: a copy started as administrator owns a mutex a normal copy may not
    # open; CreateMutexW then fails with ERROR_ACCESS_DENIED (5), not 183, and
    # a second copy started. A DACL granting nobody access stands in for it.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    descriptor = ctypes.c_void_p()
    assert advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:", 1, ctypes.byref(descriptor), None)
    attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
    name = f"WhisperAnywhereTest-denied-{os.getpid()}"
    first_copy = kernel32.CreateMutexW(ctypes.byref(attributes), False, f"Local\\{name}")
    assert first_copy, ctypes.get_last_error()
    try:
        assert main.another_copy_running(name)
    finally:
        kernel32.CloseHandle(first_copy)


def test_a_failing_start_up_step_is_logged_and_the_start_goes_on() -> None:
    # B8/B9: an unreadable legacy log or a registry error raised out of main().
    problems: list[str] = []

    def held_open() -> None:
        raise PermissionError("held by another program")

    assert main.start_up_step(problems, "Removing old logs", held_open, "fallback") == "fallback"
    assert main.start_up_step(problems, "Shortcut", lambda: "M3") == "M3"
    assert problems == ["Removing old logs: held by another program"]
    assert "Removing old logs failed: PermissionError" in app_log.recent(1)[0]


def test_openblas_is_held_to_one_thread_before_numpy_loads() -> None:
    # PF-4: numpy's OpenBLAS started a thread per core at import: 23 idle
    # threads and 747 MB committed for nothing.
    probe = (
        "import os, sys, types\n"
        "seen = []\n"
        "class Spy:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'numpy' and not seen:\n"
        "            seen.append(os.environ.get('OPENBLAS_NUM_THREADS'))\n"
        "        return None\n"
        "sys.meta_path.insert(0, Spy())\n"
        "sys.modules['sounddevice'] = types.ModuleType('sounddevice')  # no PortAudio\n"
        f"sys.path.insert(0, {str(Path(main.__file__).parent)!r})\n"
        "import main\n"
        "print(seen)\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "OPENBLAS_NUM_THREADS"}
    env["QT_QPA_PLATFORM"] = "offscreen"

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=120
    )

    assert result.stdout.strip() == "['1']", result.stderr[-2000:]


def test_cue_sounds_use_windows_own_audio_backend() -> None:
    # Qt's default FFmpeg backend loaded five codec DLLs (19 MB of the build)
    # to play four WAV cues. The installer build no longer ships them.
    probe = (
        "import os, sys, types\n"
        "sys.modules['sounddevice'] = types.ModuleType('sounddevice')  # no PortAudio\n"
        f"sys.path.insert(0, {str(Path(main.__file__).parent)!r})\n"
        "import main\n"
        "print(os.environ.get('QT_MEDIA_BACKEND'))\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "QT_MEDIA_BACKEND"}
    env["QT_QPA_PLATFORM"] = "offscreen"

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=120
    )

    assert result.stdout.strip() == "windows", result.stderr[-2000:]


def _run_main(monkeypatch, minimized: bool) -> tuple[list[str], MagicMock]:
    """main() with every part replaced: what it starts, and in which order."""
    order: list[str] = []
    saved = {"shortcut": "Ctrl+Alt+Q", "retype_shortcut": "", "debug_logs": False}
    settings = MagicMock(load_notice="", model_folder_error="")
    settings.get.side_effect = lambda key, default=None: saved.get(key, default)
    helper = MagicMock()
    helper.start.side_effect = lambda: order.append("helper started")
    window = MagicMock()

    def build_window(*parts) -> MagicMock:
        order.append("window built")
        return window

    for name in (
        "QApplication",
        "StartupManager",
        "ModelManager",
        "AudioRecorder",
        "Transcriber",
        "TextInserter",
        "FeedbackPlayer",
        "TrayManager",
        "QLocalServer",
        "QTimer",
        "app_icon",
        "set_windows_app_id",
    ):
        monkeypatch.setattr(main, name, MagicMock())
    monkeypatch.setattr(main, "another_copy_running", lambda: False)
    monkeypatch.setattr(main, "should_start_minimized", lambda: minimized)
    monkeypatch.setattr(main, "SettingsManager", lambda: settings)
    monkeypatch.setattr(main.app_log, "destroy_legacy_files", lambda *args: None)  # never this PC's files
    monkeypatch.setattr(main, "HotkeyHelper", lambda shortcut, mode: helper)
    monkeypatch.setattr(main, "MainWindow", build_window)

    main.main()
    return order, window


def test_the_shortcut_helper_starts_before_the_window_is_built(monkeypatch) -> None:
    # The helper process takes about a second to boot. Started last, the
    # shortcut came alive a second after everything else was ready.
    order, _window = _run_main(monkeypatch, minimized=False)

    assert order == ["helper started", "window built"]


def test_a_start_in_the_tray_loads_the_model_without_the_wait(monkeypatch) -> None:
    _order, hidden = _run_main(monkeypatch, minimized=True)
    hidden.warm_up_at_once.assert_called_once_with()

    _order, shown = _run_main(monkeypatch, minimized=False)
    shown.warm_up_at_once.assert_not_called()  # an opening window stays smooth first


def test_a_retype_shortcut_equal_to_the_dictation_shortcut_turns_off() -> None:
    # One press emitted both: it re-sent the last message instead of recording.
    settings = FakeSettings(shortcut="Ctrl+Alt+V", retype_shortcut="Ctrl+Alt+V")

    assert main.saved_retype_shortcut(settings) == ""
    assert settings["retype_shortcut"] == ""
