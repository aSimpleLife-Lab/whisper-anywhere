from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import ui.main_window as mw


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value) -> None:
        self[key] = value


def test_toggle_shortcut_starts_or_stops_by_the_windows_own_state() -> None:
    calls: list[str] = []
    window = SimpleNamespace(
        _is_listening=False,
        settings_manager=FakeSettings(shortcut_mode="toggle"),
        start_listening=lambda from_shortcut=False: calls.append("start"),
        stop_listening_and_transcribe=lambda: calls.append("stop"),
    )

    mw.MainWindow.shortcut_pressed(window)  # refused: still not listening...
    mw.MainWindow.shortcut_pressed(window)  # ...so this press starts again
    window._is_listening = True
    mw.MainWindow.shortcut_pressed(window)
    window.settings_manager["shortcut_mode"] = "hold"
    mw.MainWindow.shortcut_pressed(window)  # hold mode: the release stops it

    assert calls == ["start", "start", "stop", "start"]


def test_copy_keeps_the_dictation_out_of_clipboard_history() -> None:
    copied: list[tuple[str, bool]] = []
    window = SimpleNamespace(
        last_transcript=SimpleNamespace(toPlainText=lambda: "secret words"),
        text_inserter=SimpleNamespace(
            set_clipboard_text=lambda text, private=False: copied.append((text, private))
        ),
        set_status=lambda status, detail: None,
    )

    mw.MainWindow.copy_transcript(window)

    assert copied == [("secret words", True)]


def test_ctrl_c_in_the_transcript_box_is_private_too() -> None:
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    box = mw._PrivateTextEdit()
    box.setPlainText("secret words")
    box.selectAll()

    mime = box.createMimeDataFromSelection()

    assert mime.text() == "secret words"
    history = 'application/x-qt-windows-mime;value="CanIncludeInClipboardHistory"'
    assert bytes(mime.data(history)) == b"\0\0\0\0"


class FakeCombo:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.index = -1

    def blockSignals(self, blocked: bool) -> bool:
        return False

    def clear(self) -> None:
        self.items = []

    def addItem(self, name: str, data: str) -> None:
        self.items.append((name, data))

    def findData(self, data: str) -> int:
        return next((i for i, item in enumerate(self.items) if item[1] == data), -1)

    def setCurrentIndex(self, index: int) -> None:
        self.index = index


def test_a_missing_microphone_stays_the_saved_choice() -> None:
    # A headset that connects after logon was replaced by "default" for good,
    # and the frozen device list never showed it even after a refresh.
    settings = FakeSettings(microphone_device="7", microphone_name="Headset")
    rescans: list[bool] = []
    window = SimpleNamespace(
        settings_manager=settings,
        mic_combo=FakeCombo(),
        audio_recorder=SimpleNamespace(
            rescan=lambda: rescans.append(True),
            list_input_devices=lambda keep=None: [SimpleNamespace(id="default", name="Default")],
        ),
        _resolve_saved_microphone=lambda: "",
        _append_activity_log=lambda message: None,
        set_status=lambda status, detail: None,
        update_bottom_labels=lambda: None,
    )

    mw.MainWindow.refresh_microphones(window, rescan=True)

    assert rescans == [True]
    assert settings["microphone_device"] == "7"
    assert window.mic_combo.index == 0  # the default is used meanwhile


def test_a_download_still_arriving_is_not_called_too_slow(tmp_path) -> None:
    # A fixed 15-minute limit stopped slow but working downloads, and the next
    # try wrote the same file alongside them. Only a stall is a failure now.
    (tmp_path / "encoder.onnx.data.part").write_bytes(b"x" * 200)
    rearmed: list[int] = []
    ended: list[bool] = []
    window = SimpleNamespace(
        _active_model_prepare_id=1,
        _is_preparing_model=True,
        _model_prepare_bytes=100,
        model_manager=SimpleNamespace(model_storage_path=lambda: tmp_path),
        _start_model_prepare_timer=rearmed.append,
        _end_model_prepare=lambda: ended.append(True),
        _reset_transcriber=lambda: None,
        _append_activity_log=lambda message: None,
        _refresh_model_status=lambda: None,
        model_status=SimpleNamespace(setText=lambda text: None),
        set_status=lambda status, detail: None,
    )

    mw.MainWindow.handle_model_prepare_timeout(window, 1)
    assert (rearmed, ended) == ([1], [])

    window._model_prepare_bytes = 200  # nothing new since the timer was set
    mw.MainWindow.handle_model_prepare_timeout(window, 1)
    assert ended == [True]


def test_loading_settings_into_the_window_turns_the_shortcut_back_on() -> None:
    # Import and "Run setup again" show Dictation on, but a shortcut the user
    # had paused stayed off.
    window = MagicMock()

    mw.MainWindow._load_settings_into_ui(window)

    window.hotkey_enabled_changed.emit.assert_called_once_with(True)


