import requests
import json
import datetime
import pytz
import base64
import os
import re
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- NOTION ACCESS (via Composio) ---
# All Notion calls go through the Composio proxy — the same pattern main.py already
# uses for Sheets, Drive, and Gmail — using the Notion connected account in the
# byweftstudios workspace.
#
# Railway env vars:
#   COMPOSIO_API_KEY     - already set for Sheets/Drive/Gmail
#   NOTION_CONNECTION_ID - the ca_... id from Composio > Connected Accounts > Notion
#   NOTION_TOKEN         - legacy direct token; only used as a fallback if
#                          NOTION_CONNECTION_ID is not set, so the bot keeps
#                          working during the switchover.

COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "").strip()
NOTION_CONNECTION_ID = os.environ.get("NOTION_CONNECTION_ID", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()

COMPOSIO_PROXY = "https://backend.composio.dev/api/v3.1/tools/execute/proxy"
NOTION_VERSION = "2022-06-28"

NOTION_DB_ID = "39575f5d-f642-810e-854e-c80528128539"        # WEFT Expenses
NOTION_TASKS_DB_ID = "39575f5d-f642-81f4-bcc9-e40542ce4721"  # WEFT Tasks
NOTION_FOOD_DB_ID = "39575f5d-f642-81d1-9eb5-e3282247795f"   # WEFT Food

CATEGORIES = [
    "Fabric", "Hardware", "Tools/Equipment", "Software",
    "Travel", "Marketing", "Packaging/Shipping", "Food", "Personal", "Other"
]

def _notion_request(method, url, body=None, timeout=15):
    """Send a Notion API request via the Composio connection (preferred) or a
    direct token (legacy fallback). Returns the parsed Notion JSON dict, or None."""
    try:
        if COMPOSIO_API_KEY and NOTION_CONNECTION_ID:
            payload = {
                "connected_account_id": NOTION_CONNECTION_ID,
                "endpoint": url,
                "method": method,
                "headers": {"Notion-Version": NOTION_VERSION},
            }
            if body is not None:
                payload["body"] = body
            resp = requests.post(
                COMPOSIO_PROXY,
                headers={"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            data = resp.json()
            out = data.get("data") if isinstance(data, dict) else None
            if not out:
                print(f"Notion via Composio error ({method} {url}): {data}")
                return None
            return out
        if NOTION_TOKEN:
            headers = {
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            }
            resp = requests.request(method, url, headers=headers, json=body, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        print("Notion not configured: set NOTION_CONNECTION_ID (preferred) or NOTION_TOKEN.")
        return None
    except Exception as e:
        print(f"Notion request error ({method} {url}): {e}")
        return None

def write_expense_to_notion(date_str, vendor, amount, category, biz_or_personal, note, source, image_url=None):
    """Write a new expense record to the WEFT Expenses Notion database."""
    properties = {
        "Date": {"date": {"start": date_str}},
        "Vendor": {"rich_text": [{"text": {"content": vendor}}]},
        "Amount": {"number": float(amount)},
        "Category": {"select": {"name": category}},
        "Business or Personal": {"select": {"name": biz_or_personal}},
        "Source": {"select": {"name": source}}
    }
    if note:
        properties["Note"] = {"rich_text": [{"text": {"content": note}}]}
    if image_url:
        properties["Receipt Image"] = {"files": [{"type": "external", "name": "receipt.jpg", "external": {"url": image_url}}]}
    payload = {"parent": {"database_id": NOTION_DB_ID}, "properties": properties}
    return _notion_request("POST", "https://api.notion.com/v1/pages", body=payload)

def build_category_keyboard(prefix, current_page=0):
    """Build an inline keyboard for selecting a category."""
    markup = InlineKeyboardMarkup()
    row = []
    for i, cat in enumerate(CATEGORIES):
        row.append(InlineKeyboardButton(cat, callback_data=f"{prefix}_cat_{cat}"))
        if len(row) == 2:
            markup.add(*row)
            row = []
    if row:
        markup.add(*row)
    return markup

def build_biz_personal_keyboard(prefix):
    """Build an inline keyboard for selecting Business or Personal."""
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Business", callback_data=f"{prefix}_type_Business"),
        InlineKeyboardButton("Personal", callback_data=f"{prefix}_type_Personal")
    )
    return markup

def build_confirmation_keyboard(prefix):
    """Build an inline keyboard for confirming receipt data."""
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Confirm & Save", callback_data=f"{prefix}_confirm_yes"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"{prefix}_confirm_no")
    )
    markup.add(
        InlineKeyboardButton("Edit Category", callback_data=f"{prefix}_edit_cat"),
        InlineKeyboardButton("Edit Biz/Personal", callback_data=f"{prefix}_edit_type")
    )
    return markup

def extract_receipt_data_with_claude(client, img_b64):
    """Use Claude vision to extract receipt details."""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a receipt or invoice photo. Extract and return ONLY a JSON object with these exact fields:\n"
                            "- vendor: vendor or store name (string)\n"
                            "- amount: total amount as a number (no $ sign, just the number)\n"
                            "- date: date of the transaction in YYYY-MM-DD format (string, use today if missing)\n"
                            f"- category: best guess from this exact list: {', '.join(CATEGORIES)}\n"
                            "Return only valid JSON, no explanation, no markdown blocks."
                        )
                    }
                ]
            }]
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```json"):
            raw = raw[7:]
        if raw.endswith("```"):
            raw = raw[:-3]
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            return None
        return json.loads(json_match.group())
    except Exception as e:
        print(f"Claude extraction error: {e}")
        return None

