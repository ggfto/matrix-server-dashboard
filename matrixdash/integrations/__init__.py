"""Optional features behind one interface (see base.Integration)."""

from __future__ import annotations

from .argocd import ArgoCDIntegration
from .backup import BackupIntegration
from .base import Integration
from .claude import ClaudeIntegration
from .icloud import ICloudIntegration
from .tailscale import TailscaleIntegration

# Order matters on screen: ticker items, alerts, boot lines and menu sections follow it.
ALL: list[type[Integration]] = [BackupIntegration, ClaudeIntegration, ICloudIntegration, TailscaleIntegration,
                                ArgoCDIntegration]

__all__ = ["ALL", "ArgoCDIntegration", "BackupIntegration", "ClaudeIntegration", "ICloudIntegration", "Integration",
           "TailscaleIntegration"]