def test_setup_keeps_the_chosen_microphone_and_toggle_mode(monkeypatch) -> None:
    # Setup saved only the device number, so the old microphone's name won it
    # straight back; it also switched a toggle user back to hold.
    class FakeDialog:
        def __init__(self, parent) -> None:
            self.shortcut_input = MagicMock()
            self.shortcut_input.text.return_value = "M3"
            self.model_combo = SimpleNamespace(currentData=lambda: "voz")
            self.mic_combo = SimpleNamespace(
                currentData=lambda: "2", currentText=lambda: "Blue Yeti"
            )
            self.device_combo = SimpleNamespace(currentData=lambda: "cpu")

        def exec(self):
            return mw.QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw, "FirstRunSetupDialog", FakeDialog)
    settings = FakeSettings(
        microphone_device="1",
        microphone_name="Jabra",
        shortcut_mode="toggle",
        device="cpu",
        compute_type="int8",
    )
    window = MagicMock()
    window.settings_manager = settings

    mw.MainWindow.show_first_run_setup(window, force=True)

    assert (settings["microphone_device"], settings["microphone_name"]) == ("2", "Blue Yeti")
    assert settings["shortcut_mode"] == "toggle"
    assert settings["compute_type"] == "int8"  # same hardware, same precision
    window.shortcut_changed.emit.assert_called_once_with("M3", "toggle")


def test_importing_settings_switches_the_debug_log_to_match(monkeypatch, tmp_path) -> None:
    # The import showed the debug log off while it kept writing to disk.
    written: list[object] = []
    shown: list[str] = []
    monkeypatch.setattr(mw.app_log, "write_to_disk", written.append)
    monkeypatch.setattr(mw, "show_message", lambda parent, title, text, *args: shown.append(text))
    exported = tmp_path / "settings.json"
    exported.write_text('{"debug_logs": false}', encoding="utf-8-sig")  # PowerShell 5.1 adds a BOM
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *args: (str(exported), ""))

    class ImportingSettings(FakeSettings):
        debug_log_path = tmp_path / "debug.log"

        def import_portable(self, values):
            self.update(values)
            return []

    window = MagicMock()
    window.settings_manager = ImportingSettings(debug_logs=True)

    mw.MainWindow.import_settings(window)

    assert shown == []
    assert written == [None]


def test_old_thread_counts_still_show_in_the_menu() -> None:
    # v0.1 offered 1 thread and counts above this PC's cores; the menu showed
    # Auto while the engine kept using the old count.
    threads = FakeCombo()
    threads.addItem("Auto", "auto")
    threads.addItem("2 threads", 2)
    window = MagicMock()
    window.cpu_threads_combo = threads
    window.compute_combo = FakeCombo()
    window.unload_combo = FakeCombo()
    window.settings_manager = FakeSettings(cpu_threads=1)
    window._select_combo_data = lambda combo, value: mw.MainWindow._select_combo_data(
        window, combo, value
    )

    mw.MainWindow._restore_performance_controls(window)

    assert threads.items[threads.index] == ("1 thread", 1)


# ------------------------------------------------------------- v0.2.6 flow


def _window(**values) -> MagicMock:
    window = MagicMock()
    for key, value in values.items():
        setattr(window, key, value)
    return window


def _no_threads(monkeypatch) -> list[tuple]:
    """Records worker threads and watchdog timers instead of starting them."""
    started: list[tuple] = []
    monkeypatch.setattr(
        mw.threading,
        "Thread",
        lambda target, args, daemon: SimpleNamespace(start=lambda: started.append(("thread", target, args))),
    )
    monkeypatch.setattr(
        mw.threading,
        "Timer",
        lambda seconds, function: SimpleNamespace(
            start=lambda: started.append(("timer", seconds)), cancel=lambda: None, daemon=True
        ),
    )
    return started


def test_turning_dictation_off_cancels_a_running_recording() -> None:
    # The microphone kept recording with no cap once Dictation went off, and
    # the first tap after turning it back on pasted and sent all of it.
    window = _window(_loading_ui=False, _is_listening=True)

    mw.MainWindow._dictation_toggled(window, False)

    window.cancel_listening.assert_called_once_with()


def test_turning_dictation_off_stops_typing_too() -> None:
    cancel = mw.threading.Event()
    window = _window(_loading_ui=False, _is_listening=False, _insert_cancel=cancel)

    mw.MainWindow._dictation_toggled(window, False)

    assert cancel.is_set()


def test_typing_keeps_the_window_busy_and_arms_a_watchdog(monkeypatch) -> None:
    # The status said "Typing" while the scene showed idle, and nothing timed
    # out an insert that never came back.
    started = _no_threads(monkeypatch)
    seen: list[tuple[str, bool]] = []
    window = _window(_is_transcribing=False, _insert_id=0, _recent_apps=[])
    window.set_status = lambda status, detail: seen.append((status, window._is_transcribing))
    window._settings_for = lambda target: {"send_key": "enter"}
    target = SimpleNamespace(window_hwnd=5, title="chat", process_name="discord.exe")

    mw.MainWindow._start_insert(window, "hello there", target)

    assert seen == [("Typing", True)]
    thread = next(item for item in started if item[0] == "thread")
    assert thread[2][1:4] == ("hello there", target, {"send_key": "enter"})
    assert any(item[0] == "timer" and item[1] > 0 for item in started)


