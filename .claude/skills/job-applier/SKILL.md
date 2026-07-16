---
name: job-applier
description: Run the auto job-applier pipeline — discover jobs on LinkedIn/Naukri/Indeed, prepare answers, report the review queue, and submit user-approved applications. Use when asked to "run the job applier", check application status, or apply to approved jobs.
---

# Job Applier routine

Follow [AGENTS.md](../../../AGENTS.md) at the repo root — it defines the
commands, the four-step routine, and the hard rules (never self-approve,
never edit profile/config, never bypass caps or CAPTCHAs, never touch
browser profiles or credentials).

Quick reference:

```bash
.venv/Scripts/python.exe app.py discover linkedin   # then naukri, indeed — sequential
.venv/Scripts/python.exe app.py prepare
.venv/Scripts/python.exe app.py status
.venv/Scripts/python.exe app.py apply <platform>    # APPROVED items only
```

End with a summary: applied per platform (job titles), failures with one-line
reasons, pending_review count, and a dashboard reminder
(`python app.py review` → http://127.0.0.1:8377) when items await approval.
