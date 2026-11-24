"""Small JSON files written atomically (config, state, snapshot export)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_json(path: Path, data: Any, indent: int | None = None, **dump_kw: Any):
    """Write to <path>.tmp and rename, so readers never see half a file. Indented files end with a newline."""
    text = json.dumps(data, indent=indent, ensure_ascii=False, **dump_kw)
    if indent:
        text += "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