def test_a_quick_tap_is_too_short_not_an_error(monkeypatch) -> None:
    # A tap of the shortcut said "Error: No microphone audio was captured."
    started = _no_threads(monkeypatch)
    seen: list[tuple[str, str]] = []
    window = _window(_is_listening=True)
    window.audio_recorder.stop.return_value = mw.np.zeros(800, dtype=mw.np.float32)  # 0.05 s
    window.set_status = lambda status, detail: seen.append((status, detail))

    mw.MainWindow.stop_listening_and_transcribe(window)

    assert started == []
    assert seen[-1][0] == "Ready" and "short" in seen[-1][1].lower()
    window.cue.emit.assert_not_called()


def test_a_shortcut_timeout_transcribes_but_types_nothing() -> None:
    # The 60 s watchdog typed the first part and pressed Enter while M3 was
    # still held.
    stops: list[bool] = []
    window = _window(_is_listening=True)
    window.stop_listening_and_transcribe = lambda release_window_hwnd=None, deliver=True: stops.append(deliver)

    mw.MainWindow.stop_after_timeout(window)

    assert stops == [False]


def test_an_undelivered_result_is_shown_and_kept_but_not_typed() -> None:
    window = _window(
        _active_transcription_id=4,
        _deliver=False,
        _undelivered_reason="timeout",
        _target_hwnd=SimpleNamespace(window_hwnd=1, title="chat", process_name="discord.exe"),
    )

    mw.MainWindow.finish_transcription(window, 4, "first part", 61.0, 1.0)

    window._start_insert.assert_not_called()
    window.last_transcript.setPlainText.assert_called_once_with("first part")
    window._remember_for_retype.assert_called_once_with("first part")
    window.cue.emit.assert_called_once_with("error")
    assert window._target_hwnd is None


def test_esc_while_it_transcribes_drops_the_typing() -> None:
    # A misspoken message could not be stopped once the shortcut was let go.
    window = _window(_is_listening=False, _active_transcription_id=7, _deliver=True, _active_insert_id=0)

    mw.MainWindow.cancel_listening(window)

    assert window._deliver is False
    assert window._undelivered_reason == "cancelled"
    window.cue.emit.assert_called_once_with("cancel")


def test_esc_while_it_types_stops_the_typing() -> None:
    cancel = mw.threading.Event()
    window = _window(_is_listening=False, _active_transcription_id=0, _active_insert_id=3, _insert_cancel=cancel)

    mw.MainWindow.cancel_listening(window)

    assert cancel.is_set()
    window.cue.emit.assert_called_once_with("cancel")


def test_esc_cancels_a_recording_with_a_sound() -> None:
    # Esc stopped the recording silently: no way to tell it worked.
    window = _window(_is_listening=True)

    mw.MainWindow.cancel_listening(window)

    window._set_listening.assert_called_once_with(False)
    window.audio_recorder.cancel.assert_called_once_with()
    window.cue.emit.assert_called_once_with("cancel")


def test_a_tapped_mouse_shortcut_drops_its_recording_quietly() -> None:
    # A short M3 tap stays a middle-click: the recording it opened goes away
    # with no sound and no status.
    window = _window(_is_listening=True)

    mw.MainWindow.drop_tapped_recording(window)

    window._set_listening.assert_called_once_with(False)
    window.audio_recorder.cancel.assert_called_once_with()
    window.cue.emit.assert_not_called()
    window.feedback.emit.assert_not_called()


def test_retype_types_the_last_dictation_into_the_app_in_front() -> None:
    target = SimpleNamespace(window_hwnd=9, title="general", process_name="discord.exe")
    window = _window(_retype_text="gg well played")
    window.text_inserter.get_foreground_window.return_value = target
    window._pipeline_busy = lambda: False

    mw.MainWindow.retype_last_dictation(window)

    window._start_insert.assert_called_once_with("gg well played", target)


def test_retype_without_a_recent_dictation_types_nothing() -> None:
    window = _window(_retype_text="")
    window._pipeline_busy = lambda: False

    mw.MainWindow.retype_last_dictation(window)

    window._start_insert.assert_not_called()


