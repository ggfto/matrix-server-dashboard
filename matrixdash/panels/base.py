"""Every block of the screen is a Panel: it draws itself into a rectangle of the canvas.

Panels read the Snapshot and the dashboard state (self.d) and never collect data.
Full-screen layers (boot, header, key bar, overlays) get the whole canvas as rectangle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Snapshot
from ..render.canvas import Canvas

if TYPE_CHECKING:
    from ..dashboard import Dashboard


class Panel:
    def __init__(self, dash: Dashboard):
        self.d = dash

    def draw(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, h: int, t: float):
        raise NotImplementedError
