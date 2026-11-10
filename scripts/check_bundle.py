"""Fail the build when a bundled file needs a DLL that is not there.

build_exe.ps1 deletes Qt files the app never loads. A plugin left behind
that still imports one of them would load on no PC, and only a user would
find out. This reads every .exe, .dll and .pyd's import table and names each
import that is neither in the bundle nor part of Windows.

Usage: python check_bundle.py <bundle folder>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pefile  # comes with PyInstaller

SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
# Imports whose absence is fine. tbb12.dll: numba (FunASR's, in the full build)
# has a TBB threading layer that nothing ships, and uses another without it.
OPTIONAL = {"tbb12.dll"}


def imported_dlls(path: Path) -> set[str]:
    """The DLLs a file needs to load at all (delay-loaded ones are optional)."""
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        return {entry.dll.decode(errors="replace").lower() for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])}
    finally:
        pe.close()


def missing_imports(bundle: Path) -> list[str]:
    files = [path for path in bundle.rglob("*") if path.suffix.lower() in (".exe", ".dll", ".pyd")]
    bundled = {path.name.lower() for path in files}
    problems = []
    for path in sorted(files):
        for name in sorted(imported_dlls(path) - bundled - OPTIONAL):
            if name.startswith(("api-ms-", "ext-ms-")) or (SYSTEM32 / name).exists():
                continue  # Windows' own
            problems.append(f"{path.relative_to(bundle)} needs {name}, which is not in the bundle")
    return problems


if __name__ == "__main__":
    found = missing_imports(Path(sys.argv[1]))
    print("\n".join(found) or "Bundle check: every bundled file's DLLs are there.")
    sys.exit(1 if found else 0)
