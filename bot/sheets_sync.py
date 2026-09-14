import aiohttp
import logging
from typing import Optional, Tuple, List, Dict, Any
from bot.database import save_memory, get_bot_setting, set_bot_setting
from bot.config import GOOGLE_SHEET_URL

logger = logging.getLogger(__name__)

# Шаблон скрипта Google Apps Script для пользователя
APPS_SCRIPT_CODE = """// ==========================================
// GOOGLE APPS SCRIPT ДЛЯ ПАМЯТИ TELEGRAM БОТИКА
// Вставьте этот код в: Расширения -> Apps Script
// Затем: Развернуть -> Новое развертывание -> Веб-приложение (Доступ: Все)
// ==========================================

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var action = data.action || "append";
    
    // 1. Запись строки в нужный лист (например: "Нарушения", "Идеи", "Память")
    if (action === "append") {
      var sheetName = data.sheet || "Логи";
      var sheet = ss.getSheetByName(sheetName);
      if (!sheet) {
        sheet = ss.insertSheet(sheetName);
        if (data.headers && data.headers.length) {
          sheet.appendRow(data.headers);
        }
      }
      if (data.row && data.row.length) {
        sheet.appendRow(data.row);
      }
      return ContentService.createTextOutput(JSON.stringify({success: true, message: "Row added"}))
        .setMimeType(ContentService.MimeType.JSON);
    }
    
    return ContentService.createTextOutput(JSON.stringify({success: false, message: "Unknown action"}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService.createTextOutput(JSON.stringify({success: false, error: err.toString()}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName("Память");
    if (!sheet) {
      sheet = ss.insertSheet("Память");
      sheet.appendRow(["Категория", "Ключ / Тема", "Значение / Описание"]);
      sheet.appendRow(["Правило", "Мат", "Запрещён в любом виде"]);
      sheet.appendRow(["Инфо", "Создатель", "@ArTeM_aoao"]);
    }
    
    var data = sheet.getDataRange().getValues();
    var rows = [];
    for (var i = 1; i < data.length; i++) {
      if (data[i][0] || data[i][1] || data[i][2]) {
        rows.push({
          category: String(data[i][0] || "fact"),
          key: String(data[i][1] || ""),
          content: String(data[i][2] || "")
        });
      }
    }
    
    return ContentService.createTextOutput(JSON.stringify({success: true, rows: rows}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService.createTextOutput(JSON.stringify({success: false, error: err.toString()}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
"""


async def get_active_sheet_url() -> str:
    """Возвращает актуальный URL Google Apps Script Webhook (из БД или config)."""
    db_url = await get_bot_setting("google_sheet_url", "")
    return db_url.strip() or GOOGLE_SHEET_URL.strip()


async def test_sheet_connection(webhook_url: str) -> Tuple[bool, str]:
    """Проверяет подключение к Google Apps Script Webhook."""
    cleaned_url = webhook_url.strip()
    if not cleaned_url:
        return False, "URL вебхука не указан."
    if not cleaned_url.startswith("https://script.google.com/"):
        return False, "URL должен начинаться с https://script.google.com/macros/s/.../exec"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(cleaned_url) as resp:
                if resp.status != 200:
                    return False, f"Google Apps Script вернул HTTP {resp.status}"
                data = await resp.json(content_type=None)
                if data.get("success"):
                    rows = data.get("rows", [])
                    return True, f"Соединение успешно! Считано {len(rows)} записей из листа «Память»."
                return False, f"Ошибка ответа таблицы: {data.get('error', 'Неизвестная ошибка')}"
    except Exception as e:
        return False, f"Не удалось подключиться к Google Apps Script: {e}"


async def sync_knowledge_from_sheet(webhook_url: Optional[str] = None) -> Tuple[bool, int, str]:
    """Считывает базу знаний из Google Таблицы и сохраняет в память SQLite."""
    url = webhook_url or await get_active_sheet_url()
    if not url:
        return False, 0, "URL Google Таблицы не настроен. Используйте /set_sheet <url>"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, 0, f"HTTP ошибка от Google: {resp.status}"
                data = await resp.json(content_type=None)
                if not data.get("success"):
                    return False, 0, f"Ошибка таблицы: {data.get('error', 'Ошибка')}"

                rows = data.get("rows", [])
                imported_count = 0
                for r in rows:
                    cat = r.get("category", "fact").strip()
                    key = r.get("key", "").strip()
                    content = r.get("content", "").strip()
                    if key and content:
                        await save_memory(
                            key_phrase=key,
                            content=content,
                            category=cat,
                            user_id=0,
                            chat_id=0
                        )
                        imported_count += 1

                return True, imported_count, f"Успешно синхронизировано {imported_count} записей знаний!"
    except Exception as e:
        logger.error("sync_knowledge_from_sheet error: %s", e)
        return False, 0, f"Ошибка синхронизации: {e}"


async def append_row_to_sheet(sheet_name: str, row_data: List[Any], headers: Optional[List[str]] = None) -> bool:
    """Отправляет строку в Google Таблицу в указанный лист (фоновая запись)."""
    url = await get_active_sheet_url()
    if not url:
        return False

    payload = {
        "action": "append",
        "sheet": sheet_name,
        "row": [str(x) for x in row_data],
        "headers": headers or []
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.post(url, json=payload) as resp:
                return resp.status == 200
    except Exception as e:
        logger.warning("append_row_to_sheet to %s failed: %s", sheet_name, e)
        return False
