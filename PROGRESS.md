# Progress

- Completed: Rewired the fresh Hermes 0.21 default profile to Job Applier, restored Telegram delivery, redeployed its scripts/skill, recreated the 09:00/18:00 and 21:00 jobs, and started the gateway.
- In progress: None.
- Next: Confirm the first scheduled 18:00 application report and 21:00 watchdog report arrive normally.

## Latest verification

- Recreated Hermes jobs: `e6c2c53080d7` (09:00/18:00) and `43d822b140a3` (21:00), using `E:\claude-projects\infra\job-applier`.
- Telegram token and private destination verified with a successful test message; both jobs now deliver normal and failure reports there.
- Gateway is running and its Windows Scheduled Task is registered.

- Manual Hermes run `913355fbd3354a41bcd6dc1901cf57af` completed on 2026-09-09 at 20:22 IST.
- Confirmed submissions: PayFX Global and EBG Group.
- Excel board rebuilt with 997 jobs; Google Sheets sync appended 2 rows.
- Queue after the run: 54 pending review, 3 approved, 103 applied all-time.