def test_an_app_profile_overrides_the_typing_settings() -> None:
    settings = {
        "press_enter_after_text": True,
        "auto_punctuation": True,
        "insert_method": "auto",
        "app_profiles": {
            "windowsterminal.exe": {"insert_method": "auto", "send_key": "none", "full_stop": "off"},
            "game.exe": {"insert_method": "type", "send_key": "default", "full_stop": "default"},
        },
    }
    window = _window(settings_manager=SimpleNamespace(all=lambda: dict(settings)))

    terminal = mw.MainWindow._settings_for(window, SimpleNamespace(process_name="windowsterminal.exe"))
    game = mw.MainWindow._settings_for(window, SimpleNamespace(process_name="game.exe"))
    other = mw.MainWindow._settings_for(window, SimpleNamespace(process_name="notepad.exe"))

    assert (terminal["send_key"], terminal["auto_punctuation"], terminal["insert_method"]) == ("none", False, "auto")
    assert (game["insert_method"], game["auto_punctuation"]) == ("type", True)
    assert "send_key" not in game and "send_key" not in other


def test_a_failed_insert_says_why_and_plays_the_error_cue() -> None:
    window = _window(_active_insert_id=2, _insert_timer=None)

    mw.MainWindow.finish_insertion(window, 2, "error", "The app did not take the paste.")

    assert window._is_transcribing is False
    window.cue.emit.assert_called_once_with("error")
    window.set_status.assert_called_once()
    assert window.set_status.call_args[0][0] == "Error"


def test_a_late_insert_result_is_ignored_after_the_watchdog() -> None:
    window = _window(_active_insert_id=0)

    mw.MainWindow.finish_insertion(window, 2, "done", "")

    window.set_status.assert_not_called()


def test_an_insert_that_hangs_is_cancelled() -> None:
    cancel = mw.threading.Event()
    window = _window(_active_insert_id=5, _insert_cancel=cancel)

    mw.MainWindow.handle_insert_timeout(window, 5)

    assert cancel.is_set()
    assert window._active_insert_id == 0 and window._is_transcribing is False
    window.cue.emit.assert_called_once_with("error")


def test_the_idle_unload_timer_follows_the_setting() -> None:
    timer = MagicMock()
    window = _window(_idle_unload_timer=timer, settings_manager=FakeSettings(unload_model_after="15"))

    mw.MainWindow._arm_idle_unload(window)
    timer.start.assert_called_once_with(15 * 60 * 1000)

    timer.reset_mock()
    window.settings_manager["unload_model_after"] = "never"
    mw.MainWindow._arm_idle_unload(window)
    timer.start.assert_not_called()
    timer.stop.assert_called_once_with()


def test_idle_unload_waits_while_the_app_is_busy(monkeypatch) -> None:
    started = _no_threads(monkeypatch)
    window = _window()
    window._pipeline_busy = lambda: True

    mw.MainWindow._idle_unload(window)

    assert started == []
    window._arm_idle_unload.assert_called_once_with()


def test_setup_and_tour_wait_until_the_window_is_shown(monkeypatch) -> None:
    # A first start with no settings.json popped setup up over a game on a
    # hidden sign-in start.
    shots: list[object] = []
    monkeypatch.setattr(mw.QTimer, "singleShot", lambda ms, function: shots.append(function))
    monkeypatch.setattr(mw.QMainWindow, "showEvent", lambda self, event: None)
    window = _window(_tutorial_pending=False, settings_manager=FakeSettings(first_run_setup_completed=False))

    mw.MainWindow.showEvent(window, None)

    assert shots == [window.show_first_run_setup]


def test_an_error_without_a_message_still_says_what_failed() -> None:
    class Silent(Exception):
        pass

    assert mw._error_text(Silent()) == "Silent"
    assert mw._error_text(ValueError("bad clip")) == "bad clip"


def test_picking_a_model_rechecks_busy_after_the_question() -> None:
    # A dictation started while the "Load a larger model?" dialog was open, and
    # the pick then swapped the engine under it.
    window = _window()
    busy = iter([False, True])
    window._pipeline_busy = lambda: next(busy)
    window.model_manager.normalize_model_name.return_value = "large-v3"
    window.model_manager.selected_model.return_value = "voz"
    window._confirm_model_choice.return_value = True

    mw.MainWindow.select_model(window, "large-v3")

    window.model_manager.select_model.assert_not_called()


def test_the_setup_shortcut_box_pauses_dictation_while_it_captures(monkeypatch) -> None:
    class FakeSignal:
        def __init__(self) -> None:
            self.slots: list = []

        def connect(self, slot) -> None:
            self.slots.append(slot)

        def emit(self, *args) -> None:
            for slot in self.slots:
                slot(*args)

    states: list[bool] = []

    class FakeDialog:
        def __init__(self, parent) -> None:
            self.parent = parent
            self.shortcut_input = SimpleNamespace(
                text=lambda: "M3",
                capture_started=FakeSignal(),
                shortcut_captured=FakeSignal(),
                capture_cancelled=FakeSignal(),
            )

        def exec(self):
            self.shortcut_input.capture_started.emit()
            states.append(self.parent._is_capturing_shortcut)
            self.shortcut_input.shortcut_captured.emit("M3")
            states.append(self.parent._is_capturing_shortcut)
            return mw.QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw, "FirstRunSetupDialog", FakeDialog)
    window = _window(_is_capturing_shortcut=False, settings_manager=FakeSettings(first_run_setup_completed=True))

    mw.MainWindow.show_first_run_setup(window, force=True)

    assert states == [True, False]


