# Google Sheet auto-append — one-time setup

The daily routine can append each newly-applied job to a Google Sheet with no
Google Cloud project, no service account, and no OAuth token — just a small
Apps Script bound to your sheet, deployed as a web app. The routine POSTs new
applications to that URL; the script appends them.

## 1. Create the sheet + script

1. Create a new sheet at [sheets.new](https://sheets.new). Name the first tab
   `Applications` (or leave it `Sheet1` — the script handles either).
2. **Extensions → Apps Script**. Delete any boilerplate and paste
   [`sheets-webhook.gs`](sheets-webhook.gs) (in this folder).
3. In that script, set `SECRET` to any random string you choose (e.g. a UUID).
4. **Deploy → New deployment → type: Web app.**
   - *Execute as:* **Me**
   - *Who has access:* **Anyone**  ← required so the routine can POST; the
     `SECRET` is what actually gates writes, and the URL is unguessable.
5. Copy the **Web app URL** (ends in `/exec`).

## 2. Point the tool at it

In `config.yaml` (gitignored — your secret stays local):

```yaml
sheets:
  enabled: true
  webhook_url: "https://script.google.com/macros/s/AKfy…/exec"
  secret: "the-same-SECRET-you-set-in-the-script"
```

## 3. Backfill + verify

```bash
python app.py sync-sheet
```

This appends every applied job not yet in the sheet (your existing 10 on first
run) and marks them synced, so later runs only add new ones. The daily
`routine` then appends automatically after each apply — you'll see a
`sheet sync: appended N row(s)` line in the Telegram report.

## Notes

- **No duplicates:** each application is marked `synced_at` once appended;
  re-running never re-adds it.
- **Header row** is written automatically if the sheet is empty.
- **Security:** writes require the shared `secret`; a POST without it is
  ignored. The URL alone can't be used to write.
- Disable anytime with `sheets.enabled: false` — the routine simply skips it.
