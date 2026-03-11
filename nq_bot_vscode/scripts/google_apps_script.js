/**
 * Google Apps Script — NQ.BOT Subscriber Collection
 * ==================================================
 * Deploy this as a Web App in Google Apps Script to receive
 * phone number signups from the NQ.BOT website.
 *
 * SETUP:
 *   1. Go to https://script.google.com → New Project
 *   2. Paste this entire file
 *   3. Click Deploy → New Deployment → Web App
 *      - Execute as: Me
 *      - Who has access: Anyone
 *   4. Copy the Web App URL
 *   5. Add to your .env:  NOTIFY_APPS_SCRIPT_URL=<your_url>
 *   6. The script auto-creates a "Subscribers" sheet on first run
 */

function doPost(e) {
  try {
    // Handle both JSON body and form-encoded body
    var data = {};
    if (e.postData && e.postData.type === "application/json") {
      data = JSON.parse(e.postData.contents);
    } else if (e.parameter) {
      data = e.parameter;
    }
    var phone = (data.phone || "").toString().trim();

    if (!phone || phone.replace(/[\s\-\(\)\+]/g, "").length < 7) {
      return ContentService
        .createTextOutput(JSON.stringify({ result: "error", error: "Invalid phone number" }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // Get or create the Subscribers sheet
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName("Subscribers");
    if (!sheet) {
      sheet = ss.insertSheet("Subscribers");
      sheet.appendRow(["Phone", "Signed Up", "Active", "Source"]);
      sheet.getRange("A1:D1").setFontWeight("bold");
    }

    // Check for duplicates
    var phones = sheet.getRange("A:A").getValues().flat();
    if (phones.includes(phone)) {
      return ContentService
        .createTextOutput(JSON.stringify({ result: "success", message: "Already subscribed" }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // Add new subscriber
    sheet.appendRow([
      phone,
      new Date().toISOString(),
      "YES",
      data.source || "website"
    ]);

    return ContentService
      .createTextOutput(JSON.stringify({ result: "success", message: "Subscribed" }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ result: "error", error: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

// Handle CORS preflight
function doGet(e) {
  // Return all active subscribers as JSON (for the bot to read)
  var action = (e.parameter.action || "").toString();

  if (action === "list") {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName("Subscribers");
    if (!sheet) {
      return ContentService
        .createTextOutput(JSON.stringify({ result: "success", subscribers: [] }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    var data = sheet.getDataRange().getValues();
    var subscribers = [];
    for (var i = 1; i < data.length; i++) {
      if (data[i][2] === "YES") {
        subscribers.push({
          phone: data[i][0],
          signed_up: data[i][1],
        });
      }
    }

    return ContentService
      .createTextOutput(JSON.stringify({ result: "success", subscribers: subscribers }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  return ContentService
    .createTextOutput(JSON.stringify({ result: "ok", message: "NQ.BOT Subscriber API" }))
    .setMimeType(ContentService.MimeType.JSON);
}
