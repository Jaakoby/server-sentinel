#!/usr/bin/env python3
"""
Server Sentinel — keep a modded Minecraft server up, without crash-looping.

Every hosting panel will restart your server when it dies. None of them notice
that it is dying the *same way* every time. So a single bad mod turns into a
restart loop that runs all night, fills the disk with crash reports, and is
still broken in the morning.

Sentinel watches the server's own output, works out *why* it stopped, and
refuses to restart into a failure it has already seen. It stops, tells you what
the signature was, and waits.

    python sentinel.py guard "java @user_jvm_args.txt @libraries/.../win_args.txt nogui"
    python sentinel.py watch logs/latest.log

No dependencies. No install. Python 3.8+.

Copyright (c) 2026. Sold as-is under the licence in LICENSE.txt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

VERSION = "1.0.0-free"

UPGRADE_NOTICE = """
  ──────────────────────────────────────────────────────────────────────
  FREE edition: `watch` only — read-only, changes nothing.

  It tells you a crash has happened before. The full version acts on
  that: `guard` supervises the server, restarts it when it dies, and
  REFUSES to restart into a crash signature it has already seen — so one
  bad mod stops being an all-night loop that fills your disk with
  identical crash reports. Plus webhook alerts and a JSON event log.

  https://kaiven.gumroad.com/l/server-sentinel
  ──────────────────────────────────────────────────────────────────────
