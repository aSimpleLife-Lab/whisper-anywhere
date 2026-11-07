from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never a real window

import pytest
import win32api
import win32con
import win32event
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

import core.hotkey_helper as helper_module
from core import app_log
from core.hotkey_helper import HotkeyHelper

SRC = Path(__file__).resolve().parents[1] / "src"
# Stand-ins for the helper process: plain Python that hooks nothing.
ECHO = (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    command = json.loads(line)\n"
    "    print(json.dumps(['status', ' '.join(map(str, command))]), flush=True)\n"
    "    if command == ['start']:\n"
    "        for event in (['pressed'], ['released', 42], ['logged', 'hotkey: said by the helper']):\n"
    "            print(json.dumps(event), flush=True)\n"
)
CRASHES_ONCE = (  # the first copy dies mid-hold; the next one answers
    "import json, os, sys\n"
    "first = not os.path.exists(sys.argv[1])\n"
    "open(sys.argv[1], 'a').close()\n"
    "for line in sys.stdin:\n"
    "    command = json.loads(line)\n"
    "    print(json.dumps(['status', ' '.join(map(str, command))]), flush=True)\n"
    "    if command == ['start'] and first:\n"
    "        print(json.dumps(['pressed']), flush=True)\n"
    "        sys.stderr.write('helper broke')\n"
    "        sys.exit(1)\n"
)
# What the app tells every new helper, in order.
STATE_M3 = ["update_shortcut M3 hold", "update_retype_shortcut ", "set_escape_held_back False", "start"]


@pytest.fixture(scope="module")
def app() -> QCoreApplication:
    return QApplication.instance() or QApplication(["test", "-platform", "offscreen"])