def test_unlock_and_resume_rehook_and_device_changes_rescan_later() -> None:
    import ctypes
    from ctypes import wintypes

    def send(message: int, wparam: int) -> MagicMock:
        window = _window()
        msg = wintypes.MSG(0, message, wparam, 0, 0, wintypes.POINT(0, 0))
        mw.MainWindow.nativeEvent(window, b"windows_generic_MSG", ctypes.addressof(msg))
        return window

    send(mw.WM_WTSSESSION_CHANGE, mw.WTS_SESSION_UNLOCK).rehook_requested.emit.assert_called_once_with()
    send(mw.WM_POWERBROADCAST, mw.PBT_APMRESUMEAUTOMATIC).rehook_requested.emit.assert_called_once_with()
    plugged = send(mw.WM_DEVICECHANGE, mw.DBT_DEVNODES_CHANGED)
    plugged.audio_recorder.mark_devices_changed.assert_called_once_with()
    plugged._device_timer.start.assert_called_once_with()
    send(0x0200, 0).rehook_requested.emit.assert_not_called()  # WM_MOUSEMOVE: nothing to do


def test_the_shortcut_warning_comes_back_after_a_capture() -> None:
    # The line went blank after a capture and hid the M3 warning.
    window = _window(_is_capturing_shortcut=True)

    mw.MainWindow.finish_shortcut_capture(window, "Space")

    assert window._is_capturing_shortcut is False
    window.update_shortcut_warning.assert_called_with()


def test_a_shortcut_still_being_typed_shows_no_parse_error() -> None:
    # The box says this before capture_started arrives.
    window = _window(_is_capturing_shortcut=False)
    window.shortcut_input.text.return_value = "Press shortcut..."

    mw.MainWindow.update_shortcut_warning(window)

    window.shortcut_warning.setText.assert_called_once_with("")


def test_diagnostics_carry_no_user_name(monkeypatch) -> None:
    # Copy/Save diagnostics said "no personal paths" while including
    # C:\Users\<name>\... (PL-7, UB-2).
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Avery")
    monkeypatch.setenv("USERNAME", "Avery")
    monkeypatch.setattr(mw.app_log, "recent", lambda count=60: [r"model folder C:\Users\Avery\AppData\Local\x"])
    window = _window()
    window.settings_manager.portable_export.return_value = {"model_path": r"C:\Users\Avery\models"}
    window.settings_manager.get = lambda key, default=None: default
    window.model_manager.selected_model.return_value = "voz"
    window.model_manager.is_model_ready.return_value = True
    window.transcriber.is_loaded.return_value = True
    window.hotkey_status_label.text.return_value = "Global shortcut ready: M3."
    window.audio_recorder.input_device_by_id.return_value = SimpleNamespace(name="Avery's AirPods")

    text = mw.MainWindow.diagnostics_text(window)

    assert "Avery" not in text
    assert "%USERPROFILE%" in text


class _Timer:
    def __init__(self) -> None:
        self.active = False

    def start(self, ms=None) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False


class _Sink:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def emit(self, *args) -> None:
        self.calls.append(args)


