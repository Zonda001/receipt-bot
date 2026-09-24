/**
 * Проксі для завантаження фото чеків у Google Drive від імені власника папки.
 *
 * Навіщо: service account на звичайному Gmail не має квоти Drive
 * ("Service Accounts do not have storage quota"), тому фото кладе цей скрипт,
 * розгорнутий як web app з "Execute as: Me" — файли йдуть у квоту власника.
 *
 * Script Properties (Project Settings -> Script Properties):
 *   UPLOAD_SECRET — спільний секрет із ботом (DRIVE_UPLOAD_SECRET у .env)
 *   FOLDER_ID     — ID папки "Чеки"
 *
 * Запит (POST, JSON):
 *   {"secret": "...", "action": "upload", "filename": "...", "mimeType": "image/jpeg", "data": "<base64>"}
 *   {"secret": "...", "action": "delete", "id": "<fileId>"}   // компенсація, якщо Sheets впав
 * Відповідь: {"ok": true, "id": "...", "url": "..."} або {"ok": false, "error": "..."}
 */
function doPost(e) {
  try {
    var props = PropertiesService.getScriptProperties();
    var req = JSON.parse(e.postData.contents);
    if (!req.secret || req.secret !== props.getProperty('UPLOAD_SECRET')) {
      return reply({ ok: false, error: 'unauthorized' });
    }
    var folder = DriveApp.getFolderById(props.getProperty('FOLDER_ID'));

    if (req.action === 'upload') {
      var blob = Utilities.newBlob(Utilities.base64Decode(req.data), req.mimeType, req.filename);
      var file = folder.createFile(blob);
      return reply({ ok: true, id: file.getId(), url: file.getUrl() });
    }

    if (req.action === 'delete') {
      var target = DriveApp.getFileById(req.id);
      // Видаляти дозволено тільки файли з нашої папки, а не будь-що на Drive власника.
      if (!target.getParents().hasNext() || target.getParents().next().getId() !== folder.getId()) {
        return reply({ ok: false, error: 'not in folder' });
      }
      target.setTrashed(true);
      return reply({ ok: true, id: req.id });
    }

    return reply({ ok: false, error: 'unknown action' });
  } catch (err) {
    return reply({ ok: false, error: String(err) });
  }
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