def wait_until(condition, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QCoreApplication.processEvents()
        time.sleep(0.01)


def recorder(hotkey: HotkeyHelper) -> list[str]:
    seen: list[str] = []
    hotkey.status.connect(seen.append)
    hotkey.error.connect(lambda message: seen.append(f"error: {message}"))
    for name in ("pressed", "cancelled", "timed_out", "tapped", "retype_requested"):
        getattr(hotkey, name).connect(lambda name=name: seen.append(name))
    hotkey.released.connect(lambda hwnd: seen.append(f"released {hwnd}"))
    return seen


def test_the_app_sends_its_state_to_the_helper_and_relays_what_it_says(app) -> None:
    # The hooks live in a helper process now, so a model load holding the
    # app's GIL no longer holds back every key and click on the PC.
    hotkey = HotkeyHelper("M3", "hold", command=[sys.executable, "-c", ECHO])
    seen = recorder(hotkey)
    hotkey.update_retype_shortcut("Ctrl+Alt+V")
    hotkey.set_escape_held_back(True)
    hotkey.start()
    wait_until(lambda: "hotkey: said by the helper" in "\n".join(app_log.recent(5)))
    assert seen == [
        "update_shortcut M3 hold",
        "update_retype_shortcut Ctrl+Alt+V",
        "set_escape_held_back True",
        "start",
        "pressed",
        "released 42",
    ]
    hotkey.update_shortcut("M4", "toggle")
    hotkey.rehook()
    wait_until(lambda: "rehook" in seen)
    assert seen[-2:] == ["update_shortcut M4 toggle", "rehook"]
    with pytest.raises(ValueError):
        hotkey.update_retype_shortcut("M5")  # keyboard only, refused at once as before
    hotkey.stop()


def test_the_helper_process_takes_commands_and_exits_when_its_input_closes() -> None:
    # The real helper, sent no "start": nothing is hooked here.
    helper = subprocess.run(
        helper_module.helper_command(),
        input=b'["update_shortcut", "Ctrl+Alt+F12", "hold"]\n',
        capture_output=True,
        timeout=60,
    )

    events = [json.loads(line) for line in helper.stdout.splitlines()]
    assert ["status", "Shortcut active: Ctrl+Alt+F12 (hold)."] in events, helper.stderr[-2000:]
    assert helper.returncode == 0 and b"Traceback" not in helper.stderr


def test_the_helper_skips_the_apps_own_imports() -> None:
    # It is the app's own exe, started with a flag, but it needs only Qt's
    # core and pynput: no numpy, widgets or speech engines.
    probe = (
        "import atexit, sys\n"
        "heavy = ('numpy', 'PySide6.QtWidgets', 'core.transcriber', 'ui.main_window')\n"
        "atexit.register(lambda: print('loaded', [m for m in heavy if m in sys.modules], file=sys.stderr))\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "sys.argv = ['main.py', '--hotkey-helper']\n"
        "import main\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], input=b"", capture_output=True, timeout=60)

    assert b"loaded []" in result.stderr, result.stderr[-2000:]


def test_the_helper_dies_with_the_app(tmp_path) -> None:
    # A helper left behind by a crashed app would keep M3 (and, mid-recording,
    # Esc) from every app with nothing to act on them. This one even ignores
    # its closed input, so only the app's job object can end it.
    pid_file = tmp_path / "helper.pid"
    sleeper = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(120)"
    crashing_app = (
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "from core.hotkey_helper import HotkeyHelper\n"
        "app = QCoreApplication([])\n"
        f"command = [sys.executable, '-c', {sleeper!r}, {str(pid_file)!r}]\n"
        "HotkeyHelper('Ctrl+Alt+F12', 'hold', command=command).start()\n"
        "print(os.getpid(), flush=True)\n"
        "time.sleep(120)\n"
    )
    app_process = subprocess.Popen([sys.executable, "-c", crashing_app], stdout=subprocess.PIPE)
    helper_pid = 0
    try:
        app_pid = int(app_process.stdout.readline())
        deadline = time.monotonic() + 30
        while not helper_pid:
            assert time.monotonic() < deadline, "the helper never started"
            time.sleep(0.05)
            helper_pid = int(pid_file.read_text() or 0) if pid_file.exists() else 0
        helper = win32api.OpenProcess(win32con.SYNCHRONIZE, False, helper_pid)

        os.kill(app_pid, signal.SIGTERM)  # TerminateProcess: a crash, no clean-up runs

        assert win32event.WaitForSingleObject(helper, 10000) == win32event.WAIT_OBJECT_0
    finally:
        app_process.kill()
        if helper_pid:
            try:
                os.kill(helper_pid, signal.SIGTERM)
            except OSError:
                pass  # gone, as it should be


def test_a_helper_that_dies_mid_hold_ends_the_hold_and_comes_back(app, tmp_path, monkeypatch) -> None:
    # Its watchdog dies with it: the recording stops untyped (timed_out),
    # and a new helper gets the app's state.
    monkeypatch.setattr(helper_module, "RESTART_AFTER_SECONDS", 0.0)
    hotkey = HotkeyHelper("M3", "hold", command=[sys.executable, "-c", CRASHES_ONCE, str(tmp_path / "ran")])
    seen = recorder(hotkey)
    hotkey.start()
    wait_until(lambda: seen.count("start") == 2)

    assert seen == STATE_M3 + ["pressed", "timed_out"] + STATE_M3
    assert "helper broke" in "\n".join(app_log.recent(20))  # its last words are kept
    hotkey.stop()


def test_a_helper_that_keeps_dying_is_reported_instead_of_restarted(app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(helper_module, "RESTART_AFTER_SECONDS", 60.0)
    launches = tmp_path / "launches"
    dies_at_once = f"open({str(launches)!r}, 'a').write('x'); raise SystemExit(1)"
    hotkey = HotkeyHelper("Ctrl+Alt+F12", "hold", command=[sys.executable, "-c", dies_at_once])
    seen = recorder(hotkey)
    hotkey.start()
    wait_until(lambda: any(item.startswith("error: ") for item in seen))
    assert launches.read_text() == "x"  # no restart loop

    hotkey.rehook()  # unlock or resume tries once more
    wait_until(lambda: launches.read_text() == "xx")
    hotkey.stop()
