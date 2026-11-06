"""The PowerShell and Inno Setup scripts, read as text: running them would
install packages or change the registry."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_install_funasr_stops_at_the_first_failed_command() -> None:
    # SW-6: a failed venv or pip step still ended with "FunASR runtime installed".
    lines = (ROOT / "scripts" / "install_funasr.ps1").read_text(encoding="utf-8").splitlines()
    native = [index for index, line in enumerate(lines) if re.match(r"\s*(& \$Python |py )", line)]

    assert native
    for index in native:
        check = next(line for line in lines[index + 1 :] if line.strip())
        assert "$LASTEXITCODE" in check and "throw" in check, lines[index]


def test_the_cpu_torch_is_pinned_like_the_cuda_one() -> None:
    script = (ROOT / "scripts" / "install_funasr.ps1").read_text(encoding="utf-8")
    version = re.search(r'"torch==([\d.]+)\+cu\d+"', script).group(1)

    assert f'"torch=={version}+cpu" "torchaudio=={version}+cpu"' in script
    assert "install --upgrade torch torchaudio" not in script


def test_uninstall_removes_start_with_windows_only_when_it_starts_this_install() -> None:
    # PL-2: uninstall deleted the Run value whichever copy it started.
    iss = (ROOT / "installer" / "WhisperAnywhere.iss").read_text(encoding="utf-8")
    code = iss.split("[Code]", 1)[-1]

    assert "uninsdeletevalue" not in iss
    assert "RegQueryStringValue(HKCU, RunKey, 'Whisper Anywhere', Command)" in code
    assert "Pos(Lowercase(ExpandConstant('{app}\\')), Lowercase(Command)) > 0" in code
    assert "RegDeleteValue(HKCU, RunKey, 'Whisper Anywhere')" in code
