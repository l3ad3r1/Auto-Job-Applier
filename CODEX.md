# Codex onboarding — Auto Job Applier

Orientation for an agent picking up this repo cold: what the system is, how the
Hermes scheduler drives it, how to maintain it, and what to do about
credentials (short answer: there are none to hand you, and that is deliberate).

**Read in this order:**

| File | What it gives you | Committed? |
|---|---|---|
| [AGENTS.md](AGENTS.md) | operating rules — commands, the routine, hard rules | yes |
| **CODEX.md** (this file) | how it all fits together, maintenance, credentials | yes |
| `RUNBOOK.md` | machine-specific values: cron job IDs, absolute paths, chat ID | **no — gitignored** |
| [hermes/README.md](hermes/README.md) | deploying the Hermes side | yes |

This repo is **public**. Machine-specific and personal values live in
`RUNBOOK.md` and `config.yaml`, both gitignored. Keep it that way — never move
a secret, chat ID, or absolute user path into a committed file.

Last updated: 2026-09-07.

---

## 1. What the system does

Rinu is a sales leader in Hyderabad (Branch Manager / Sales Manager / KAM / BDM
roles, no relocation). This tool finds matching jobs, drafts the screening
answers, and submits them.

```
discover  →  prepare  →  auto-approve  →  apply  →  sync-sheet
 scrape       score vs     strong ones      submit     append to
 listings     job spec,    skip review      paced +    Google Sheet
 into queue   draft answers      ↑          capped
                          everything else waits
                          for the dashboard at
                          127.0.0.1:8377
```

> **The human review gate is no longer absolute.** It was, until 2026-09-03,
> when the owner deliberately turned it off for strong matches: waiting for a
> twice-daily run meant listings expired before they were applied to. Jobs at or
> above `auto_apply.min_score` are now promoted straight to APPROVED and
> submitted **with nobody having read them**. This is the owner's decision, made
> with the trade-off stated; it is not an invitation to widen it further.
>
> Everything below that bar still waits for a human in the dashboard, and the
> rule for *you* is unchanged: **an agent never approves anything.** Your job is
> to run the CLI, interpret results, and report. See §5.

Auto-apply rails, all enforced in `cmd_auto_approve` before anything is
promoted: score at or above the platform's bar, **zero unanswered screening
questions** (never guess on a real application), easy-apply only, and
`daily_max` unreviewed promotions per day on top of the normal per-platform
caps in `safety.py`. Every promoted row is stamped in the `auto_approved`
column, so unreviewed submissions can always be told apart from human-approved
ones — including after a failure overwrites `notes`.

Platforms: **LinkedIn** (Easy Apply), **Naukri**, **Indeed**. Wellfound is
configured but unused (tech boards, no market fit).

