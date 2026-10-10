"""TraeX rollout discovery and text-message projection (no network or host writes).

TraeX 0.209.1 persists completed items in history_mutation/append. Its UI
display_completions and event_msg entries are mirrors, not extra messages.
Older rollouts contain response_item entries instead.
"""
from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3


def runtime_home() -> Path:
    shared = Path(os.environ.get("TRAE_HOME", str(Path.home() / ".trae"))).expanduser()
    return Path(os.environ.get("TRAECLI_HOME", str(shared / "cli"))).expanduser().resolve()


def transcript(session_id: str) -> Path | None:
    """Resolve the exact session when hook transcript_path is null.

    Read the host's index without creating a database; the filename lookup also
    works before the index is updated and on hosts without the SQLite index.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise ValueError("Invalid TraeX session_id")
    root = runtime_home()
    database = root / "state_5.sqlite"
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                row = conn.execute("SELECT rollout_path FROM threads WHERE id = ?", (session_id,)).fetchone()
            if row and Path(row[0]).is_file():
                return Path(row[0])
        except sqlite3.Error:
            pass  # Index unavailable or a different schema; fall back to filenames.
    matches = [p for base in (root / "sessions", root / "archived_sessions") if base.is_dir()
               for p in base.rglob(f"rollout-*-{session_id}.jsonl") if p.is_file()]
    if len(matches) > 1:
        raise ValueError("Multiple TraeX rollouts match the same session_id")
    return matches[0] if matches else None


def is_trae(entries: list[dict], path: str = "") -> bool:
    if "/.trae/" in path or "/.coco/" in path:
        return True
    for entry in entries[:50]:
        if entry.get("type") == "session_meta":
            originator = str((entry.get("payload") or {}).get("originator", "")).lower()
            if originator.startswith(("trae", "coco")):
                return True
    return False


def message_entries(entries: list[dict]) -> list[dict]:
    """Normalize only original user/assistant text, excluding runtime scaffolding.

    Never replay replacement/compaction snapshots or UI mirrors. The caller
    commits its byte offset only after the resulting messages have been saved.
    """
    out = []
    for entry in entries:
        payload = entry.get("payload") or {}
        if entry.get("type") == "response_item":
            items = [payload]
        elif entry.get("type") == "history_mutation":
            if payload.get("version") != 1:
                raise ValueError("Unsupported TraeX history_mutation version")
            if payload.get("operation") != "append":
                continue
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError("TraeX append is missing items")
        else:
            continue
        for item in items:
            if item.get("type") != "message" or item.get("role") not in ("user", "assistant"):
                continue
            if item.get("channel") == "analysis" or item.get("phase") == "analysis":
                continue
            parts = item.get("content") or []
            meta = item.get("internal_chat_message_metadata_passthrough") or {}
            kinds = meta.get("content_item_kinds")
            if item["role"] == "user" and kinds is not None:
                if not isinstance(kinds, list) or len(kinds) != len(parts):
                    raise ValueError("TraeX content_item_kinds does not match content")
                parts = [part for part, kind in zip(parts, kinds)
                         if isinstance(kind, str) and kind.startswith("user.")]
            normalized = dict(item, content=parts)
            out.append({"type": "response_item", "timestamp": entry.get("timestamp"), "payload": normalized})
    return out
