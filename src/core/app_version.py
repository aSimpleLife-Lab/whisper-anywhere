from __future__ import annotations

APP_VERSION = "0.1.1"
GITHUB_REPO = "aSimpleLife-Lab/whisper-anywhere"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"


def app_version_tag() -> str:
    return APP_VERSION if APP_VERSION.startswith("v") else f"v{APP_VERSION}"
