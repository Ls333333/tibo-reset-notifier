from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import asdict

MAX_ENTRIES = 1500


class StateStore:
    def __init__(self, path: Path):
        path = self.path = Path(path)
        self.fresh_file = not path.exists()
        self._data: dict = {"version": 2, "seen": {}, "pending": {}, "last_run": None}
        if not self.fresh_file:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                raise ValueError("Cannot read notification state; preserve it and repair before continuing") from error
            if not isinstance(loaded, dict) or loaded.get("version") not in (1, 2):
                raise ValueError("Unsupported notification state")
            if not isinstance(loaded.get("seen"), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in loaded["seen"].items()):
                raise ValueError("Invalid seen-post state")
            pending = loaded.get("pending", {})
            if not isinstance(pending, dict):
                raise ValueError("Invalid pending notification state")
            from .model import Post
            for key, record in pending.items():
                if not isinstance(record, dict) or not isinstance(record.get("post"), dict):
                    raise ValueError("Invalid pending post")
                try:
                    post = Post(**record["post"])
                except (TypeError, ValueError) as error:
                    raise ValueError("Invalid pending post fields") from error
                if key != post.key or not all(isinstance(record.get(f), list) and all(isinstance(c, str) for c in record[f]) for f in ("remaining", "completed")):
                    raise ValueError("Invalid pending channels")
                if not isinstance(record.get("score_total"), int) or not isinstance(record.get("score_hits"), str):
                    raise ValueError("Invalid pending score")
            self._data = {**loaded, "version": 2, "pending": pending}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def is_seen(self, key: str) -> bool:
        return key in self._data["seen"]

    def mark_seen(self, key: str) -> None:
        self._data["seen"][key] = self._now()

    @property
    def pending(self) -> dict:
        return self._data["pending"]

    def enqueue(self, post, score_total: int, score_hits: str, channels: list[str]) -> None:
        if post.key not in self.pending:
            self.pending[post.key] = {
                "post": asdict(post), "score_total": score_total,
                "score_hits": score_hits, "remaining": list(channels),
                "completed": [], "queued_at": self._now(),
            }
        self.mark_seen(post.key)

    def complete_channel(self, key: str, channel: str) -> bool:
        record = self.pending[key]
        record["remaining"].remove(channel)
        record["completed"].append(channel)
        finished = not record["remaining"]
        if finished:
            del self.pending[key]
        return finished

    def prune(self, limit: int = MAX_ENTRIES) -> None:
        seen = self._data["seen"]
        if len(seen) <= limit:
            return
        newest_first = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
        self._data["seen"] = dict(newest_first[:limit])

    def tracked_count(self) -> int:
        return len(self._data["seen"])

    def save(self) -> None:
        self._data["last_run"] = self._now()
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=1, ensure_ascii=True)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
