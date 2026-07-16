"""Review dashboard: approve/reject prepared applications.

Deliberately submits nothing itself — approving marks rows APPROVED;
submission happens only via `python app.py apply <platform>`.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from core.models import State
from core.queue import Queue

TEMPLATES = Path(__file__).parent / "templates"

app = FastAPI(title="Job Applier — Review")
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)


def q() -> Queue:
    return Queue()


@app.get("/", response_class=HTMLResponse)
def index():
    queue = q()
    pending = [i.to_dict() for i in queue.items(State.PENDING_REVIEW)]
    approved = [i.to_dict() for i in queue.items(State.APPROVED)]
    recent = [i.to_dict() for i in queue.items(State.APPLIED)][:20]
    failed = [i.to_dict() for i in queue.items(State.FAILED)][:20]
    tpl = env.get_template("index.html")
    return tpl.render(pending=pending, approved=approved, recent=recent,
                      failed=failed, counts=queue.counts())


@app.post("/decide/{app_id}/{decision}")
def decide(app_id: int, decision: str):
    if decision not in ("approve", "reject"):
        return RedirectResponse("/", status_code=303)
    q().set_state(app_id,
                  State.APPROVED if decision == "approve" else State.REJECTED)
    return RedirectResponse("/", status_code=303)


@app.post("/decide-all/{decision}")
def decide_all(decision: str):
    queue = q()
    if decision in ("approve", "reject"):
        target = State.APPROVED if decision == "approve" else State.REJECTED
        for item in queue.items(State.PENDING_REVIEW):
            # Bulk-approve only items with no unanswered questions
            if decision == "reject" or not item.application.unanswered:
                queue.set_state(item.app_id, target)
    return RedirectResponse("/", status_code=303)
