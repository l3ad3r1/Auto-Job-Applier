# Progress

- Completed (2026-10-04): diagnosed the application drop and fixed two leaks. Naukri chatbot jobs were misfiled as "no confirmation" because detection resolved to an invisible wrapper (`CHATBOT_DRAWER` now targets the drawer). The title gate now accepts reordered titles ("Manager - Business Development"); 23 distinct historical titles recovered, no false positives.
- Findings: Hermes ran nothing 2026-09-23 to 2026-09-30 (gateway silent; the watchdog shares the machine so could not report it). Indeed's session expires repeatedly. About half of attempts fail (Naukri chatbot, Indeed external apply, LinkedIn Easy Apply missing). Caps and `auto_apply.daily_max` are not binding.
- Next: user re-logs into Indeed (`.venv\Scripts\python.exe app.py login indeed`); consider Foundit/Shine adapters; decide whether to requeue the 12 Naukri rows failed by the misdetection.

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
