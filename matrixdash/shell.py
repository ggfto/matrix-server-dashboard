"""Running external commands."""

from __future__ import annotations

import subprocess


def run_cmd(args: list[str], timeout: float = 2.0) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except Exception:
        return 1, ""