Scheduled LinkedIn discovery uses a focused preset: keyword `Hiring`, posted
within the last 24 hours, and Easy Apply only. The normal manual discovery
command remains available. Focused results are tagged in SQLite and listed on
the Excel workbook's `Today's Hiring` sheet after profile matching. The tag is
discovery metadata only; it never bypasses human approval.

### Data model

One SQLite file, `data/queue.db` (gitignored):

| Table | Holds | Note |
|---|---|---|
| `jobs` | one row per discovered listing | `UNIQUE (platform, external_id)` |
| `applications` | one row per job, with state | FK to `jobs`, `UNIQUE(job_id)` |
| `submissions_log` | one row per successful submit | drives the daily caps |
| `answer_log` | every LLM-drafted answer | audit trail, shown in dashboard |

States: `pending_review → approved → applied`, plus `skipped`, `rejected`,
`failed`.

Columns added later, all with a "not captured" sentinel so older rows degrade
rather than rank badly: `jobs.posted_age_days` and `jobs.applicants` (`-1`
= unknown), and `applications.auto_approved` (empty = human-approved).

> **Put durable metadata in its own column, never in `notes`.** The auto-apply
> marker lived in `notes` for one afternoon. `set_state` *overwrites* `notes`,
> so a failure erased the marker — losing attribution for exactly the rows worth
> attributing, and under-counting the `daily_max` ceiling so extra unreviewed
> applications could slip through.

### Scoring

`core/matching.py` ranks jobs **0–100**: title fit 0–40 (graded by phrase
specificity, match position, seniority markers), freshness 0–25, easy-apply 15,
location 10, pay 6, domain 8, crowding ±4; a reject phrase or pay below the
floor hard-fails at −50. Thresholds: `job_spec.min_score` to reach review,
`auto_apply.min_score` (+ `min_score_by_platform`) to skip it.

It replaced a scorer that gave a flat +4 for any title substring hit, under
which **88% of the queue scored exactly 6 or 7** — a score-9 job failed to apply
on the same run two score-6 jobs succeeded. The root cause was data starvation,
not weights: 76% of rows have no description at all, so the industry bonus
almost never fired. If ranking looks wrong, check what the adapters actually
capture before touching the weights.

> **The dedupe trap.** Discovery inserts with `INSERT OR IGNORE` against
> `UNIQUE(platform, external_id)`. That uniqueness is the *only* thing stopping
> a re-application to a job already applied to. If you ever purge the queue,
> **keep every `applied` row and the `jobs` row it points at** — delete the job
> row and the next discovery re-adds it as brand new and it becomes appliable
> again. The queue was cleared on 2026-08-19 following exactly this rule.

### Layout

```
app.py              CLI entry point — all commands live here
safety.py           Limiter: daily caps + randomized human pacing
core/               queue.py, profile.py (config/profile loading), matching.py, sheets.py
adapters/           linkedin.py, naukri.py, indeed.py — one per platform
llm/                client.py (OpenAI-compatible), answers.py (answer resolution order)
dashboard/          FastAPI review UI; decision routes are POST-only by design
hermes/             deployable Hermes assets — see §2
data/debug/         screenshots + HTML captured on every failed apply and doctor
```

---

## 2. How Hermes operates

[Hermes](https://github.com/NousResearch/hermes-agent) is a local agent runtime.
Here it is used **only as a scheduler** — the interesting part is what it does
*not* do.

### no_agent script mode

Both cron jobs run with `no_agent: true`: Hermes runs a script on a schedule and
delivers its **stdout verbatim** to Telegram. No LLM is in the loop.

This was a deliberate retreat. An LLM-driven cron was tried first and failed two
ways: the agent's own inference call to a cold local model returned a connection
error, and on another run the model chose `execute_code`, which cron mode
blocks. The task is five deterministic CLI steps; an LLM only added failure
modes.

If you ever re-enable agent mode, the job must be **pinned**
(provider/model/base_url) or a config-drift spend guard silently skips the run.

### The two jobs

| | Applier | Watchdog |
|---|---|---|
| Schedule | `0 9,18 * * *` | `0 21 * * *` |
| Script | `job-applier-routine.py` | `job-applier-monitor.py` |
| Does | `app.py routine` | health-checks the applier, auto-repairs, reports |

Job IDs are in `RUNBOOK.md`. The watchdog runs three hours after the evening
applier run so it sees a finished result. It exists because a broken cron is
otherwise silent — an outage once ran nine days unnoticed.

### Four rules that will bite you

**1. Cron scripts must be `.py`, never `.sh`.** Hermes picks the interpreter by
file extension. For `.sh` it calls `shutil.which("bash")` — and on this machine
the persisted PATH has no Git bash on it (`Git/cmd` ships `git.exe`, not
`bash.exe`), so `bash` resolves to the **WSL launcher**, which strips the
backslashes out of the Windows script path and dies:

```
Script exited with code 127
/bin/bash: C:Users<user>AppDataLocalhermes...: No such file or directory
```

That killed 17 consecutive runs. The `.py` branch uses Hermes's own interpreter
and never consults PATH.

**2. Scripts must live in the *active profile's* scripts dir**, not
`HERMES_HOME/scripts` — `<HERMES_HOME>/profiles/<profile>/scripts/`. A script
only in the root dir fails with "Script not found". Paths are sandboxed to that
directory.

**3. Delivery needs an explicit target.** Bare `deliver: telegram` fails with
"no delivery target resolved". Use `telegram:<chat_id>`.

**4. Reports must be ASCII.** Output crosses Python → Hermes → Telegram and
emoji mojibake somewhere in that chain. Both scripts coerce with
`.encode("ascii", "replace")`.

### The gateway is the single point of failure

**Cron only fires while the Hermes gateway process is running.** It is started
as `hermes_cli/main.py gateway run` with `stdin_is_tty: true` — attached to a
terminal or the desktop app, *not* a service — so it dies on logout, sleep, or a
crash. `gateway-exit-diag.log` recorded unclean exits on six days across Aug
13–19, and a death at ~08:42 on 2026-08-19 caused the 09:00 run to be missed
entirely.

The watchdog **cannot** catch this, because it needs the same gateway. Diagnose:

```bash
hermes cron list                    # next_run_at in the past = scheduler not ticking
hermes gateway status               # authoritative: reads the gateway's pid file
hermes gateway run                  # restart it
```

**Auto-start since 2026-09-07:** use Hermes's own installer, not a hand-rolled
task.

```bash
hermes gateway install --start-on-login   # the supported way
hermes gateway status                     # authoritative liveness check
```

Without admin it falls back from a Scheduled Task to a Startup-folder `.vbs`
(`%APPDATA%\...\Startup\Hermes_Gateway_<profile>.vbs`), which is windowless.
That covers login and reboot but **not a mid-session crash**, which is the
common case here.

[hermes/gateway-guard.py](hermes/gateway-guard.py) covers crashes: it starts the
gateway when `hermes gateway status` says it is down, never kills anything, and
logs only when it acts. It is **not currently registered** — register it with
`pythonw.exe` (no console) if crash recovery is wanted:

```bash
schtasks /create /tn "Hermes Gateway Guard" /sc minute /mo 15 /f   /tr "<repo>\.venv\Scripts\pythonw.exe <repo>\hermes\gateway-guard.py"
```

To stop Hermes deliberately, create `gateway-guard.disabled` in the profile dir
rather than deleting the task.

> **Never schedule `powershell.exe` for a frequent background task.** The first
> version did, and Task Scheduler allocates a console even with
> `-WindowStyle Hidden`, so a black window flashed every 5 minutes until the
> owner switched it off — after which the gateway stayed down 11 hours and two
> scheduled runs vanished silently. Use `pythonw.exe` or a `.vbs`.

> **`hermes gateway status` is the liveness check, not a process scan.** A WMI
> command-line scan cannot see a gateway that is still booting: two guard runs
> 34s apart both concluded "nothing running" and launched a second instance.
> `tasklist | grep hermes` is worse still — the gateway often runs as
> `python.exe`, so its image name contains no "hermes" at all.

---

## 3. Maintenance playbook

### Routine health check

```bash
python app.py doctor              # read-only; also flags search-keyword drift
python app.py status              # queue counts per state
python app.py auto-approve --dry-run   # what would submit unreviewed, changes nothing
hermes cron list                  # both jobs, last status, next run
hermes gateway status             # is the scheduler even alive?
```

`python` means `.venv/Scripts/python.exe` on Windows. Later additions to the CLI
include `auto-approve`, `retire-stale`, `job-board`, and
`discover linkedin --today-hiring`; `app.py --help` is authoritative.

**`auto-approve --dry-run` before any threshold change.** It prints exactly which
jobs would be submitted without review, and is the only cheap way to see the
blast radius of a `min_score` edit.

### Diagnosing a failed run

1. `hermes cron list` → `last_status` / `last_error` on the job.
2. Run history: `cron/executions.db` in the profile dir (`status`, `error`).
3. Delivered reports, one file per run: `cron/output/<job-id>/`.
4. Scheduler and delivery lines: `logs/agent.log` in the profile dir.
5. For a failed *application*: `data/debug/` has a screenshot **and** the full
   HTML from the moment of failure. Read these before theorising.

### Failure taxonomy

| Symptom | Almost always means | Action |
|---|---|---|
| `exit 127`, path missing backslashes | `.sh` script → WSL bash | use the `.py` wrapper |
| "Script not found" | wrong scripts dir | profile dir, not root |
| Nothing ran at all, `next_run_at` in the past | **gateway is down** | `hermes gateway run` |
| `llm: DEGRADED` | Ollama down, or model not installed | see §4 |
| One platform: 0 cards | transient throttling | see below |
| Mass "Easy Apply button/modal not found" | **listings have expired** | purge stale queue rows |
| Naukri "needs manual completion" | free-text recruiter chatbot | user finishes on Naukri |

### 0 cards is usually *not* selector rot

Within twenty minutes on 2026-08-18: Naukri reported 0 cards then parsed 5 fine;
LinkedIn did the reverse; a third run had all three clean. The LinkedIn debug
capture showed a valid logged-in session **redirected to the feed**
(`<title>Feed | LinkedIn</title>`) rather than the job search — no authwall, no
captcha. Back-to-back runs cause this.

Before touching an adapter: let a **cold** run be the diagnostic, read the debug
capture, and only conclude selector rot if a cold run still fails *and* the HTML
shows genuinely changed markup. Do not hammer Naukri — ~10 apply runs in a few
minutes produced rate-limited behaviour.

### Per-platform capture quirks

The same fact lives in a different place on each board — always verify against a
real **search-results** capture before assuming:

| | posting age | easy-apply |
|---|---|---|
| Naukri | in the card, `.job-post-day` | assumed true, refined at apply time |
| Indeed | **not rendered** — only in the jobcards JSON (`formattedRelativeTime`) | JSON `indeedApplyEnabled` |
| LinkedIn | **genuinely absent** from cards; most results are "Promoted" | "Easy Apply" badge text |

LinkedIn age stays `-1` and scores neutral **by design** — it exists only on the
job's own page, and fetching that per listing is the traffic most likely to get
the account banned.

Indeed's JSON orders fields alphabetically, so `jobkey` comes *after* both
values in each object: attribute a value to the first key that **follows** it.
Pairing by nearest key looks right and silently attributes flags to the next
job.

> Two false "this job is external" conclusions came from stale selectors, not
> from the jobs. `easy_apply` was hardcoded `True` for every LinkedIn result,
> and Indeed's `[data-testid='indeedApply']` / `.ialbl` match nothing on its
> React page. Before concluding a platform can't be applied to, check that the
> selector still matches something.

### Expired listings

Discovered listings go stale within weeks. A run where most applies fail with
"Easy Apply button/modal not found" means the postings closed, not that the code
broke. The fix is to purge non-applied rows and rediscover — preserving
`applied` rows per the dedupe trap in §1.

### Deploying a changed wrapper

`hermes/` is the source of truth; the scripts dir holds deployed copies.

```bash
cp hermes/job-applier-routine.py hermes/job-applier-monitor.py \
   "$LOCALAPPDATA/hermes/profiles/<profile>/scripts/"
