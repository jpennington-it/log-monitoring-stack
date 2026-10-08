#!/usr/bin/env python3
"""Synthetic log generator for the log-monitoring-stack lab.

Writes two files that Promtail tails:

* ``auth.log``   - sshd authentication events in syslog format with RFC 3339
  timestamps (the default on recent Ubuntu/Debian releases).
* ``webapp.log`` - JSON-lines HTTP access logs from a pretend web app.

Every so often it emits a *burst* of failed SSH logins from a single source
address to simulate a brute-force attempt, so the alert rules and dashboard
have something to show. All addresses come from the RFC 5737 documentation
ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) or RFC 1918 private
space, and all host/user names are made up.

Standard library only. Examples::

    python3 generate_logs.py --output-dir ../sample-logs            # run forever
    python3 generate_logs.py --output-dir ../sample-logs --count 500 --burst-now
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, TextIO

HOSTS = ["lab-web01", "lab-web02", "lab-jump01"]
STAFF_USERS = ["jdoe", "asmith", "deploy", "backup"]
PROBE_USERS = ["admin", "root", "test", "oracle", "ubuntu", "pi", "guest", "support"]
INTERNAL_CLIENTS = [f"192.168.56.{n}" for n in (10, 11, 12, 25)]
EXTERNAL_SOURCES = [f"203.0.113.{n}" for n in (14, 37, 88, 142)] + [f"198.51.100.{n}" for n in (7, 61)]
PATHS = ["/", "/login", "/api/orders", "/api/orders/42", "/api/health", "/static/app.js", "/reports"]
METHODS = ["GET"] * 8 + ["POST"] * 2

#: Share of background SSH events that are failures. Kept low so the alert
#: thresholds are only crossed by the simulated bursts.
FAIL_RATIO = 0.08


def now_rfc3339() -> str:
    """Current local time as RFC 3339 with microseconds and offset."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


def sshd_line(rng: random.Random, ts: str, src: Optional[str] = None, fail: Optional[bool] = None) -> str:
    """Return one sshd syslog line.

    Args:
        rng: Random source (seedable for tests).
        ts: Timestamp string to use.
        src: Force a source IP (used for bursts).
        fail: Force a failed (True) or successful (False) login.
    """
    host = rng.choice(HOSTS)
    pid = rng.randint(1000, 65000)
    port = rng.randint(30000, 65000)
    if fail is None:
        fail = rng.random() < FAIL_RATIO
    if fail:
        src = src or rng.choice(EXTERNAL_SOURCES + INTERNAL_CLIENTS)
        if rng.random() < 0.4:
            user = rng.choice(PROBE_USERS)
            msg = f"Failed password for invalid user {user} from {src} port {port} ssh2"
            if rng.random() < 0.5:
                msg = f"Invalid user {user} from {src} port {port}"
        else:
            msg = f"Failed password for {rng.choice(STAFF_USERS)} from {src} port {port} ssh2"
    else:
        src = src or rng.choice(INTERNAL_CLIENTS)
        user = rng.choice(STAFF_USERS)
        msg = rng.choice([
            f"Accepted publickey for {user} from {src} port {port} ssh2: ED25519 SHA256:EXAMPLEKEYFINGERPRINT",
            f"Disconnected from user {user} {src} port {port}",
        ])
    return f"{ts} {host} sshd[{pid}]: {msg}"


def webapp_line(rng: random.Random, ts: str) -> str:
    """Return one JSON access-log line."""
    roll = rng.random()
    status = 500 if roll < 0.02 else 503 if roll < 0.03 else 404 if roll < 0.08 else 401 if roll < 0.1 else 200
    level = "error" if status >= 500 else "warn" if status >= 400 else "info"
    record = {
        "time": ts,
        "level": level,
        "host": rng.choice(HOSTS[:2]),
        "method": rng.choice(METHODS),
        "path": rng.choice(PATHS),
        "status": status,
        "duration_ms": round(rng.lognormvariate(3.3, 0.6), 1),
        "client_ip": rng.choice(INTERNAL_CLIENTS),
    }
    return json.dumps(record, separators=(",", ":"))


def burst_lines(rng: random.Random, size: int = 40) -> List[str]:
    """Return a burst of failed logins from one external address (simulated brute force)."""
    src = rng.choice(EXTERNAL_SOURCES)
    return [sshd_line(rng, now_rfc3339(), src=src, fail=True) for _ in range(size)]


def write_line(fh: TextIO, line: str) -> None:
    fh.write(line + "\n")
    fh.flush()


def run(output_dir: Path, rate: float, burst_every: float, count: Optional[int],
        burst_now: bool, seed: Optional[int], sleep: Callable[[float], None] = time.sleep) -> int:
    """Generate logs until ``count`` regular SSH events are written (or forever if None).

    Burst lines are extra and don't count toward ``count``. Returns the total
    number of SSH lines written (regular + burst).
    """
    rng = random.Random(seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    auth_path, web_path = output_dir / "auth.log", output_dir / "webapp.log"
    written = 0
    events = 0
    last_burst = time.monotonic()
    with auth_path.open("a", encoding="utf-8") as auth, web_path.open("a", encoding="utf-8") as web:
        if burst_now:
            for line in burst_lines(rng):
                write_line(auth, line)
                written += 1
        while count is None or events < count:
            write_line(auth, sshd_line(rng, now_rfc3339()))
            written += 1
            events += 1
            for _ in range(rng.randint(1, 4)):
                write_line(web, webapp_line(rng, now_rfc3339()))
            if burst_every > 0 and time.monotonic() - last_burst >= burst_every:
                for line in burst_lines(rng):
                    write_line(auth, line)
                    written += 1
                last_burst = time.monotonic()
                print(f"{now_rfc3339()} emitted simulated brute-force burst", flush=True)
            if rate > 0:
                sleep(1.0 / rate)
    return written


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output-dir", type=Path, default=Path("sample-logs"),
                        help="directory for auth.log and webapp.log (default: ./sample-logs)")
    parser.add_argument("--rate", type=float, default=0.5,
                        help="SSH events per second; 0 = as fast as possible (default: 0.5)")
    parser.add_argument("--burst-every", type=float, default=600,
                        help="seconds between simulated brute-force bursts; 0 disables (default: 600)")
    parser.add_argument("--burst-now", action="store_true", help="emit one burst immediately at start")
    parser.add_argument("--count", type=int, default=None,
                        help="stop after this many regular SSH events (default: run until stopped)")
    parser.add_argument("--seed", type=int, default=None, help="random seed for repeatable output")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean stop under `docker compose stop`
    print(f"writing synthetic logs to {args.output_dir.resolve()}", flush=True)
    try:
        n = run(args.output_dir, args.rate, args.burst_every, args.count, args.burst_now, args.seed)
    except KeyboardInterrupt:
        return 0
    print(f"wrote {n} sshd lines", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