def _flow_window(**overrides) -> SimpleNamespace:
    """A window whose own flow methods run for real (MagicMock would stub them)."""
    window = SimpleNamespace(
        settings_manager=FakeSettings(unload_model_after="5", shortcut="M3", shortcut_mode="hold"),
        _idle_unload_timer=_Timer(),
        _cue_timer=_Timer(),
        _is_capturing_shortcut=False,
        _is_listening=False,
        _is_transcribing=False,
        _is_preparing_model=False,
        _start_cue_played=False,
        _error_cue_pending=False,
        _target_hwnd=None,
        _insert_cancel=None,
        _active_insert_id=0,
        _active_transcription_id=0,
        _deliver=True,
        _undelivered_reason="",
        _loading_ui=False,
        model_manager=SimpleNamespace(
            is_model_ready=lambda name=None: True, selected_model=lambda: "voz", model_engine=lambda name=None: "voz"
        ),
        transcriber=SimpleNamespace(is_loaded=lambda: True),
        _selected_microphone_id=lambda: "default",
        _resolve_saved_microphone=lambda: "default",
        text_inserter=SimpleNamespace(get_foreground_window=lambda: "discord", get_window_target=lambda hwnd: None),
        audio_recorder=SimpleNamespace(
            start=lambda *args, **kwargs: None,
            cancel=lambda: None,
            rescan=lambda: None,
            stop=lambda: mw.np.zeros(1600, dtype=mw.np.float32),
        ),
        level_changed=_Sink(),
        escape_hold_changed=_Sink(),
        feedback=_Sink(),
        cue=_Sink(),
        hotkey_enabled_changed=_Sink(),
        _append_activity_log=lambda message: None,
        set_status=lambda status, detail: None,
        _ready_detail=lambda done=False: "",
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    for name in ("_set_listening", "_mouse_hold_shortcut", "_arm_idle_unload", "cancel_listening", "_play_delayed_start_cue"):
        method = getattr(mw.MainWindow, name)
        setattr(window, name, lambda *args, _method=method, **kwargs: _method(window, *args, **kwargs))
    return window


def test_a_tap_or_an_esc_cancel_keeps_the_idle_unload_countdown() -> None:
    # Every middle-click stopped the countdown, so the model was never freed.
    for end in (mw.MainWindow.drop_tapped_recording, mw.MainWindow.cancel_listening):
        window = _flow_window()
        window._arm_idle_unload()

        mw.MainWindow.start_listening(window, from_shortcut=True)
        end(window)

        assert window._idle_unload_timer.active


def test_voz_freed_for_idle_time_loads_while_you_speak() -> None:
    # The first dictation after Free memory waited 0.9 s for the load after
    # the release. It loads while you speak now, into a recording with
    # headroom for the GIL that load holds.
    cases = {
        ("voz", False, "5"): (True, [{"while_listening": True}]),
        ("voz", True, "5"): (False, []),  # still loaded: as before
        ("voz", False, "each"): (False, []),  # freed after every dictation: loads after
        ("whisper", False, "5"): (False, []),  # its GIL hold is unmeasured
    }
    for (engine, loaded, unload), (headroom, loads) in cases.items():
        events: list[object] = []
        window = _flow_window(
            settings_manager=FakeSettings(unload_model_after=unload, shortcut="Tab", shortcut_mode="hold"),
            transcriber=SimpleNamespace(is_loaded=lambda loaded=loaded: loaded),
            warm_up_selected_model=lambda **kwargs: events.append(kwargs),
        )
        window.model_manager.model_engine = lambda name=None, engine=engine: engine
        window.audio_recorder.start = lambda *args, **kwargs: events.append(("start", kwargs.get("headroom")))

        mw.MainWindow.start_listening(window, from_shortcut=True)

        assert events == [("start", headroom), *loads], (engine, loaded, unload)


def test_a_mouse_shortcut_loads_voz_only_once_it_is_held() -> None:
    # A quick middle-click stays a click. Loading Voz for every one would
    # keep Free memory from ever freeing it.
    loads: list[dict] = []
    window = _flow_window(
        transcriber=SimpleNamespace(is_loaded=lambda: False),
        warm_up_selected_model=lambda **kwargs: loads.append(kwargs),
    )

    mw.MainWindow.start_listening(window, from_shortcut=True)  # M3 goes down
    assert loads == []  # a tap ends here: drop_tapped_recording stops the cue timer
    window._play_delayed_start_cue()  # TAP_SECONDS later, still held
    assert loads == [{"while_listening": True}]


def test_a_load_while_listening_waits_for_the_first_audio() -> None:
    # The recorder dates its audio by its first buffers: had they waited
    # behind the load's GIL hold, it would date the recording late and
    # stop before the end of it was in.
    calls: list[str] = []
    window = _window()
    window.audio_recorder.wait_for_audio = lambda timeout: calls.append("audio") or True
    transcriber = SimpleNamespace(prepare_selected_model=lambda settings, model_name: calls.append("load"))

    mw.MainWindow._warm_up_worker(window, 1, transcriber, {}, "voz", while_listening=True)
    mw.MainWindow._warm_up_worker(window, 2, transcriber, {}, "voz")

    assert calls == ["audio", "load", "load"]


def test_only_the_dictations_own_load_runs_while_listening(monkeypatch) -> None:
    started = _no_threads(monkeypatch)
    window = _window(_is_listening=True, _is_transcribing=False, _is_preparing_model=False, _warm_up_generation=0)
    window._pipeline_busy = lambda: True
    window.settings_manager.all.return_value = {"unload_model_after": "5"}
    window.transcriber.is_loaded.return_value = False
    window.model_manager.is_model_ready.return_value = True

    mw.MainWindow.warm_up_selected_model(window)  # the start-up warm-up waits its turn
    assert started == []
    mw.MainWindow.warm_up_selected_model(window, while_listening=True)
    assert [item[0] for item in started] == ["thread"]
    assert started[0][2][-1] is True  # its worker waits for the first audio


def test_dictation_off_while_it_transcribes_keeps_the_text_untyped() -> None:
    started: list[str] = []
    window = _flow_window(_is_transcribing=True, _active_transcription_id=7, _target_hwnd="discord")
    window._cancel_transcription_timer = lambda: None
    window._show_last_dictation = lambda *args: None
    window.last_transcript = SimpleNamespace(setPlainText=lambda text: None)
    window._remember_for_retype = lambda text: None
    window.transcriber = SimpleNamespace(runtime_message="")
    window._start_insert = lambda text, target: started.append(text)

    mw.MainWindow._dictation_toggled(window, False)
    mw.MainWindow.finish_transcription(window, 7, "hello there", 2.0, 0.3)

    assert started == []


def test_a_tap_with_no_microphone_plays_no_error_cue_but_a_hold_does() -> None:
    def no_microphone(*args, **kwargs) -> None:
        raise mw.AudioRecorderError("Could not start the microphone.")

    tapped = _flow_window()
    tapped.audio_recorder.start = no_microphone
    mw.MainWindow.start_listening(tapped, from_shortcut=True)
    mw.MainWindow.drop_tapped_recording(tapped)
    assert tapped.cue.calls == []

    held = _flow_window()
    held.audio_recorder.start = no_microphone
    mw.MainWindow.start_listening(held, from_shortcut=True)
    assert held.cue.calls == []
    held._play_delayed_start_cue()  # TAP_SECONDS later, still held
    assert held.cue.calls == [("error",)]


def test_a_hold_let_go_before_its_start_sound_plays_no_stop_sound(monkeypatch) -> None:
    # A 0.3 s middle-click played a lone stop sound.
    _no_threads(monkeypatch)
    window = _flow_window()

    mw.MainWindow.start_listening(window, from_shortcut=True)  # the start sound waits TAP_SECONDS
    mw.MainWindow.stop_listening_and_transcribe(window)

    assert window.feedback.calls == []


def test_the_dictation_shortcut_cannot_be_the_retype_shortcut(monkeypatch) -> None:
    # One press then re-sent the last message instead of recording.
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_message", lambda parent, title, text, *args: shown.append(title) or True)
    window = _window(settings_manager=FakeSettings(retype_shortcut="Ctrl+Alt+V"))

    assert mw.MainWindow._shortcut_accepted(window, "Alt+Ctrl+V", "M3") is False
    assert shown == ["Shortcut problem"]


def test_a_hook_error_never_pops_a_dialog_from_the_tray(monkeypatch) -> None:
    # The re-hook watchdog can report errors while a game is in front; a
    # minimized window still counts as visible.
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_message", lambda parent, title, text, *args: shown.append(title))
    window = _window()
    window.isVisible.return_value = True
    window.isActiveWindow.return_value = False

    mw.MainWindow.show_hotkey_error(window, "Could not restart the global shortcut listener.")

    assert shown == []
    window.set_status.assert_called_once_with("Hotkey error", "Could not restart the global shortcut listener.")


def test_clearing_the_transcript_forgets_the_retype_text_too() -> None:
    window = _window(_retype_text="secret")

    mw.MainWindow.clear_transcript(window)

    window.last_transcript.clear.assert_called_once_with()
    window._forget_retype.assert_called_once_with()


def test_a_shortcut_with_a_problem_is_refused_and_a_risky_one_asks(monkeypatch) -> None:
    # Ctrl+Alt+Esc could be saved and cancelled itself; setup saved any
    # shortcut without the main window's question.
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_message", lambda parent, title, text, *args: shown.append(title) or False)
    monkeypatch.setattr(mw, "shortcut_problem", lambda text: "Esc is the cancel key." if "Esc" in text else "")
    window = _window()

    assert mw.MainWindow._shortcut_accepted(window, "Ctrl+Alt+Esc", "M3") is False
    assert mw.MainWindow._shortcut_accepted(window, "Ctrl", "M3") is False  # asked, and declined
    assert mw.MainWindow._shortcut_accepted(window, "Ctrl+Alt+Q", "M3") is True
    assert shown == ["Shortcut problem", "Shortcut warning"]


def test_tab_alone_is_refused_as_the_retype_shortcut(monkeypatch) -> None:
    # Only the dictation shortcut holds a bare Tab back from apps; as the
    # retype one, every Tab would type the last dictation again.
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_message", lambda parent, title, text, *args: shown.append(text))
    window = _window(settings_manager=FakeSettings(shortcut="M3"))

    mw.MainWindow.save_retype_shortcut(window, "Tab")

    assert len(shown) == 1 and "while you type" in shown[0]
    assert "retype_shortcut" not in window.settings_manager


# ------------------------------------------------------------- v0.2.8


def test_a_problem_is_reported_for_the_tray_before_the_state_changes() -> None:
    # Errors only showed inside the window, which sits hidden in the tray: a
    # failed dictation gave the error sound and nothing else.
    calls: list[tuple] = []
    window = _window()
    window.problem.emit = lambda heading, detail: calls.append(("problem", heading, detail))
    window._refresh_visual_state = lambda: calls.append(("state",))

    mw.MainWindow.set_status(window, "Error", "The app did not take the paste.")
    mw.MainWindow.set_status(window, "Not typed", "The shortcut seemed stuck.")
    mw.MainWindow.set_status(window, "Ready", "Done.")

    assert calls == [
        ("problem", "Dictation problem", "The app did not take the paste."),
        ("state",),
        ("problem", "Not typed", "The shortcut seemed stuck."),
        ("state",),
        ("state",),
    ]


def test_the_tray_is_told_about_the_error_state() -> None:
    window = _window(
        _is_listening=False,
        _is_transcribing=False,
        _is_preparing_model=False,
        _status_is_error=True,
        _last_state="busy",
    )
    window._visual_state = lambda: mw.MainWindow._visual_state(window)

    mw.MainWindow._refresh_visual_state(window)

    window.state_changed.emit.assert_called_once_with("error")


def test_the_first_close_to_the_tray_says_the_app_is_still_running() -> None:
    # The window vanished, and nothing said the shortcut still worked or how to quit.
    settings = FakeSettings(minimize_to_tray=True)
    window = _window(settings_manager=settings, _force_exit=False)
    event = MagicMock()

    mw.MainWindow.closeEvent(window, event)
    mw.MainWindow.closeEvent(window, event)

    assert event.ignore.call_count == 2
    assert window.notice.emit.call_count == 1  # said once, ever
    assert settings["tray_hint_shown"] is True


def test_a_start_in_the_tray_loads_the_model_at_once() -> None:
    # The 1.5 s wait keeps an opening window smooth; a hidden start has none to keep.
    window = _window()

    mw.MainWindow.warm_up_at_once(window)

    window._warm_up_timer.stop.assert_called_once_with()
    window.warm_up_selected_model.assert_called_once_with()


def test_controls_that_do_nothing_for_the_selected_model_are_switched_off() -> None:
    # With Voz selected, Auto/CPU/GPU and Whisper precision could be changed, and changed nothing.
    expected = {"voz": (False, False), "whisper": (True, True), "funasr": (True, False)}
    for engine, (hardware, precision) in expected.items():
        window = _window()
        window.model_manager.selected_info.return_value = SimpleNamespace(engine=engine, label="Model")

        mw.MainWindow._show_what_applies(window)

        window.device_segmented.setEnabled.assert_called_once_with(hardware)
        window.compute_combo.setEnabled.assert_called_once_with(precision)


def test_setup_asks_about_hardware_only_for_models_that_use_it() -> None:
    # Voz always runs on the CPU: one question fewer on the first run.
    from PySide6.QtWidgets import QApplication, QWidget

    from core.model_manager import ModelManager

    _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    parent = QWidget()
    parent.settings_manager = FakeSettings()
    parent.model_manager = ModelManager(parent.settings_manager)
    parent.audio_recorder = SimpleNamespace(
        list_input_devices=lambda keep=None: [SimpleNamespace(id="default", name="Default system microphone")]
    )
    parent._resolve_saved_microphone = lambda: "default"
    parent._select_combo_data = lambda combo, value: mw.MainWindow._select_combo_data(parent, combo, value)

    dialog = mw.FirstRunSetupDialog(parent)

    assert not dialog.form.isRowVisible(dialog.device_combo)
    dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("small"))
    assert dialog.form.isRowVisible(dialog.device_combo)
    dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("voz"))
    assert not dialog.form.isRowVisible(dialog.device_combo)