```

Test a job end-to-end **including delivery**:

```bash
hermes cron run <job-id>      # goes through the gateway, so delivery runs too
```

Calling `cron.scheduler._run_job_script` directly tests only the script.

### Testing

`tests/test_matching.py` runs without pytest (it isn't installed): import the
module and call each `test_*`. The tests assert **ordering**, not exact scores,
so weights can be retuned without rewriting them.

The watchdog edits `jobs.json` and, once, `config.yaml`. Test that against a
**sandboxed copy** — point the module's `JOBS_JSON` / `CONFIG` at a temp dir.
Never test repair logic against production state.

> **A saved debug capture cannot validate apply-side selectors.**
> `page.content()` stores only the main frame and the file does not re-render:
> loading one via `set_content` reported 0 matches and no button text, which
> looked exactly like a failed fix. Verify with a live read-only probe instead —
> open the job page, dismiss consent, count matches in the main frame *and*
> every iframe, and never click apply. Discovery-side selectors are different:
> those *can* be checked against a saved search-results page.

> Copying `data/queue.db` alone can miss recent committed rows — WAL mode keeps
> them in `queue.db-wal` until checkpoint. Copy all three files, or checkpoint
> first, or the "backup" quietly lacks the newest data.

`tests/test_linkedin_send_message.py` runs the same way (no pytest, `if
__name__` runner) but *is* safe to check with `set_content` against static
fixtures under `tests/fixtures/`, unlike the apply-page caution above: the
profile Message button and chat dock are plain top-level DOM, not inside an
iframe. The fixtures are hand-built to match a real live probe's findings
(verified once, read-only, no click/send), not raw dumps of a real contact's
page — so no PII sits in the repo. This is what caught the `:has-text`
substring bug where an unscoped `button:has-text('Message')` matched
LinkedIn's global "Compose message" control on a profile with no real
Message button.

---

## 4. Credentials

**There are none to hand you, and nothing to configure.** The codebase contains
no API keys, reads no environment variables, and there is no `.env`. Verified:
no `getenv`, no `api_key`, no `ANTHROPIC_*` anywhere in `app.py`, `core/`,
`llm/`, `adapters/`, `dashboard/`, or `safety.py`.

Here is every credential-shaped thing in the system and where it actually lives:

| What | Where | Can an agent use it? |
|---|---|---|
| LinkedIn / Naukri / Indeed logins | nowhere — **the tool never sees them** | **No.** See below |
| Browser sessions | `profiles/` (gitignored) | Never touch |
| Local LLM (Ollama) | `http://localhost:11434` — no auth | Yes, it is local and keyless |
| Google Sheets webhook + shared secret | `config.yaml` `sheets:` block (gitignored) | Read if needed; never copy elsewhere |
| Telegram bot token | Hermes profile config, **not this repo** | Not needed here |
| Telegram chat ID | `RUNBOOK.md` (gitignored) | Read only |

