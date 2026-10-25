from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import voz_engine as ve

FILES = {"meta.json": b'{"ok": true}', "encoder.onnx.data": bytes(range(256)) * 400}


def serve(files: dict[str, bytes], fail_first: set[str] | None = None):
    """A local stand-in for huggingface.co that honours Range requests."""
    failed: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            name = self.path.rsplit("/", 1)[-1]
            if name not in files:
                self.send_error(404)
                return
            data = files[name]
            start = 0
            if self.headers.get("Range"):
                start = int(self.headers["Range"].split("=")[1].split("-")[0])
            if fail_first and name in fail_first and name not in failed:
                failed.add(name)  # first try: send half the file, then drop the connection
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[: len(data) // 2])
                return
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def small_bundle(monkeypatch):
    monkeypatch.setattr(ve, "BUNDLE_FILES", {name: len(data) for name, data in FILES.items()})


def test_downloads_every_missing_file(tmp_path, monkeypatch, small_bundle) -> None:
    server, url = serve(FILES)
    monkeypatch.setattr(ve, "HF_BASE", url)
    try:
        ve.download_bundle(tmp_path)
    finally:
        server.shutdown()
    assert ve.missing_files(tmp_path) == []
    assert (ve.bundle_path(tmp_path) / "encoder.onnx.data").read_bytes() == FILES["encoder.onnx.data"]


def test_a_dropped_connection_resumes_where_it_stopped(tmp_path, monkeypatch, small_bundle) -> None:
    server, url = serve(FILES, fail_first={"encoder.onnx.data"})
    monkeypatch.setattr(ve, "HF_BASE", url)
    monkeypatch.setattr(ve.time, "sleep", lambda seconds: None)
    try:
        ve.download_bundle(tmp_path)
    finally:
        server.shutdown()
    assert (ve.bundle_path(tmp_path) / "encoder.onnx.data").read_bytes() == FILES["encoder.onnx.data"]


def test_a_silent_network_fails_fast_with_a_plain_reason(tmp_path, monkeypatch, small_bundle) -> None:
    # Accepts connections and never answers, like a firewall dropping the traffic.
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(8)
    held = []

    def accept_all() -> None:
        try:
            while True:
                held.append(hole.accept())
        except OSError:
            pass  # closed at the end of the test

    threading.Thread(target=accept_all, daemon=True).start()
    monkeypatch.setattr(ve, "HF_BASE", f"http://127.0.0.1:{hole.getsockname()[1]}")
    monkeypatch.setattr(ve, "READ_TIMEOUT", 0.5)
    monkeypatch.setattr(ve.time, "sleep", lambda seconds: None)

    started = time.monotonic()
    with pytest.raises(ve.VozError, match="did not respond"):
        ve.download_bundle(tmp_path)
    assert time.monotonic() - started < 10  # the old path sat at 0 MB for minutes
    hole.close()


def test_whisper_failures_name_the_network_cause() -> None:
    from core.transcriber import _failure_reason

    try:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as low:
            raise RuntimeError("Cannot send a request, as the client has been closed.") from low
    except RuntimeError as exc:
        assert "did not respond" in _failure_reason(exc)
    assert "ValueError: bad" in _failure_reason(ValueError("bad"))


def test_an_http_error_is_reported(tmp_path, monkeypatch, small_bundle) -> None:
    server, url = serve({})  # every file is a 404
    monkeypatch.setattr(ve, "HF_BASE", url)
    monkeypatch.setattr(ve.time, "sleep", lambda seconds: None)
    try:
        with pytest.raises(ve.VozError, match="error 404"):
            ve.download_bundle(tmp_path)
    finally:
        server.shutdown()
