from __future__ import annotations

import sys
from pathlib import Path

from core.startup_manager import StartupManager


def test_a_source_run_starts_pythonw_with_the_script(tmp_path, monkeypatch) -> None:
    # From source, python.exe ends in .exe too: the Run key got
    # "python.exe --minimized", which exits at sign-in with "unknown option".
    (tmp_path / "pythonw.exe").write_bytes(b"")
    script = tmp_path / "src" / "main.py"
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "argv", [str(script)])

    command = StartupManager(tmp_path / "python.exe").startup_command()

    assert command == f'"{tmp_path / "pythonw.exe"}" "{Path(script).resolve()}" --minimized'
