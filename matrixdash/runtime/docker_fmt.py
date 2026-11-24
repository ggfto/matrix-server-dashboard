"""Parsing of docker CLI output."""

from __future__ import annotations

import re

AGE_RE = re.compile(r"(\d+|About an?|Less than a|an?)\s+(second|minute|hour|day|week|month|year)", re.I)
AGE_UNITS = {"second": "s", "minute": "m", "hour": "h", "day": "d", "week": "w", "month": "mo", "year": "y"}
PORT_RE = re.compile(r"(0\.0\.0\.0|\[::\]|127\.0\.0\.1):(\d+)->")


def short_age(status: str) -> str:
    match = AGE_RE.search(status)
    if not match:
        return "-"
    num = match.group(1)
    if num.isdigit():
        n = num
    elif num.lower().startswith("less"):
        n = "<1"
    else:
        n = "1"
    return f"{n}{AGE_UNITS[match.group(2).lower()]}"


def short_ports(ports: str) -> str:
    seen: list[str] = []
    for host, port in PORT_RE.findall(ports):
        label = f"lo:{port}" if host == "127.0.0.1" else f":{port}"
        if label not in seen and f":{port}" not in seen:
            seen.append(label)
    return ",".join(seen[:2]) if seen else "-"
