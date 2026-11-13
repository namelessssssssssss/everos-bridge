"""Credential-scoped hard-quota circuit; no request bodies or credentials on disk."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import time
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Generated Windows runtime supplies the same lock interface.
    import windows_lock as fcntl

CST = timezone(timedelta(hours=8))
COOLDOWN = 900
DEFAULT_DAILY_BUDGET = 40
RESOURCES = ("mcu", "retrieval")
READ_PATHS = {"/api/v2/memory/get", "/api/v2/memory/search",
              "/api/v1/memories/get", "/api/v1/memories/search"}
WRITE_PATHS = {"/api/v2/memory/add", "/api/v2/memory/flush", "/api/v2/memory/edit",
               "/api/v1/memories", "/api/v1/memories/flush",
               "/api/v1/memories/agent", "/api/v1/memories/agent/flush"}
QUOTA_TYPES = {"mcu": "mcu", "retrieval": "retrieval",
               "retrieval_api_calls": "retrieval", "retrieval_api": "retrieval"}


def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


class QuotaError(RuntimeError):
    """A fixed, safe message suitable for CLI, MCP and hook logs."""
    def __init__(self, resource, balance=None, retry_at=None, cached=False):
        self.resource = resource
        self.balance = number(balance)
        self.retry_at = number(retry_at)
        self.cached = cached
        label = "MCU" if resource == "mcu" else "检索调用"
        detail = f"，balance={self.balance:g}" if self.balance is not None else ""
        super().__init__(f"EverOS {label}配额已耗尽（quota_exceeded{detail}）；相关请求已暂停，"
                         f"请求尚未完成。额度恢复后执行 everos quota retry --resource {resource}，"
                         "允许下一次请求验证恢复。")

    def failure(self):
        result = {"kind": "quota" if self.cached else "http", "status": 429, "exception": "QuotaError", "retryable": False,
                  "code": "quota_exceeded", "quota_type": self.resource, "cached": self.cached}
        if self.balance is not None:
            result["balance"] = self.balance
        return result


def is_cached_refusal(error):
    if isinstance(error, QuotaError):
        return error.cached
    return isinstance(error, WriteError) and error.cached_quota


class WriteError(RuntimeError):
    """Preserve aggregated failure provenance without hiding partial writes."""
    def __init__(self, message, failures, attempted, progressed=False):
        super().__init__(message)
        self.cached_quota = (bool(failures) and len(failures) == attempted and not progressed
                             and all(is_cached_refusal(error) for error in failures))


def inspect_error(error):
    """Only recognized hard quotas open the circuit; preserve ordinary 429 bodies."""
    if error.code != 429:
        return None, error
    try:
        raw = error.read(16384)
    except Exception:
        return None, error
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    detail = payload.get("error")
    if isinstance(detail, dict):
        payload = detail
        code = payload.get("code")
    else:
        code = detail or payload.get("code")
    resource = QUOTA_TYPES.get(payload.get("quota_type")) if isinstance(payload.get("quota_type"), str) else None
    info = None
    if code == "quota_exceeded" and resource:
        info = {"resource": resource, "balance": number(payload.get("balance"))}
    replacement = urllib.error.HTTPError(error.url, error.code, error.msg, error.headers, io.BytesIO(raw))
    error.close()
    return info, replacement


class Gate:
    def __init__(self, state: Path, config: dict, resource: str):
        if resource not in RESOURCES:
            raise ValueError("unknown quota resource")
        self.resource = resource
        identity = config.get("EVEROS_API_BASE", "").rstrip("/") + "\0" + (
            config.get("EVEROS_API_KEY") or "proxy:" + config.get("EVEROS_USER_ID", ""))
        digest = hashlib.sha256(identity.encode()).hexdigest()
        self.path = state / "quotas" / f"{digest}-{resource}.json"

    @classmethod
    def for_path(cls, state, config, path):
        resource = "mcu" if path in WRITE_PATHS else "retrieval" if path in READ_PATHS else None
        return cls(state, config, resource) if resource else None

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(state, dict) or state.get("resource") != self.resource:
            return {}
        if number(state.get("retry_at")) is None or number(state.get("probe_until", 0)) is None:
            return {}
        return state

    def save(self, state):
        tmp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def before(self, timeout):
        """Healthy traffic stays concurrent; only a recovering circuit leases a probe."""
        with self.locked():
            state = self.load()
            if not state:
                return None
            now = time.time()
            if state["retry_at"] > now or state.get("probe_until", 0) > now:
                raise QuotaError(self.resource, state.get("balance"), state["retry_at"], cached=True)
            token = uuid.uuid4().hex
            state.update(probe=token, probe_until=now + max(1, float(timeout)) + 5)
            self.save(state)
            return token

    def block(self, balance, headers=None):
        delay = COOLDOWN
        try:
            retry_after = float((headers or {}).get("Retry-After", "0"))
            if math.isfinite(retry_after) and retry_after > 0:
                delay = max(delay, min(retry_after, 86400))
        except (TypeError, ValueError):
            pass
        with self.locked():
            previous = self.load()
            state = {"resource": self.resource, "balance": number(balance),
                     "retry_at": time.time() + delay, "probe_until": 0}
            self.save(state)
        return QuotaError(self.resource, balance, state["retry_at"]), not bool(previous)

    def finish(self, token, success):
        if token is None:
            return
        with self.locked():
            state = self.load()
            if state.get("probe") != token:
                return  # A late result must not clear a newer refusal/probe.
            if success:
                self.path.unlink()
            else:
                state.update(probe_until=0, retry_at=time.time() + COOLDOWN)
                state.pop("probe", None)
                self.save(state)

    def status(self):
        with self.locked():
            state = self.load()
            if not state:
                return {"resource": self.resource, "paused": False}
            return {"resource": self.resource, "paused": True, "balance": number(state.get("balance")),
                    "retry_at": state["retry_at"], "probe_in_flight": state.get("probe_until", 0) > time.time()}

    def retry(self):
        """Operator asks for one probe after top-up, never for a burst of retries."""
        with self.locked():
            state = self.load()
            if not state:
                return False
            # Preserve an in-flight probe lease rather than allowing a second one.
            state["retry_at"] = 0
            self.save(state)
            return True


def parse_daily_budget(config: dict, *, strict: bool = False) -> int:
    """Parse EVEROS_DAILY_WRITE_BUDGET (0 disables soft cap; default 40 auto add batches/day)."""
    raw = config.get("EVEROS_DAILY_WRITE_BUDGET")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_DAILY_BUDGET
    if isinstance(raw, bool):
        if strict:
            raise ValueError("EVEROS_DAILY_WRITE_BUDGET 须为非负整数")
        return DEFAULT_DAILY_BUDGET
    text = str(raw).strip()
    if not text.isdigit():
        if strict:
            raise ValueError("EVEROS_DAILY_WRITE_BUDGET 须为非负整数")
        return DEFAULT_DAILY_BUDGET
    return int(text)


class DailyBudget:
    """Credential+user-scoped soft daily cap for automatic background writes.

    Reaching the cap downgrades background dialogue capture to curated-only for
    the rest of the CST day; explicit remember / practice / fix_profile / flush
    writes are never blocked by this soft budget.
    """

    def __init__(self, state: Path, config: dict):
        self.limit = parse_daily_budget(config, strict=False)
        identity = (config.get("EVEROS_API_BASE", "").rstrip("/") + "\0"
                    + (config.get("EVEROS_API_KEY") or "proxy:" + config.get("EVEROS_USER_ID", ""))
                    + "\0" + config.get("EVEROS_USER_ID", ""))
        digest = hashlib.sha256(identity.encode()).hexdigest()
        self.path = state / "quotas" / f"{digest}-daily.json"

    @staticmethod
    def today() -> str:
        return datetime.now(CST).strftime("%Y-%m-%d")

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _fresh(self, day: str) -> dict:
        return {"resource": "daily_auto_write", "day": day, "auto_adds": 0,
                "auto_messages": 0, "auto_chars": 0, "curated_writes": 0, "downgraded": False}

    def _load(self, day: str) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._fresh(day)
        if not isinstance(raw, dict) or raw.get("resource") != "daily_auto_write" or raw.get("day") != day:
            return self._fresh(day)
        out = self._fresh(day)
        for key in ("auto_adds", "auto_messages", "auto_chars", "curated_writes"):
            val = raw.get(key)
            if type(val) is int and val >= 0:
                out[key] = val
        out["downgraded"] = bool(raw.get("downgraded"))
        return out

    def _save(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def allow_auto(self) -> tuple[bool, bool]:
        """Return (allowed, newly_downgraded)."""
        if self.limit <= 0:
            return True, False
        day = self.today()
        with self.locked():
            data = self._load(day)
            if data["auto_adds"] < self.limit:
                return True, False
            newly = not data["downgraded"]
            if newly:
                data["downgraded"] = True
                self._save(data)
            return False, newly

    def record_auto(self, adds: int, messages: int, chars: int) -> dict:
        day = self.today()
        with self.locked():
            data = self._load(day)
            data["auto_adds"] += max(0, int(adds))
            data["auto_messages"] += max(0, int(messages))
            data["auto_chars"] += max(0, int(chars))
            self._save(data)
            return data

    def record_curated(self, writes: int = 1) -> None:
        day = self.today()
        with self.locked():
            data = self._load(day)
            data["curated_writes"] += max(0, int(writes))
            self._save(data)

    def status(self) -> dict:
        day = self.today()
        if not self.path.is_file():
            data = self._fresh(day)
        else:
            with self.locked():
                data = self._load(day)
        exhausted = self.limit > 0 and data["auto_adds"] >= self.limit
        return {
            "day": day,
            "budget": self.limit,
            "enabled": self.limit > 0,
            "auto_adds": data["auto_adds"],
            "auto_messages": data["auto_messages"],
            "auto_chars": data["auto_chars"],
            "curated_writes": data["curated_writes"],
            "downgraded": exhausted or bool(data["downgraded"]),
        }
