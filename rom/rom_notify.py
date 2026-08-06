"""Best-effort terminal notifications with durable retry spooling."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NewType

EventId = NewType("EventId", str)


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    ntfy_url: str
    spool_dir: Path
    timeout_seconds: float = 5.0
    token_file: Path | None = None
    paseo_host: str | None = None
    paseo_agent_id: str | None = None

    @classmethod
    def load(cls, path: Path) -> NotifyConfig | None:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text())
        return cls(
            ntfy_url=str(raw["ntfy_url"]),
            spool_dir=Path(raw.get("spool_dir", "/srv/android/logs/notify-spool")),
            timeout_seconds=float(raw.get("timeout_seconds", 5.0)),
            token_file=Path(raw["token_file"]) if raw.get("token_file") else None,
            paseo_host=str(raw["paseo_host"]) if raw.get("paseo_host") else None,
            paseo_agent_id=str(raw["paseo_agent_id"]) if raw.get("paseo_agent_id") else None,
        )


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    event_id: EventId
    state: str
    lane: str
    run_id: str
    phase: str
    log: str
    excerpt: tuple[str, ...]
    build_id: str = "-"
    build_class: str = "-"
    artifact_source: str = "-"
    artifact_dest: str = "-"
    returncode: int | None = None
    occurred: str = "-"


def _atomic_json(path: Path, event: NotifyEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(asdict(event), handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Notifier:
    """Deliver events without allowing alert transport to change build outcome."""

    def __init__(self, config: NotifyConfig) -> None:
        self.config = config

    def emit(self, event: NotifyEvent) -> bool:
        delivered = self._post_ntfy(event)
        if delivered:
            self._send_paseo(event)
            return True
        _atomic_json(self.config.spool_dir / f"{event.event_id}.json", event)
        return False

    def retry_spool(self) -> tuple[int, int]:
        delivered = 0
        failed = 0
        if not self.config.spool_dir.is_dir():
            return delivered, failed
        for path in sorted(self.config.spool_dir.glob("*.json")):
            raw = json.loads(path.read_text())
            event = NotifyEvent(
                event_id=EventId(raw["event_id"]),
                state=raw["state"], lane=raw["lane"], run_id=raw["run_id"],
                phase=raw["phase"], log=raw["log"], excerpt=tuple(raw["excerpt"]),
                build_id=raw.get("build_id", "-"), build_class=raw.get("build_class", "-"),
                artifact_source=raw.get("artifact_source", "-"),
                artifact_dest=raw.get("artifact_dest", "-"),
                returncode=raw.get("returncode"), occurred=raw.get("occurred", "-"),
            )
            if self._post_ntfy(event):
                path.unlink()
                self._send_paseo(event)
                delivered += 1
            else:
                failed += 1
        return delivered, failed

    def _post_ntfy(self, event: NotifyEvent) -> bool:
        body = json.dumps(asdict(event), sort_keys=True).encode()
        headers = {
            "Content-Type": "application/json",
            "Title": f"ROM {event.lane} {event.state}",
            "Tags": "white_check_mark" if event.state == "SUCCESS" else "rotating_light",
            "X-Message-ID": str(event.event_id),
        }
        if self.config.token_file and self.config.token_file.is_file():
            headers["Authorization"] = f"Bearer {self.config.token_file.read_text().strip()}"
        request = urllib.request.Request(self.config.ntfy_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _send_paseo(self, event: NotifyEvent) -> None:
        if not (self.config.paseo_host and self.config.paseo_agent_id):
            return
        token = base64.urlsafe_b64encode(json.dumps(asdict(event), sort_keys=True).encode()).decode()
        subprocess.run(
            ["tailscale", "ssh", self.config.paseo_host, "paseo", "send",
             self.config.paseo_agent_id, f"ROM event base64url={token}"],
            timeout=self.config.timeout_seconds,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def load_notifier(config_path: Path) -> Notifier | None:
    config = NotifyConfig.load(config_path)
    return Notifier(config) if config else None
