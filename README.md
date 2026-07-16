# Job Applier

Personal auto job-application tool with a **review queue**: it discovers jobs, prepares
applications (tailored answers, resume selection), and waits for your approval in a local
dashboard before submitting anything.

## Design principles

- **You log in, not the bot.** Playwright uses a persistent browser profile per platform
  (`profiles/<platform>/`). Run `python app.py login linkedin` once and sign in manually;
  the session is reused afterwards. The tool never sees or stores credentials.
- **Nothing is submitted without approval.** Pipeline states:
  `discovered → prepared → pending_review → approved → applied` (or `rejected` / `failed`).
- **Rate-limited by design.** Per-platform daily caps and randomized human-like delays
  (`safety.py`). LinkedIn/Indeed ToS prohibit automation — keep caps conservative.

## Usage

```bash
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml     # edit: search terms, locations, caps
cp profile.example.yaml profile.yaml   # edit: your details + screening answers

python app.py login linkedin      # one-time manual sign-in
python app.py discover linkedin   # scrape matching jobs into the queue
python app.py prepare             # draft answers for queued jobs
python app.py review              # open dashboard at http://127.0.0.1:8377
python app.py apply linkedin      # submit approved applications, paced
python app.py status              # queue counts per state
```

## Layout

- `core/` — models, SQLite queue, profile loader
- `adapters/` — one module per platform (LinkedIn first; Indeed, Naukri, Wellfound planned)
- `llm/` — answer drafting / resume tailoring (Phase 2)
- `dashboard/` — FastAPI review UI
- `safety.py` — caps and pacing
- `_references/` — cloned upstream projects studied for selectors/patterns (gitignored)

## Phases

1. ✅ LinkedIn Easy Apply end-to-end with review queue
2. LLM resume/cover tailoring per job
3. Indeed adapter
4. Naukri adapter
5. Wellfound/RemoteOK + daily digest
