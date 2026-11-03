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
from pathlib import Path

try:
    import fcntl
except ImportError:  # Generated Windows runtime supplies the same lock interface.
    import windows_lock as fcntl

COOLDOWN = 900
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
