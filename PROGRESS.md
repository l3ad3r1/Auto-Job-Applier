# Progress

- Completed (2026-10-05, later): Foundit apply verified live and implemented (Quick Apply, instant, ends on /srp/applied-success). Applied to S3connections Sales/BD Manager (Hyderabad) as the observation. A first attempt wrongly clicked a sidebar card ("Pre-Sales Consultant", UNITY Infotech, Kochi): external-apply job, nothing submitted, but Foundit now lists it as applied in the user's history. Guards and tests added.
- Blocked: Shine. Session-only cookie is lost on browser close; `persist_session_cookies` added, but the user must redo `login shine` (window stays open) before a Shine apply can be observed.
- Completed (2026-10-05): re-queued the 12 Naukri rows from the last week that the chatbot bug had misfiled. Result: 1 applied, 11 flagged "needs manual completion" (10 are the same shape, a free-text "How many years of experience do you have in X?", plus one Naukri "Something went wrong" error after the relocation question was answered). The 43 older rows (back to Aug 19) were left alone as almost certainly closed.
- Completed (2026-10-05): Foundit and Shine adapters, discovery only and verified live; `apply()` is inert on both until one real apply is watched in a logged-in session. `parse_posted_age` now reads "Posted a day ago" / "Posted today".
- Next: user runs `login foundit`, `login shine`, and re-logs into Indeed; then watch one apply per new board. Decide whether to add numeric experience answers to `profile.yaml` (see the 10 blocked Naukri questions).
- Completed (2026-10-04): diagnosed the application drop and fixed two leaks. Naukri chatbot jobs were misfiled as "no confirmation" because detection resolved to an invisible wrapper (`CHATBOT_DRAWER` now targets the drawer). The title gate now accepts reordered titles ("Manager - Business Development"); 23 distinct historical titles recovered, no false positives.
- Findings: Hermes ran nothing 2026-09-23 to 2026-09-30 (gateway silent; the watchdog shares the machine so could not report it). Indeed's session expires repeatedly. About half of attempts fail (Naukri chatbot, Indeed external apply, LinkedIn Easy Apply missing). Caps and `auto_apply.daily_max` are not binding.

## Latest verification

- Manual Hermes test run `2fe31cd7b1634a04b281ba995d27742d` completed on 2026-09-11 at 12:52 IST and delivered to Telegram.
- Discovered 20 new LinkedIn Today/Hiring jobs and auto-approved 3; no application received a trustworthy success confirmation.
- Indeed was skipped because its session expired; local LLM calls failed, so unanswered questions were flagged safely.
- Excel board rebuilt with 1,025 jobs; queue is 57 pending review, 3 approved, 103 applied all-time.

- Recreated Hermes jobs: `e6c2c53080d7` (09:00/18:00) and `43d822b140a3` (21:00), using `E:\claude-projects\infra\job-applier`.
- Telegram token and private destination verified with a successful test message; both jobs now deliver normal and failure reports there.
- Gateway is running and its Windows Scheduled Task is registered.

- Manual Hermes run `913355fbd3354a41bcd6dc1901cf57af` completed on 2026-09-09 at 20:22 IST.
- Confirmed submissions: PayFX Global and EBG Group.
- Excel board rebuilt with 997 jobs; Google Sheets sync appended 2 rows.
- Queue after the run: 54 pending review, 3 approved, 103 applied all-time.