### Platform logins: the model

The user signs in **by hand, once**, and Playwright keeps a persistent browser
profile:

```bash
python app.py login <platform>
```

The tool never asks for, stores, or transmits a password. So:

- "Not logged in" / expired session → **tell the user to run `login`**. Do not
  attempt to sign in, and never enter credentials into a browser.
- CAPTCHA or verification wall → stop and hand it to the user. Never solve or
  bypass one.
- "profile is already in use" → a stray browser window holds the profile; ask
  the user to close it, retry once.
- Indeed must be logged in on **in.indeed.com**; a US-domain session does not
  carry over.

### Google Sheets

Auth is a shared secret in a POST body to a Google Apps Script web app — no
Google Cloud project, no service account, no OAuth (a no_agent script cannot use
an MCP connector). Setup: [hermes/sheets-setup.md](hermes/sheets-setup.md).

The web app's access must be **"Anyone"**; a 401 or login redirect means it is
still "Only myself". To rotate, redeploy the Apps Script with a new secret and
update `config.yaml`. `python app.py sync-sheet` backfills; a `synced_at` column
prevents duplicates.

### Handling rules

- Never copy a secret out of `config.yaml` into any other file, commit message,
  log, or report.
- Never create `config.yaml.bak*` inside the repo. `config.yaml` is gitignored
  but a backup alongside it was **not**, and it carries the Sheets secret — the
  pattern is now ignored, but put backups outside the tree anyway.
