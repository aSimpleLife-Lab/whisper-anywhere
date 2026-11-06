from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import ui.main_window as mw


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value) -> None:
        self[key] = value


def test_a_rebuilt_transcriber_keeps_the_input_freeze_guard() -> None:
    # Model and Performance changes rebuild the transcriber; the rebuilt one
    # loaded Voz with the hooks live and froze the PC for ~3 s per dictation.
    guard = object()
    old = mw.Transcriber(SimpleNamespace())
    old.load_guard = guard
    window = SimpleNamespace(_warm_up_generation=0, transcriber=old, model_manager=old.model_manager)

    mw.MainWindow._reset_transcriber(window)

    assert window.transcriber is not old
    assert window.transcriber.load_guard is guard


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


def test_a_hook_error_never_pops_a_dialog_from_the_tray(monkeypatch) -> None:
    # The re-hook watchdog can report errors while a game is in front.
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_message", lambda parent, title, text, *args: shown.append(title))
    window = _window()
    window.isVisible.return_value = False

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
