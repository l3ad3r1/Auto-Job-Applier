"""Create and verify the local Excel job board used by Hermes routines."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .queue import Queue, ROOT

BOARD_DIR = ROOT / "outputs" / "job-board-20260820"
BOARD_PATH = BOARD_DIR / "job_board.xlsx"
MANIFEST_PATH = BOARD_DIR / "job_board_manifest.json"
BUILDER = ROOT / "hermes" / "job-board-runtime" / "job-board-builder.mjs"
BUNDLED_NODE = (Path.home() / ".cache" / "codex-runtimes" /
                "codex-primary-runtime" / "dependencies" / "node" /
                "bin" / "node.exe")


def _node() -> Path | str:
    if BUNDLED_NODE.exists():
        return BUNDLED_NODE
    found = shutil.which("node")
    if found:
        return found
    raise RuntimeError("Node.js runtime not found; Excel board was not updated")


def build_job_board(queue: Queue | None = None,
                    output: Path = BOARD_PATH,
                    preview_dir: Path | None = None) -> dict:
    """Rebuild the workbook atomically from SQLite's current state."""
    queue = queue or Queue()
    rows = queue.job_board_rows()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {"generated_at": generated_at, "rows": rows}
    output.parent.mkdir(parents=True, exist_ok=True)

    payload_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="job-board-",
                dir=output.parent, delete=False, encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            payload_path = Path(f.name)
        temp_output = output.with_suffix(".xlsx.building")
        args = [str(_node()), str(BUILDER), str(payload_path), str(temp_output)]
        if preview_dir:
            args.extend(["--preview-dir", str(preview_dir)])
        proc = subprocess.run(args, cwd=str(ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=240)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or
                                "Excel builder failed").strip())
        os.replace(temp_output, output)
    finally:
        if payload_path:
            payload_path.unlink(missing_ok=True)

    # The user's tracker, as the first sheet. Runs after the Node builder has
    # written the workbook, so it is the last thing to touch the file. Never
    # fatal: a broken tracker must not cost us the whole board.
    try:
        from core.tracker_sheet import refresh as refresh_tracker
        import openpyxl
        wb = openpyxl.load_workbook(output)
        tracker = refresh_tracker(wb, queue.db_path)
        wb.save(output)
        manifest_tracker = tracker
    except Exception as e:  # noqa: BLE001 - board is still valid without it
        manifest_tracker = {"error": f"{type(e).__name__}: {e}"}

    counts = dict(sorted(Counter(row["state"] for row in rows).items()))
    suitable_states = {"pending_review", "approved", "applied"}
    fresh_hiring_suitable = sum(
        1 for row in rows
        if row.get("source_tag") == "linkedin_today_hiring"
        and row["state"] in suitable_states)
    manifest = {
        "generated_at": generated_at,
        "workbook": str(output),
        "total_jobs": len(rows),
        "counts": counts,
        "fresh_hiring_suitable": fresh_hiring_suitable,
        "size_bytes": output.stat().st_size,
        "tracker": manifest_tracker,
    }
    manifest_path = output.with_name("job_board_manifest.json")
    temp_manifest = manifest_path.with_suffix(".json.building")
    temp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(temp_manifest, manifest_path)
    return manifest