- If you need to show config, redact the `sheets:` block.

---

## 5. Ground rules

Beyond the hard rules in [AGENTS.md](AGENTS.md):

- **Never approve or reject queue items yourself** — not in the dashboard, not
  in SQLite. The *tool* auto-approves strong matches because the owner asked it
  to (§1); that is a configured behaviour with rails, not a licence for you to
  promote rows by hand. The dashboard's decision routes are POST-only precisely
  so nothing can trigger them by accident.
- **Do not widen the auto-apply blast radius casually.** `min_score`,
  `daily_max` and `require_easy_apply` are the only things standing between a
  scraper and unreviewed applications to real employers. Changing them is the
  owner's call, and a scoring change silently re-scales every threshold — after
  the 0–100 rewrite the old `min_score: 6` would have meant "submit everything".
- **Never edit `profile.yaml` or `config.yaml`** unasked. When an apply fails on
  an unanswered question, surface the question; the user adds the answer.
  Keep `profile.yaml` answer-map keys **specific** — a broad
  `years of experience` key once wrongly answered a question about GCC
  experience.
- **Never bypass caps, CAPTCHAs, or login walls.** Caps are hard stops in
  `safety.py`. LinkedIn and Indeed ToS prohibit automation and the ban risk is
  real — the caps, the pacing, and the human gate all exist for that reason.
- **Never auto-answer free-text recruiter questions.** Radio and choice
  questions are answered from the profile map; free text is flagged for the
  user. Inventing an answer on a real application is not recoverable.
- **One browser at a time.** Discovery and apply take minutes — run them
  sequentially with generous timeouts, never in parallel.
- **Do not push.** The user's standing instruction is that this work stays in
  the local repo; commit freely, but never push to `origin` unless asked. The
  `local-history` branch must never be pushed anywhere — its commit messages
  contain personal data.
- **Verify before claiming.** This system has a history of confident wrong
  diagnoses (selector rot that was throttling; a "fixed" cron that had been
  failing for nine days). Read the debug capture, check `last_status`, run the
  cold test.
