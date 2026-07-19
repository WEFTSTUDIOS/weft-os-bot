"""Flash reminders - nagging reminders that live in the WEFT Tasks Notion DB.

A flash is a normal WEFT Tasks row with Type = "Flash". The bot re-sends the
reminder text every RESEND_INTERVAL until the row is marked Done (via /done,
same as any task). All state - text, created time, resend count, last sent -
lives in Notion, so the cycle survives restarts and redeploys.

Uses the same Composio-routed Notion access as notion_expenses.py.
"""
import datetime
import time

from notion_expenses import _notion_request, NOTION_TASKS_DB_ID

RESEND_INTERVAL = 5 * 60 * 60   # 5 hours between re-sends
CHECK_INTERVAL = 300            # how often to poll Notion state (seconds)

TYPE_PROP = "Type"
COUNT_PROP = "Resend Count"
LAST_SENT_PROP = "Last Sent"

_schema_ready = False

def ensure_flash_schema():
    """Add the flash properties to WEFT Tasks if they're missing (idempotent)."""
    global _schema_ready
    if _schema_ready:
        return True
    res = _notion_request(
        "PATCH",
        f"https://api.notion.com/v1/databases/{NOTION_TASKS_DB_ID}",
        body={"properties": {
            TYPE_PROP: {"select": {"options": [{"name": "Flash", "color": "yellow"}]}},
            COUNT_PROP: {"number": {"format": "number"}},
            LAST_SENT_PROP: {"date": {}},
        }},
    )
    _schema_ready = res is not None
    if not _schema_ready:
        print("[FLASH] Could not ensure flash properties on WEFT Tasks.")
    return _schema_ready

def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def create_flash(text):
    """Create a flash reminder row. Last Sent starts now, so the first re-send
    lands 5 hours after creation. Returns the Notion page dict or None."""
    ensure_flash_schema()
    payload = {
        "parent": {"database_id": NOTION_TASKS_DB_ID},
        "properties": {
            "Task": {"title": [{"text": {"content": text}}]},
            "Status": {"select": {"name": "To Do"}},
            "Date Added": {"date": {"start": _now_iso()}},
            TYPE_PROP: {"select": {"name": "Flash"}},
            COUNT_PROP: {"number": 0},
            LAST_SENT_PROP: {"date": {"start": _now_iso()}},
        },
    }
    return _notion_request("POST", "https://api.notion.com/v1/pages", body=payload)

def get_active_flashes():
    """All flash rows still marked To Do, oldest first."""
    payload = {
        "filter": {"and": [
            {"property": "Status", "select": {"equals": "To Do"}},
            {"property": TYPE_PROP, "select": {"equals": "Flash"}},
        ]},
        "sorts": [{"property": "Date Added", "direction": "ascending"}],
    }
    data = _notion_request(
        "POST",
        f"https://api.notion.com/v1/databases/{NOTION_TASKS_DB_ID}/query",
        body=payload,
    )
    flashes = []
    if not data:
        return flashes
    for item in data.get("results", []):
        props = item.get("properties", {})
        title = props.get("Task", {}).get("title", [])
        text = title[0].get("plain_text", "") if title else ""
        count = props.get(COUNT_PROP, {}).get("number") or 0
        last = (props.get(LAST_SENT_PROP, {}).get("date") or {}).get("start")
        if text:
            flashes.append({"id": item["id"], "name": text, "resend_count": count, "last_sent": last})
    return flashes

def _mark_sent(page_id, new_count):
    _notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", body={
        "properties": {
            COUNT_PROP: {"number": new_count},
            LAST_SENT_PROP: {"date": {"start": _now_iso()}},
        }
    })

def _due(last_sent_iso):
    if not last_sent_iso:
        return True
    try:
        ts = datetime.datetime.fromisoformat(last_sent_iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - ts).total_seconds() >= RESEND_INTERVAL

def run_flash_loop(send_fn, get_chat_id):
    """Daemon loop: re-send every active flash whose last send is 5+ hours old.
    State is read from Notion each pass, so restarts never reset the cycle."""
    while True:
        try:
            ensure_flash_schema()
            chat_id = get_chat_id()
            if chat_id:
                for f in get_active_flashes():
                    if _due(f["last_sent"]):
                        n = f["resend_count"] + 1
                        send_fn(chat_id, f"FLASH: {f['name']}\n\n(reminder #{n} - /done to clear it)")
                        _mark_sent(f["id"], n)
        except Exception as e:
            print(f"[FLASH] loop error: {e}")
        time.sleep(CHECK_INTERVAL)
