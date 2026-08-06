from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rom_notify import EventId, Notifier, NotifyConfig, NotifyEvent


class CaptureHandler(BaseHTTPRequestHandler):
    payloads: list[dict] = []
    response_status = 200

    def do_POST(self) -> None:
        size = int(self.headers["Content-Length"])
        self.payloads.append(json.loads(self.rfile.read(size)))
        self.send_response(self.response_status)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return


def serve(status: int = 200) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    CaptureHandler.payloads = []
    CaptureHandler.response_status = status
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}/topic"


def event() -> NotifyEvent:
    return NotifyEvent(
        event_id=EventId("run-failed"), state="FAILED", lane="test", run_id="run",
        phase="NINJA", log="/logs/build.log", excerpt=("error: boom",), returncode=1,
    )


def test_notifier_posts_structured_failure(tmp_path: Path) -> None:
    server, thread, url = serve()
    try:
        notifier = Notifier(NotifyConfig(url, tmp_path / "spool"))

        delivered = notifier.emit(event())

        assert delivered
        assert CaptureHandler.payloads[0]["log"] == "/logs/build.log"
        assert CaptureHandler.payloads[0]["excerpt"] == ["error: boom"]
    finally:
        server.shutdown()
        thread.join()


def test_notifier_spools_failure_then_retries_once(tmp_path: Path) -> None:
    failing, failing_thread, failing_url = serve(503)
    notifier = Notifier(NotifyConfig(failing_url, tmp_path / "spool"))
    try:
        assert not notifier.emit(event())
        assert len(list((tmp_path / "spool").glob("*.json"))) == 1
    finally:
        failing.shutdown()
        failing_thread.join()

    success, success_thread, success_url = serve()
    try:
        retry = Notifier(NotifyConfig(success_url, tmp_path / "spool"))
        assert retry.retry_spool() == (1, 0)
        assert not list((tmp_path / "spool").glob("*.json"))
        assert len(CaptureHandler.payloads) == 1
    finally:
        success.shutdown()
        success_thread.join()