"""


# How many lines after a crash marker to read before signing the crash.
# The cause and the first non-vanilla frame are always well inside this.
CRASH_TRACE_LINES = 60

BAR = "=" * 72
THIN = "-" * 72

# ---------------------------------------------------------------------------
# Events worth noticing in a server's output
# ---------------------------------------------------------------------------

UP = "UP"
DOWN = "DOWN"
CRASH = "CRASH"
HANG = "HANG"
OOM = "OOM"
LAG = "LAG"
JOIN = "JOIN"
LEAVE = "LEAVE"
CHAT = "CHAT"

# (event, compiled pattern, severity)  severity: 3 fatal, 2 bad, 1 notable, 0 info
EVENT_PATTERNS: List[Tuple[str, "re.Pattern", int]] = [
    (UP,    re.compile(r"Done \((?P<secs>[\d.]+)s\)! For help"), 0),
    (DOWN,  re.compile(r"Stopping (?:the )?server"), 1),
    (OOM,   re.compile(r"java\.lang\.OutOfMemoryError: (?P<what>[\w ]+)"), 3),
    (HANG,  re.compile(r"A single server tick took (?P<secs>[\d.]+) seconds"), 3),
    (HANG,  re.compile(r"Considering it to be crashed, server will forcibly shutdown"), 3),
    (CRASH, re.compile(r"Preparing crash report|This crash report has been saved to"), 3),
    (CRASH, re.compile(r"Exception (?:in server tick loop|during server start|stopping the server)"), 3),
    (LAG,   re.compile(r"Can't keep up!.*?Running (?P<ms>\d+)ms or (?P<ticks>\d+) ticks behind"), 1),
    (JOIN,  re.compile(r"(?P<who>[A-Za-z0-9_]{3,16}) joined the game"), 0),
    (LEAVE, re.compile(r"(?P<who>[A-Za-z0-9_]{3,16}) left the game"), 0),
]

# How bad a lag spike has to be before it is worth waking someone for.
LAG_WARN_MS = 5000
LAG_ALERT_MS = 15000

# Lines that identify the cause of a crash, in priority order. The first match
# becomes part of the crash signature.
CAUSE_PATTERNS = [
    re.compile(r"Caused by: (?P<cause>[\w$.]+(?:Error|Exception)[^\n]{0,160})"),
    re.compile(r"^(?P<cause>[\w$.]+(?:Error|Exception)[^\n]{0,160})", re.M),
    re.compile(r"Description: (?P<cause>[^\n]{1,120})"),
]

# Frames that never identify a culprit (same list the Doctor uses).
_VANILLA = re.compile(
    r"(server-1\.|client-1\.|forge-\d|neoforge-\d|^java\.|^jdk\.|^sun\.|"
    r"fmlcore|fmlloader|modlauncher|securejarhandler|bootstraplauncher|"
    r"^net\.minecraftforge|^net\.neoforged|^cpw\.mods|"
    r"netty-|^io\.netty|^com\.google|^org\.apache|^com\.mojang|eventbus-|"
    r"brigadier-|guava-|mixin-|log4j|slf4j|gson-|commons-|asm-)",
    re.I,
)
FRAME_RE = re.compile(r"^\s+at ([\w$.]+)\([^)]*\)\s*~?\[([^\]]+)\]")


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


class Event:
    __slots__ = ("kind", "severity", "text", "detail", "when")

    def __init__(self, kind: str, severity: int, text: str, detail: Dict[str, str]):
        self.kind = kind
        self.severity = severity
        self.text = text.strip()
        self.detail = detail
        self.when = now()


def classify(line: str) -> Optional[Event]:
    for kind, pat, sev in EVENT_PATTERNS:
        m = pat.search(line)
        if not m:
            continue
        detail = {k: v for k, v in (m.groupdict() or {}).items() if v}
        if kind == LAG:
            ms = int(detail.get("ms", 0))
            if ms < LAG_WARN_MS:
                return None                      # ordinary chunk-load stutter
            sev = 2 if ms >= LAG_ALERT_MS else 1
        return Event(kind, sev, line, detail)
    return None


# ---------------------------------------------------------------------------
# Crash signatures — the whole point
# ---------------------------------------------------------------------------

def crash_signature(recent: List[str]) -> Tuple[str, str, Optional[str]]:
    """Reduce a crash to a stable fingerprint.

    Returns (signature, cause line, culprit jar). Two crashes with the same
    signature are the same crash, so restarting will not help.
    """
    blob = "\n".join(recent)

    cause = ""
    for pat in CAUSE_PATTERNS:
        m = pat.search(blob)
        if m:
            cause = m.group("cause").strip()
            break

    culprit = None
    for line in recent:
        m = FRAME_RE.match(line)
        if not m:
            continue
        symbol, origin = m.group(1), m.group(2)
        jar = origin.split("%")[0].split("!")[0]
        if _VANILLA.search(jar) or _VANILLA.search(symbol) or not jar.endswith(".jar"):
            continue
        culprit = jar
        break

    # Strip numbers out of the cause so line numbers and coordinates do not
    # make two instances of the same crash look different.
    stable = re.sub(r"\d+", "N", cause)
    sig = hashlib.sha256(f"{stable}|{culprit or ''}".encode()).hexdigest()[:12]
    return sig, cause, culprit


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

class Alerter:
    """Console only. The paid edition also posts to a webhook."""

    COLORS = {3: "\033[91m", 2: "\033[93m", 1: "\033[96m", 0: "\033[90m"}
    RESET = "\033[0m"

    def __init__(self, webhook: None = None, quiet: bool = False,
                 jsonl: Optional[str] = None):
        self.webhook = webhook
        self.quiet = quiet
        self.jsonl = jsonl
        self._color = sys.stdout.isatty() and os.name != "nt"

    def say(self, severity: int, title: str, body: str = "") -> None:
        if not self.quiet or severity >= 2:
            c = self.COLORS.get(severity, "") if self._color else ""
            r = self.RESET if self._color else ""
            print(f"{c}[{now()}] {title}{r}")
            if body:
                for line in body.rstrip().split("\n"):
                    print(f"           {line}")
            sys.stdout.flush()

        if self.jsonl:
            try:
                with open(self.jsonl, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                         "severity": severity, "title": title,
                                         "body": body}) + "\n")
            except OSError:
                pass


    def _discord(self, title: str, body: str, severity: int) -> None:
        colour = {3: 0xE4705E, 2: 0xD9A743}.get(severity, 0x7FB574)
        payload = {"embeds": [{
            "title": title[:256],
            "description": (body or "")[:3900],
            "color": colour,
            "footer": {"text": f"Server Sentinel {VERSION}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]}
        req = urllib.request.Request(
            self.webhook, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "ServerSentinel"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except (urllib.error.URLError, OSError) as e:
            # An alerting failure must never take the server down with it.
            print(f"[{now()}] (webhook failed: {e})")


# ---------------------------------------------------------------------------
# watch — follow a log file
# ---------------------------------------------------------------------------

def follow(path: str, from_start: bool = False, idle_marks: bool = False):
    """Yield lines as they are appended, surviving log rotation.

    With idle_marks, yields None whenever it has caught up and is waiting. A
    caller that buffers lines after a crash marker needs that: otherwise a crash
    at the end of the file is never flushed, because the next line never comes.
    """
    while not os.path.exists(path):
        time.sleep(1)
    fh = open(path, "r", encoding="utf-8", errors="replace")
    if not from_start:
        fh.seek(0, os.SEEK_END)
    inode = os.fstat(fh.fileno()).st_ino if hasattr(os, "fstat") else None
    try:
        while True:
            line = fh.readline()
            if line:
                yield line
                continue
            if idle_marks:
                yield None
            time.sleep(0.4)
            try:
                st = os.stat(path)
                # Rotated (new file) or truncated -> reopen from the top.
                if (inode is not None and st.st_ino != inode) or st.st_size < fh.tell():
                    fh.close()
                    fh = open(path, "r", encoding="utf-8", errors="replace")
                    inode = os.fstat(fh.fileno()).st_ino
            except OSError:
                pass
    finally:
        fh.close()


def cmd_watch(args) -> int:
    al = Alerter(None, args.quiet, None)
    print(BAR)
    print(f" SERVER SENTINEL {VERSION} — watching {args.logfile}")
    print(BAR)
    recent: Deque[str] = deque(maxlen=400)
    players: Dict[str, str] = {}
    # How many times each crash signature has been seen in this run. Reporting
    # "this is the same crash as 20 minutes ago" is the whole point -- a list of
    # crashes tells you the server is down, which you knew.
    seen_sigs: Dict[str, int] = {}

    # A crash marker ("Exception in server tick loop") arrives BEFORE the
    # exception and stack trace it introduces. Signing at the marker produced a
    # signature of the wrong text -- and on a second crash, of the PREVIOUS
    # crash's trace still sitting in `recent`. So buffer what follows, then sign.
    pending: Optional[List[str]] = None

    def flush_crash() -> None:
        nonlocal pending
        if pending is None:
            return
        sig, cause, culprit = crash_signature(pending)
        seen_sigs[sig] = seen_sigs.get(sig, 0) + 1
        n = seen_sigs[sig]
        body = f"cause:   {cause or 'unknown'}\n"
        if culprit:
            body += f"culprit: {culprit}\n"
        body += f"sig:     {sig}"
        if n > 1:
            body += (f"\n\nTHIS IS THE SAME CRASH AS {n - 1} TIME(S) ALREADY.\n"
                     f"Restarting will reproduce it. Fix the cause above first.")
        al.say(3, "CRASH" if n == 1 else f"CRASH (repeat #{n})", body)
        pending = None

    for line in follow(args.logfile, args.from_start, idle_marks=True):
        if line is None:                 # caught up -- flush anything buffered
            flush_crash()
            continue

        recent.append(line.rstrip("\n"))

        ev = classify(line)

        if pending is not None:
            # The trace ends when the next real event starts -- the server coming
            # back up, stopping, or crashing again. Swallowing those lost the
            # second crash of a loop entirely, which is the one that matters.
            if ev is not None and ev.kind in (UP, DOWN, CRASH):
                flush_crash()
            else:
                pending.append(line.rstrip("\n"))
                if len(pending) >= CRASH_TRACE_LINES:
                    flush_crash()
                continue

        if not ev:
            continue

        if ev.kind == UP:
            al.say(1, f"Server is up ({ev.detail.get('secs','?')}s)")
        elif ev.kind == DOWN:
            al.say(1, "Server is stopping")
        elif ev.kind == JOIN:
            who = ev.detail.get("who", "?")
            players[who] = now()
            al.say(0, f"{who} joined ({len(players)} online)")
        elif ev.kind == LEAVE:
            who = ev.detail.get("who", "?")
            players.pop(who, None)
            al.say(0, f"{who} left ({len(players)} online)")
        elif ev.kind == LAG:
            al.say(ev.severity, f"Lag spike — {ev.detail.get('ms')}ms behind "
                                f"({ev.detail.get('ticks')} ticks)")
        elif ev.kind == OOM:
            al.say(3, "OUT OF MEMORY", ev.text[:300])
        elif ev.kind == HANG:
            al.say(3, "Server hung — watchdog fired", ev.text[:300])
        elif ev.kind == CRASH:
            # Start buffering; the trace is on the lines after this one.
            pending = [line.rstrip("\n")]
    return 0




def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sentinel",
        description="Keep a modded Minecraft server up without crash-looping.")
    ap.add_argument("--version", action="version", version=f"server-sentinel {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", action="store_true", help="only print severity 2+")

    w = sub.add_parser("watch", parents=[common], help="follow a log file read-only")
    w.add_argument("logfile")
    w.add_argument("--from-start", action="store_true", help="read the whole file first")
    w.set_defaults(func=cmd_watch)


    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
