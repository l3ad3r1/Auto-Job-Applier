"""Append newly-applied jobs to a Google Sheet via an Apps Script webhook.

Deliberately self-contained (stdlib only, no OAuth, no service account) so it
runs unattended from the deterministic `routine`. Configure a Google Apps
Script web-app URL + shared secret in config.yaml; see hermes/sheets-setup.md
for the one-time setup and the Apps Script to paste.

No-op (returns a status string) when disabled or unconfigured, so it is safe
to call every run before the sheet is wired up.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from core.queue import Queue

# Column order must match cmd_export's APPLIED_COLUMNS / the sheet header.
COLUMNS = ["Date Applied", "Platform", "Title", "Company",
           "Location", "Salary", "URL"]


def _row(item) -> list[str]:
    j, a = item.job, item.application
    return [a.updated_at[:10], j.platform, j.title, j.company,
            j.location, j.salary or "", j.url]


def sync_applied(config: dict, queue: Queue | None = None) -> str:
    """Append not-yet-synced APPLIED jobs to the sheet. Returns a status line."""
    cfg = config.get("sheets", {})
    if not cfg.get("enabled"):
        return "sheet sync: disabled"
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        return "sheet sync: no webhook_url set"

    queue = queue or Queue()
    pending = queue.unsynced_applied()
    if not pending:
        return "sheet sync: nothing new"

    payload = {
        "secret": cfg.get("secret", ""),
        "title": cfg.get("title", ""),   # rename the sheet if set (blank = leave as-is)
        "columns": COLUMNS,
        "rows": [_row(i) for i in pending],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return f"sheet sync: FAILED ({e})"
    if not body.get("ok"):
        return f"sheet sync: rejected ({body})"

    for item in pending:
        queue.mark_synced(item.app_id)
    return f"sheet sync: appended {len(pending)} row(s)"
