"""Local JSON state for daily email quota and per-row send metadata (reply sync)."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def _today_local() -> str:
    return datetime.now().astimezone().date().isoformat()


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "quota_date": _today_local(),
            "sent_today": 0,
            "by_row": {},
        }
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {
            "quota_date": _today_local(),
            "sent_today": 0,
            "by_row": {},
        }
    if not isinstance(data, dict):
        data = {}
    data.setdefault("quota_date", _today_local())
    data.setdefault("sent_today", 0)
    data.setdefault("by_row", {})
    if not isinstance(data["by_row"], dict):
        data["by_row"] = {}
    return data


def save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def reset_quota_if_new_day(data: dict[str, Any]) -> None:
    today = _today_local()
    if data.get("quota_date") != today:
        data["quota_date"] = today
        data["sent_today"] = 0


def remaining_quota(data: dict[str, Any], daily_limit: int) -> int:
    reset_quota_if_new_day(data)
    return max(0, daily_limit - int(data.get("sent_today") or 0))


def record_successful_send(
    data: dict[str, Any],
    *,
    sheet_row: int,
    to_email: str,
    message_id: str,
) -> None:
    reset_quota_if_new_day(data)
    data["sent_today"] = int(data.get("sent_today") or 0) + 1
    key = str(sheet_row)
    data["by_row"][key] = {
        "to": to_email,
        "sent_at": _now_iso(),
        "message_id": message_id,
    }


def get_row_send_meta(data: dict[str, Any], sheet_row: int) -> dict[str, Any] | None:
    entry = data.get("by_row", {}).get(str(sheet_row))
    return entry if isinstance(entry, dict) else None


@contextmanager
def acquire_send_lock(lock_path: Path) -> Iterator[None]:
    """
    Prevent overlapping outbound email batches.

    A live Python sender process keeps this advisory lock open for the full duration of
    the send loop. If a second send starts, it fails fast instead of emailing the same
    "not sent" rows from a stale in-memory snapshot.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            holder = fh.read().strip()
            msg = "Another outbound email run is already active."
            if holder:
                msg += f" Lock holder: {holder}"
            raise RuntimeError(msg)

        meta = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat(),
            "argv": sys.argv,
        }
        fh.seek(0)
        fh.truncate(0)
        fh.write(json.dumps(meta, separators=(",", ":")))
        fh.flush()
        os.fsync(fh.fileno())
        yield
    finally:
        try:
            fh.seek(0)
            fh.truncate(0)
            fh.flush()
        except OSError:
            pass
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()
