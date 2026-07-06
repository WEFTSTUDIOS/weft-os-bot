import requests
import json
import datetime
import pytz
import base64
import re
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
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
        response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=15 )
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
    We upload to Imgur and store the resulting URL in the Receipt Image property.
    """
    try:
        headers = {"Authorization": "Client-ID 8a93649479261cb"}
        payload = {"image": base64.b64encode(img_data).decode("utf-8")}
        response = requests.post("https://api.imgur.com/3/image", headers=headers, data=payload, timeout=15 )
        if response.status_code == 200:
            return response.json()["data"]["link"]
    except Exception as e:
        print(f"Error uploading image: {e}")
    return None
