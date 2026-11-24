"""Container runtimes behind one interface (see base.ContainerRuntime)."""

from __future__ import annotations

from typing import Any

from .base import ContainerRuntime, Endpoint, Network
from .docker import DockerRuntime

__all__ = ["ContainerRuntime", "DockerRuntime", "Endpoint", "Network", "from_config"]

RUNTIMES: dict[str, type[ContainerRuntime]] = {DockerRuntime.name: DockerRuntime}


def from_config(config: dict[str, Any] | None = None) -> list[ContainerRuntime]:
    """Runtimes listed in config["runtimes"]; Docker when the key is missing."""
    names = (config or {}).get("runtimes") or [DockerRuntime.name]
    return [RUNTIMES[n]() for n in names if n in RUNTIMES]