def test_text_with_no_app_to_type_into_stays_in_last_transcription() -> None:
    # Dictating from the window's own button pasted into the window itself,
    # and Enter clicked its focused button.
    window = _window(_active_transcription_id=2, _deliver=True, _target_hwnd=None)

    mw.MainWindow.finish_transcription(window, 2, "notes to self", 2.0, 0.3)

    window._start_insert.assert_not_called()
    window.last_transcript.setPlainText.assert_called_once_with("notes to self")
    assert "Last transcription" in window.set_status.call_args[0][1]


def test_recent_apps_offer_profiles_most_recent_first() -> None:
    window = _window(_recent_apps=["notepad.exe", "discord.exe"])

    mw.MainWindow._remember_app(window, SimpleNamespace(process_name="Discord.exe"))
    mw.MainWindow._remember_app(window, SimpleNamespace(process_name=""))

    assert window._recent_apps == ["discord.exe", "notepad.exe"]


def test_adding_and_removing_an_app_profile_saves_it() -> None:
    settings = FakeSettings(app_profiles={})
    window = _window(settings_manager=settings)

    mw.MainWindow.add_app_profile(window, "  Game.EXE ")
    assert settings["app_profiles"] == {
        "game.exe": {"insert_method": "auto", "send_key": "default", "full_stop": "default"}
    }
    mw.MainWindow.save_app_profile(window, "game.exe", "insert_method", "type")
    assert settings["app_profiles"]["game.exe"]["insert_method"] == "type"
    mw.MainWindow.add_app_profile(window, "not an exe")
    assert list(settings["app_profiles"]) == ["game.exe"]
    mw.MainWindow.remove_app_profile(window, "game.exe")
    assert settings["app_profiles"] == {}
