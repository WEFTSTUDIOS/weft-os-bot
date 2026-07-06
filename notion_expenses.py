import requests
import json
import datetime
import pytz
import base64
import re
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

NOTION_TOKEN = "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"
NOTION_DB_ID = "39575f5d-f642-810e-854e-c80528128539"

CATEGORIES = [
    "Fabric", "Hardware", "Tools/Equipment", "Software",
    "Travel", "Marketing", "Packaging/Shipping", "Food", "Personal", "Other"
]

def write_expense_to_notion(date_str, vendor, amount, category, biz_or_personal, note, source, image_url=None):
    """Write a new expense record to the WEFT Expenses Notion database."""
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    
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
        
    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": properties
    }
    
    try:
        response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error writing to Notion: {e}")
        if 'response' in locals() and hasattr(response, 'text'):
            print(f"Notion API response: {response.text}")
        return None

def build_category_keyboard(prefix, current_page=0):
    """Build an inline keyboard for selecting a category."""
    markup = InlineKeyboardMarkup()
    # For simplicity in Telegram, we can show all 10 categories in 2 columns
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
            model="claude-3-5-sonnet-20240620",
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
        # Clean up markdown if Claude included it despite instructions
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

# State management for multi-step flows
# Keys will be chat_id
expense_states = {}

def get_state(chat_id):
    if chat_id not in expense_states:
        expense_states[chat_id] = {}
    return expense_states[chat_id]

def clear_state(chat_id):
    if chat_id in expense_states:
        del expense_states[chat_id]

def upload_file_to_notion_or_imgur(img_data):
    """
    Notion API doesn't support direct file uploads via the public API for the 'files' property.
    We must provide an external URL. We'll use a free image host like Imgur for this bot.
    """
    try:
        # Using a public client ID for Imgur anonymous uploads (temporary solution)
        headers = {"Authorization": "Client-ID 8a93649479261cb"}
        payload = {"image": base64.b64encode(img_data).decode("utf-8")}
        response = requests.post("https://api.imgur.com/3/image", headers=headers, data=payload, timeout=15)
        if response.status_code == 200:
            return response.json()["data"]["link"]
    except Exception as e:
        print(f"Error uploading image: {e}")
    return None

def has_expense_today():
    """Query the WEFT Expenses Notion DB for any entries logged today (ET). Returns bool."""
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    today = datetime.datetime.now(pytz.timezone('US/Eastern')).strftime("%Y-%m-%d")
    payload = {
        "filter": {
            "property": "Date",
            "date": {"equals": today}
        },
        "page_size": 1
    }
    try:
        response = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers=headers, json=payload, timeout=10
        )
        data = response.json()
        return len(data.get("results", [])) > 0
    except Exception as e:
        print(f"Notion today-check error: {e}")
        return False  # fail open — send the reminder if unsure

# --- NOTION TASKS ---

NOTION_TASKS_DB_ID = "39575f5d-f642-81f4-bcc9-e40542ce4721"

def write_task_to_notion(task_name, priority=None):
    """Write a new task to the WEFT Tasks Notion database."""
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    date_str = datetime.datetime.now(pytz.timezone('US/Eastern')).strftime("%Y-%m-%d")
    
    properties = {
        "Task": {"title": [{"text": {"content": task_name}}]},
        "Status": {"select": {"name": "To Do"}},
        "Date Added": {"date": {"start": date_str}}
    }
    
    if priority and priority in ["High", "Medium", "Low"]:
        properties["Priority"] = {"select": {"name": priority}}
        
    payload = {
        "parent": {"database_id": NOTION_TASKS_DB_ID},
        "properties": properties
    }
    
    try:
        response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error writing task to Notion: {e}")
        return None

def get_open_tasks_from_notion():
    """Retrieve all open tasks (Status = To Do) from Notion, returning list of dicts."""
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    
    payload = {
        "filter": {
            "property": "Status",
            "select": {"equals": "To Do"}
        },
        # Sort by Date Added ascending so oldest are first
        "sorts": [{"property": "Date Added", "direction": "ascending"}]
    }
    
    tasks = []
    try:
        response = requests.post(f"https://api.notion.com/v1/databases/{NOTION_TASKS_DB_ID}/query", headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        for item in data.get("results", []):
            props = item.get("properties", {})
            
            # Extract Task Name
            title_prop = props.get("Task", {}).get("title", [])
            name = title_prop[0].get("plain_text", "Untitled") if title_prop else "Untitled"
            
            # Extract Priority
            priority_prop = props.get("Priority", {}).get("select")
            priority = priority_prop.get("name") if priority_prop else None
            
            # Map priority to a sort weight (High=3, Medium=2, Low=1, None=0)
            p_weight = {"High": 3, "Medium": 2, "Low": 1}.get(priority, 0)
            
            tasks.append({
                "id": item["id"],
                "name": name,
                "priority": priority,
                "priority_weight": p_weight
            })
            
        return tasks
    except Exception as e:
        print(f"Error retrieving tasks from Notion: {e}")
        return []

def mark_tasks_done_in_notion(page_ids):
    """Mark a list of Notion page IDs as Done."""
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    date_str = datetime.datetime.now(pytz.timezone('US/Eastern')).strftime("%Y-%m-%d")
    
    properties = {
        "Status": {"select": {"name": "Done"}},
        "Date Completed": {"date": {"start": date_str}}
    }
    
    success_count = 0
    for page_id in page_ids:
        try:
            payload = {"properties": properties}
            requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=headers, json=payload, timeout=10)
            success_count += 1
        except Exception as e:
            print(f"Error marking task {page_id} done: {e}")
            
    return success_count