# State management for multi-step flows; keys are chat_id
expense_states = {}

def get_state(chat_id):
    if chat_id not in expense_states:
        expense_states[chat_id] = {}
    return expense_states[chat_id]

def clear_state(chat_id):
    if chat_id in expense_states:
        del expense_states[chat_id]

def upload_file_to_notion_or_imgur(img_data, upload_to_drive=None):
    """Notion's public API needs an external URL for 'files' properties, so receipts
    are uploaded to Google Drive (private, already connected via Composio).
    `upload_to_drive` is injected from main.py to avoid a circular import."""
    if upload_to_drive is None:
        print("Error uploading image: no Drive uploader provided.")
        return None
    try:
        return upload_to_drive(img_data)
    except Exception as e:
        print(f"Error uploading image to Drive: {e}")
    return None

def has_expense_today():
    """Query the WEFT Expenses Notion DB for any entries logged today (ET). Returns bool."""
    today = datetime.datetime.now(pytz.timezone('US/Eastern')).strftime("%Y-%m-%d")
    payload = {"filter": {"property": "Date", "date": {"equals": today}}, "page_size": 1}
    data = _notion_request("POST", f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query", body=payload, timeout=10)
    if not data:
        return False  # fail open — send the reminder if unsure
    return len(data.get("results", [])) > 0

# --- NOTION TASKS ---

def write_task_to_notion(task_name, priority=None):
    """Write a new task to the WEFT Tasks Notion database."""
    date_str = datetime.datetime.now(pytz.timezone('US/Eastern')).strftime("%Y-%m-%d")
    properties = {
        "Task": {"title": [{"text": {"content": task_name}}]},
        "Status": {"select": {"name": "To Do"}},
        "Date Added": {"date": {"start": date_str}}
    }
    if priority and priority in ["High", "Medium", "Low"]:
        properties["Priority"] = {"select": {"name": priority}}
    payload = {"parent": {"database_id": NOTION_TASKS_DB_ID}, "properties": properties}
    return _notion_request("POST", "https://api.notion.com/v1/pages", body=payload, timeout=10)

def get_open_tasks_from_notion():
    """Retrieve all open tasks (Status = To Do) from Notion, returning list of dicts."""
    payload = {
        "filter": {"property": "Status", "select": {"equals": "To Do"}},
        "sorts": [{"property": "Date Added", "direction": "ascending"}]
    }
    data = _notion_request("POST", f"https://api.notion.com/v1/databases/{NOTION_TASKS_DB_ID}/query", body=payload)
    tasks = []
    if not data:
        return tasks
    for item in data.get("results", []):
        props = item.get("properties", {})
        title_prop = props.get("Task", {}).get("title", [])
        name = title_prop[0].get("plain_text", "Untitled") if title_prop else "Untitled"
        priority_prop = props.get("Priority", {}).get("select")
        priority = priority_prop.get("name") if priority_prop else None
        p_weight = {"High": 3, "Medium": 2, "Low": 1}.get(priority, 0)
        tasks.append({
            "id": item["id"],
            "name": name,
            "priority": priority,
            "priority_weight": p_weight
        })
    return tasks

def mark_tasks_done_in_notion(page_ids):
    """Mark a list of Notion page IDs as Done."""
    date_str = datetime.datetime.now(pytz.timezone('US/Eastern')).strftime("%Y-%m-%d")
    properties = {
        "Status": {"select": {"name": "Done"}},
        "Date Completed": {"date": {"start": date_str}}
    }
    success_count = 0
    for page_id in page_ids:
        res = _notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", body={"properties": properties}, timeout=10)
        if res:
            success_count += 1
        else:
            print(f"Error marking task {page_id} done.")
    return success_count

# --- NOTION FOOD (PANTRY & MEALS) ---

def write_food_to_notion(item_name, item_type, qty=None, unit=None, meal_type=None, status=None, image_url=None):
    """Write a new food record (Pantry or Meal Log) to the WEFT Food Notion database."""
    date_str = datetime.datetime.now(pytz.timezone('US/Eastern')).strftime("%Y-%m-%d")
    properties = {
        "Item": {"title": [{"text": {"content": item_name}}]},
        "Type": {"select": {"name": item_type}}
    }
    if qty is not None:
        properties["Quantity"] = {"number": float(qty)}
    if unit:
        properties["Unit"] = {"rich_text": [{"text": {"content": unit}}]}
    if item_type == "Pantry":
        properties["Date Added"] = {"date": {"start": date_str}}
        properties["Status"] = {"select": {"name": status or "In Stock"}}
    elif item_type == "Meal Log":
        properties["Date Consumed"] = {"date": {"start": date_str}}
        if meal_type:
            properties["Meal Type"] = {"select": {"name": meal_type}}
    if image_url:
        properties["Receipt Photo"] = {"files": [{"type": "external", "name": "photo.jpg", "external": {"url": image_url}}]}
    payload = {"parent": {"database_id": NOTION_FOOD_DB_ID}, "properties": properties}
    return _notion_request("POST", "https://api.notion.com/v1/pages", body=payload, timeout=10)

def get_pantry_from_notion():
    """Retrieve all Pantry items where Status != Out, returning list of dicts."""
    payload = {
        "filter": {
            "and": [
                {"property": "Type", "select": {"equals": "Pantry"}},
                {"property": "Status", "select": {"does_not_equal": "Out"}}
            ]
        }
    }
    data = _notion_request("POST", f"https://api.notion.com/v1/databases/{NOTION_FOOD_DB_ID}/query", body=payload)
    items = []
    if not data:
        return items
    for item in data.get("results", []):
        props = item.get("properties", {})
        title_prop = props.get("Item", {}).get("title", [])
        name = title_prop[0].get("plain_text", "") if title_prop else ""
        qty = props.get("Quantity", {}).get("number")
        unit_prop = props.get("Unit", {}).get("rich_text", [])
        unit = unit_prop[0].get("plain_text", "") if unit_prop else ""
        status_prop = props.get("Status", {}).get("select")
        status = status_prop.get("name") if status_prop else "In Stock"
        if name:
            items.append({"id": item["id"], "name": name, "qty": qty, "unit": unit, "status": status})
    return items

def update_pantry_item_in_notion(page_id, new_qty, new_status):
    """Update quantity and status of a pantry item."""
    properties = {}
    if new_qty is not None:
        properties["Quantity"] = {"number": float(new_qty)}
    if new_status:
        properties["Status"] = {"select": {"name": new_status}}
    if not properties:
        return False
    res = _notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", body={"properties": properties}, timeout=10)
    return res is not None

def attach_photo_to_notion_page(page_id, image_url):
    """Attach an external image URL to a Notion page as a file property or embed block."""
    res = _notion_request(
        "PATCH",
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        body={"children": [{"object": "block", "type": "image", "image": {"type": "external", "external": {"url": image_url}}}]},
        timeout=10
    )
    return res is not None
