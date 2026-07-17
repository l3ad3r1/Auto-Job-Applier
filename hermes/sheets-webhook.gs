/**
 * Auto Job Applier — Google Sheet append webhook.
 * Paste into Extensions > Apps Script of your target sheet, set SECRET,
 * then Deploy > New deployment > Web app (Execute as: Me, Access: Anyone).
 * See hermes/sheets-setup.md.
 */
const SECRET = "CHANGE-ME-to-a-random-string";
const TAB = "Applications"; // falls back to the first sheet if absent

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (SECRET && body.secret !== SECRET) {
      return json_({ ok: false, error: "bad secret" });
    }
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ss.getSheetByName(TAB) || ss.getSheets()[0];

    // Write the header row once, on an empty sheet.
    if (sheet.getLastRow() === 0 && body.columns) {
      sheet.appendRow(body.columns);
    }
    (body.rows || []).forEach(function (r) { sheet.appendRow(r); });

    return json_({ ok: true, added: (body.rows || []).length });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
