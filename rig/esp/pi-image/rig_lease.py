#!/usr/bin/env python3
"""rig-lease: one agent drives the HIL rig hardware at a time.

The lease is a small JSON record in ``/run/larkbridge-rig.lease`` (``--path`` or
``$RIG_LEASE_PATH`` override it; the tests use a temp path). Every read and write happens
under ``flock(2)`` on that file: ``LOCK_SH`` to read, ``LOCK_EX`` to change it. The record
is rewritten in place (truncate + write + fsync) because ``/run`` is not writable by the
rig user, so a temp-file rename is not available; the exclusive lock makes that safe.

The flock is held only while the record is read or changed. The lease itself outlives the
process, because agents drive the rig over short SSH sessions: the holder is named by
``--owner`` and the lease ends at release or expiry.

Commands (all print one JSON object on stdout):

* ``acquire --owner NAME [--ttl S] [--wait S] [--pid PID] [--note TEXT]``
  Take the lease. If NAME already holds it, this renews it (new expiry, same token).
* ``release --owner NAME [--force]``  Give it up. ``--force`` releases anyone's lease.
* ``status``  Show the lease and whether it is live.
* ``check --owner NAME``  Exit 0 only if NAME holds a live lease (for gate preconditions).

Stale-lease takeover rules. A lease is free when any of these holds:

1. the file is missing, empty or not a valid lease record (reported as ``corrupt``);
2. it was written during a different boot (``boot_id`` differs; ``/run`` is a tmpfs, so
   this matters only for a non-default path);
3. it has expired. Expiry is measured on ``CLOCK_BOOTTIME`` when the boot matches, so the
   Pi's clock step at NTP sync (it has no RTC) cannot expire or extend a lease. The UTC
   expiry in the record is informational;
4. it names a ``pid`` on this host and boot, and that process no longer exists.

Otherwise it is live, and another owner gets exit 75 (EX_TEMPFAIL), or waits with
``--wait``. A takeover records the previous holder and the reason under ``previous``.

Exit codes: 0 ok, 1 error, 2 usage, 3 not the holder (release/check), 75 busy.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

DEFAULT_PATH = "/run/larkbridge-rig.lease"
DEFAULT_TTL = 3600
MAX_TTL = 24 * 3600
SCHEMA = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_HOLDER = 3
EXIT_BUSY = 75


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return "unknown"


def _boottime() -> float:
    clock = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    return time.clock_gettime(clock)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Env:
    """Everything the lease logic reads from the machine, injectable for tests."""

    wall: Callable[[], float] = time.time
    boottime: Callable[[], float] = _boottime
    boot_id: Callable[[], str] = _boot_id
    host: Callable[[], str] = socket.gethostname
    pid_alive: Callable[[int], bool] = _pid_alive
    sleep: Callable[[float], None] = time.sleep
    new_token: Callable[[], str] = field(default=lambda: uuid.uuid4().hex)


class LeaseError(Exception):
    def __init__(self, code: int, payload: dict[str, Any]):
        super().__init__(payload.get("error", "lease error"))
        self.code = code
        self.payload = payload


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(handle) -> dict[str, Any] | None | str:
    """Return the record, None when empty, or the string 'corrupt'."""
    handle.seek(0)
    raw = handle.read()
    if not raw.strip():
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return "corrupt"
    required = {"owner", "token", "boot_id", "expires_boottime"}
    if not isinstance(record, dict) or not required <= record.keys():
        return "corrupt"
    return record


def _write(handle, record: dict[str, Any] | None) -> None:
    handle.seek(0)
    handle.truncate()
    if record is not None:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def stale_reason(record: dict[str, Any] | None | str, env: Env) -> str | None:
    """Why this lease may be taken over, or None if it is live."""
    if record is None:
        return "free"
    if record == "corrupt":
        return "corrupt"
    assert isinstance(record, dict)
    if record.get("boot_id") != env.boot_id():
        return "rebooted"
    try:
        if env.boottime() >= float(record["expires_boottime"]):
            return "expired"
    except (TypeError, ValueError):
        return "corrupt"
    pid = record.get("pid")
    if isinstance(pid, int) and pid > 0 and record.get("host") == env.host():
        if not env.pid_alive(pid):
            return "holder-exited"
    return None


def _describe(record: dict[str, Any] | None | str, env: Env) -> dict[str, Any]:
    reason = stale_reason(record, env)
    out: dict[str, Any] = {"live": reason is None}
    if isinstance(record, dict):
        out["lease"] = record
        if reason is None:
            out["remaining_s"] = round(float(record["expires_boottime"]) - env.boottime(), 1)
    if reason is not None:
        out["state"] = reason
    else:
        out["state"] = "held"
    return out


class Lease:
    def __init__(self, path: str, env: Env | None = None):
        self.path = path
        self.env = env or Env()

    def _open(self):
        # O_CREAT so a test path or a fresh /run works; provision.sh pre-creates the
        # real file with the right owner through tmpfiles.d.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o664)
        return os.fdopen(fd, "r+", encoding="utf-8")

    def status(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"live": False, "state": "free", "path": self.path}
        with open(self.path, encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            record = _read(handle)
        out = _describe(record, self.env)
        out["path"] = self.path
        return out

    def acquire(self, owner: str, ttl: float = DEFAULT_TTL, wait: float = 0.0,
                pid: int | None = None, note: str = "") -> dict[str, Any]:
        if not owner.strip():
            raise LeaseError(EXIT_USAGE, {"ok": False, "error": "owner must not be empty"})
        if not 0 < ttl <= MAX_TTL:
            raise LeaseError(EXIT_USAGE, {"ok": False, "error": f"ttl must be in (0, {MAX_TTL}]"})
        deadline = self.env.boottime() + max(wait, 0.0)
        while True:
            result = self._try_acquire(owner, ttl, pid, note)
            if result is not None:
                return result
            if self.env.boottime() >= deadline:
                raise LeaseError(EXIT_BUSY, {"ok": False, "error": "busy", **self.status()})
            self.env.sleep(min(1.0, max(deadline - self.env.boottime(), 0.05)))

    def _try_acquire(self, owner, ttl, pid, note) -> dict[str, Any] | None:
        env = self.env
        with self._open() as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            record = _read(handle)
            reason = stale_reason(record, env)
            now_wall, now_boot = env.wall(), env.boottime()
            if reason is None:
                assert isinstance(record, dict)
                if record["owner"] != owner:
                    return None
                record.update(
                    expires_boottime=now_boot + ttl,
                    expires_utc=_utc(now_wall + ttl),
                    renewed_utc=_utc(now_wall),
                )
                if pid is not None:
                    record["pid"] = pid
                if note:
                    record["note"] = note
                _write(handle, record)
                return {"ok": True, "action": "renewed", "path": self.path, "lease": record}
            new = {
                "schema": SCHEMA,
                "owner": owner,
                "token": env.new_token(),
                "host": env.host(),
                "boot_id": env.boot_id(),
                "pid": pid,
                "note": note,
                "acquired_utc": _utc(now_wall),
                "expires_utc": _utc(now_wall + ttl),
                "expires_boottime": now_boot + ttl,
            }
            if isinstance(record, dict) or record == "corrupt":
                new["previous"] = {
                    "owner": record.get("owner") if isinstance(record, dict) else None,
                    "reason": reason,
                }
            _write(handle, new)
            action = "acquired" if reason == "free" else "took-over"
            return {"ok": True, "action": action, "path": self.path, "lease": new}

    def release(self, owner: str, force: bool = False) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"ok": True, "action": "already-free", "path": self.path}
        with self._open() as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            record = _read(handle)
            reason = stale_reason(record, self.env)
            if record is None:
                return {"ok": True, "action": "already-free", "path": self.path}
            holder = record.get("owner") if isinstance(record, dict) else None
            if not force and holder != owner:
                if reason is not None:
                    # Someone else's dead lease: leave it for the takeover to record.
                    return {"ok": True, "action": "already-free", "state": reason,
                            "path": self.path}
                raise LeaseError(EXIT_NOT_HOLDER, {
                    "ok": False, "error": "not the holder", "holder": holder,
                    "path": self.path})
            _write(handle, None)
            return {"ok": True, "action": "released", "released_owner": holder,
                    "forced": bool(force and holder != owner), "path": self.path}

    def check(self, owner: str) -> dict[str, Any]:
        out = self.status()
        holder = out.get("lease", {}).get("owner")
        out["holder_matches"] = bool(out["live"] and holder == owner)
        return out


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rig-lease", description=__doc__.split("\n\n")[0])
    parser.add_argument("--path", default=os.environ.get("RIG_LEASE_PATH", DEFAULT_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--ttl", type=float, default=DEFAULT_TTL)
    acquire.add_argument("--wait", type=float, default=0.0)
    acquire.add_argument("--pid", type=int)
    acquire.add_argument("--note", default="")
    release = sub.add_parser("release")
    release.add_argument("--owner", required=True)
    release.add_argument("--force", action="store_true")
    sub.add_parser("status")
    check = sub.add_parser("check")
    check.add_argument("--owner", required=True)
    return parser


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    args = _parser().parse_args(argv)
    lease = Lease(args.path, env)
    code = EXIT_OK
    try:
        if args.command == "acquire":
            out = lease.acquire(args.owner, args.ttl, args.wait, args.pid, args.note)
        elif args.command == "release":
            out = lease.release(args.owner, args.force)
        elif args.command == "check":
            out = lease.check(args.owner)
            code = EXIT_OK if out["holder_matches"] else EXIT_NOT_HOLDER
        else:
            out = lease.status()
    except LeaseError as exc:
        out, code = exc.payload, exc.code
    except OSError as exc:
        out = {"ok": False, "error": f"{exc.strerror or exc}", "path": args.path}
        code = EXIT_ERROR
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
