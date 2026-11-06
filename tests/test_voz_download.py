from __future__ import annotations

import hashlib
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import voz_engine as ve

FILES = {"meta.json": b'{"ok": true}', "encoder.onnx.data": bytes(range(256)) * 400}
# A Wi-Fi sign-in page answers every request with its own HTML.
PORTAL = b"<html><head><meta http-equiv='refresh' content='0;url=http://login.example/'></head></html>"


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
                if start >= len(data):
                    self.send_error(416)  # Range Not Satisfiable, as huggingface.co answers
                    return
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
    monkeypatch.setattr(
        ve, "BUNDLE_SHA256", {name: hashlib.sha256(data).hexdigest() for name, data in FILES.items()}
    )


def test_every_bundle_file_has_a_pinned_checksum() -> None:
    assert set(ve.BUNDLE_SHA256) == set(ve.BUNDLE_FILES)
    assert all(len(digest) == 64 for digest in ve.BUNDLE_SHA256.values())


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


def test_a_part_file_that_is_already_complete_is_moved_into_place(
    tmp_path, monkeypatch, small_bundle
) -> None:
    # The rename failed (antivirus scanning the new file) or the app stopped
    # just before it: asking for the bytes after the end got HTTP 416 forever.
    server, url = serve(FILES)
    monkeypatch.setattr(ve, "HF_BASE", url)
    monkeypatch.setattr(ve.time, "sleep", lambda seconds: None)
    folder = ve.bundle_path(tmp_path)
    folder.mkdir(parents=True)
    (folder / "encoder.onnx.data.part").write_bytes(FILES["encoder.onnx.data"])
    try:
        ve.download_bundle(tmp_path)
    finally:
        server.shutdown()
    assert ve.missing_files(tmp_path) == []


def test_a_part_file_with_foreign_bytes_is_downloaded_again(tmp_path, monkeypatch, small_bundle) -> None:
    # A Wi-Fi sign-in page left its HTML as the start of a download; resuming
    # after it gave a file of the right size that the model cannot read.
    server, url = serve(FILES)
    monkeypatch.setattr(ve, "HF_BASE", url)
    monkeypatch.setattr(ve.time, "sleep", lambda seconds: None)
    folder = ve.bundle_path(tmp_path)
    folder.mkdir(parents=True)
    (folder / "encoder.onnx.data.part").write_bytes(PORTAL)
    try:
        ve.download_bundle(tmp_path)
    finally:
        server.shutdown()
    assert (folder / "encoder.onnx.data").read_bytes() == FILES["encoder.onnx.data"]


def test_a_part_file_longer_than_the_file_is_discarded(tmp_path, monkeypatch, small_bundle) -> None:
    server, url = serve(FILES)
    monkeypatch.setattr(ve, "HF_BASE", url)
    folder = ve.bundle_path(tmp_path)
    folder.mkdir(parents=True)
    (folder / "meta.json.part").write_bytes(FILES["meta.json"] + b" and more")
    try:
        ve.download_bundle(tmp_path)
    finally:
        server.shutdown()
    assert (folder / "meta.json").read_bytes() == FILES["meta.json"]


def test_a_file_that_never_matches_its_checksum_fails_plainly(tmp_path, monkeypatch, small_bundle) -> None:
    # Something on the way rewrites the file (a proxy, a scanner): right size, wrong bytes.
    altered = dict(FILES, **{"encoder.onnx.data": bytes(len(FILES["encoder.onnx.data"]))})
    server, url = serve(altered)
    monkeypatch.setattr(ve, "HF_BASE", url)
    monkeypatch.setattr(ve.time, "sleep", lambda seconds: None)
    try:
        with pytest.raises(ve.VozError, match="encoder.onnx.data did not match the published file"):
            ve.download_bundle(tmp_path)
    finally:
        server.shutdown()
    folder = ve.bundle_path(tmp_path)
    assert not (folder / "encoder.onnx.data").exists()
    assert not (folder / "encoder.onnx.data.part").exists()  # never resumed into the next try
    assert (folder / "meta.json").read_bytes() == FILES["meta.json"]


def test_two_downloads_never_write_the_same_files_at_once(
    tmp_path, monkeypatch, small_bundle
) -> None:
    # A prepare the window had given up on kept downloading while a new one
    # wrote the same .part file.
    active: list[str] = []
    most: list[int] = []

    def slow_download(remote: str, target, size: int, sha256: str) -> None:
        active.append(remote)
        most.append(len(active))
        time.sleep(0.05)
        active.remove(remote)

    def prepare() -> None:
        try:
            ve.download_bundle(tmp_path)
        except ve.VozError:
            pass  # nothing really arrives here

    monkeypatch.setattr(ve, "_download_file", slow_download)
    threads = [threading.Thread(target=prepare) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max(most) == 1


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
