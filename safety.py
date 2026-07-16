"""Rate limiting and human-like pacing.

Every submission path MUST go through Limiter.check_cap() and pace between
actions with human_delay(). Caps are hard stops, not suggestions.
"""
from __future__ import annotations

import random
import time

from core.queue import Queue


class CapReached(Exception):
    """Daily application cap hit for a platform."""


class Limiter:
    def __init__(self, config: dict, queue: Queue):
        self.limits = config.get("limits", {})
        self.queue = queue

    def check_cap(self, platform: str) -> None:
        cap = int(self.limits.get("daily_applications", {}).get(platform, 10))
        used = self.queue.submissions_today(platform)
        if used >= cap:
            raise CapReached(
                f"{platform}: daily cap reached ({used}/{cap}). Try again tomorrow.")

    def remaining(self, platform: str) -> int:
        cap = int(self.limits.get("daily_applications", {}).get(platform, 10))
        return max(0, cap - self.queue.submissions_today(platform))

    def action_delay(self) -> None:
        """Short randomized pause between clicks/keystrokes on a page."""
        lo, hi = self.limits.get("action_delay", [2.0, 6.0])
        time.sleep(random.uniform(float(lo), float(hi)))

    def application_delay(self) -> None:
        """Long randomized pause between submitted applications."""
        lo, hi = self.limits.get("application_delay", [45, 120])
        time.sleep(random.uniform(float(lo), float(hi)))
