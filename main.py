import telebot
import requests
import json
import datetime
import pytz
import os
import re
import random
import time
import threading
import base64
import schedule
import anthropic
from http.server import HTTPServer, BaseHTTPRequestHandler
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from notion_expenses import (
    write_expense_to_notion, extract_receipt_data_with_claude,
    build_category_keyboard, build_biz_personal_keyboard, build_confirmation_keyboard,
    get_state, clear_state, upload_file_to_notion_or_imgur, has_expense_today,
    write_task_to_notion, get_open_tasks_from_notion, mark_tasks_done_in_notion,
    write_food_to_notion, get_pantry_from_notion, update_pantry_item_in_notion, attach_photo_to_notion_page
)

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SPREADSHEET_ID = "1A9xUO-6pyn7z8_yadwkyXtyVXcuGbkvPR_IZp9wQ7lg"
REPORT_EMAIL = "byweftstudios@gmail.com"

# --- CONNECTIONS ---
SHEETS_CONNECTION_ID = "ca_IiHAEZge9MFQ"
MAIN_CONNECTION_ID   = SHEETS_CONNECTION_ID
DRIVE_CONNECTION_ID  = "ca_DtO3TQJSSycg"

WEFT_GMAIL_ID       = "ca_jGjDU1VkI0nt"
KARIM_CONNECTION_ID = "ca_MPxmaIWiL6Kh"
OLD_CONNECTION_ID   = "ca_-8JPIJXZII1P"
DAD_CONNECTION_ID   = "ca_R0AvyogLME_t"

_WEFT_STUDIOS_FOLDER_ID = None

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)
est = pytz.timezone('US/Eastern')
user_state = {}
LAST_CHAT_ID_FILE = "/tmp/last_chat_id.txt"

def get_last_chat_id():
    if os.path.exists(LAST_CHAT_ID_FILE):
        with open(LAST_CHAT_ID_FILE, "r") as f:
            content = f.read().strip()
            return int(content) if content else None
    return None

def set_last_chat_id(chat_id):
    with open(LAST_CHAT_ID_FILE, "w") as f:
        f.write(str(chat_id))

def execute_proxy(endpoint, method="GET", body=None, connection_id=None):
    url = "https://backend.composio.dev/api/v3.1/tools/execute/proxy"
    headers = {
        "x-api-key": COMPOSIO_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "connected_account_id": connection_id or MAIN_CONNECTION_ID,
        "endpoint": endpoint,
        "method": method
    }
    if body:
        payload["body"] = body
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        return response.json()
    except requests.exceptions.Timeout:
        return {"error": "Request timed out."}
    except Exception as e:
        return {"error": str(e)}

def _get_weft_studios_folder_id():
    global _WEFT_STUDIOS_FOLDER_ID
    if _WEFT_STUDIOS_FOLDER_ID:
        return _WEFT_STUDIOS_FOLDER_ID
    try:
        import urllib.parse
        q = urllib.parse.quote("name='WEFT Studios' and mimeType='application/vnd.google-apps.folder' and trashed=false")
        res = execute_proxy(
            f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name)",
            connection_id=DRIVE_CONNECTION_ID
        )
        files = res.get("data", {}).get("files", [])
        if files:
            _WEFT_STUDIOS_FOLDER_ID = files[0]["id"]
    except Exception as e:
        print(f"Drive folder lookup error: {e}")
    return _WEFT_STUDIOS_FOLDER_ID

def drive_backup(tab_name, values):
    try:
        folder_id = _get_weft_studios_folder_id()
        now_str = datetime.datetime.now(est).strftime("%Y-%m-%d %H:%M:%S ET")
        import json as _json
        summary = _json.dumps({"tab": tab_name, "rows": values, "logged_at": now_str}, ensure_ascii=False)
        file_name = f"weft_log_{datetime.datetime.now(est).strftime('%Y%m%d_%H%M%S')}_{tab_name}.json"
        meta = {"name": file_name, "description": summary, "mimeType": "application/json"}
        if folder_id:
            meta["parents"] = [folder_id]
        execute_proxy(
            "https://www.googleapis.com/drive/v3/files",
            method="POST",
            body=meta,
            connection_id=DRIVE_CONNECTION_ID
        )
    except Exception as e:
        print(f"Drive backup error: {e}")

SHEETS_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK", "")

def _webhook_post(action, tab, **kwargs):
    payload = {"action": action, "tab": tab}
    payload.update(kwargs)
    try:
        r = requests.post(SHEETS_WEBHOOK_URL, json=payload, timeout=20)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def sheets_append(sheet_name, values):
    if SHEETS_WEBHOOK_URL:
        result = _webhook_post("append", sheet_name, rows=values)
    else:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_name}!A1:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
        result = execute_proxy(url, method="POST", body={"values": values})
    drive_backup(sheet_name, values)
    return result

def sheets_get(sheet_name, range_notation="A:E"):
    if SHEETS_WEBHOOK_URL:
        result = _webhook_post("read", sheet_name)
        if "values" in result:
            return {"data": {"values": result["values"]}}
        return result
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_name}!{range_notation}?majorDimension=ROWS"
    return execute_proxy(url, method="GET")

def sheets_batch_update(updates):
    batch_body = {"valueInputOption": "RAW", "data": updates}
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values:batchUpdate"
    return execute_proxy(url, method="POST", body=batch_body)

def safe_send(chat_id, text):
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        print(f"Error sending message: {e}")

def get_unchecked_tasks():
    tasks = get_open_tasks_from_notion()
    return [(i+1, t["name"]) for i, t in enumerate(tasks)]

@bot.message_handler(content_types=['photo'])
def handle_photo(message, caption=None):
    set_last_chat_id(message.chat.id)
    chat_id = message.chat.id

    state = get_state(chat_id)
    target_page_id = state.get('last_expense_id') or state.get('last_food_id')

    if not target_page_id:
        bot.send_message(chat_id, "Send a photo immediately after /spent, /groceries, or /ate to attach it.")
        return

    status_msg = bot.send_message(chat_id, "Uploading and attaching photo...")

    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        img_data = requests.get(file_url, timeout=15).content

        image_url = upload_file_to_notion_or_imgur(img_data)

        if image_url:
            success = attach_photo_to_notion_page(target_page_id, image_url)
            if success:
                bot.edit_message_text("Photo attached to Notion.", chat_id, status_msg.message_id)
            else:
                bot.edit_message_text("Uploaded, but failed to attach to Notion.", chat_id, status_msg.message_id)
        else:
            bot.edit_message_text("Failed to upload photo.", chat_id, status_msg.message_id)

        if 'last_expense_id' in state: del state['last_expense_id']
        if 'last_food_id' in state: del state['last_food_id']

    except Exception as e:
        print(f"Photo attach error: {e}")
        bot.edit_message_text(f"Error attaching photo: {e}", chat_id, status_msg.message_id)

def parse_food_item(item_str):
    parts = item_str.strip().split(' ', 1)
    if not parts:
        return None, None, ""

    qty_str = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    match = re.match(r'^([\d.]+)([a-zA-Z]*)$', qty_str)
    if match:
        qty = float(match.group(1))
        unit = match.group(2)

        if not unit and rest:
            next_word = rest.split(' ', 1)[0].lower()
            if next_word in ['gallon', 'oz', 'lb', 'lbs', 'cup', 'cups', 'ct', 'pack', 'bag', 'box']:
                unit = next_word
                rest = rest.split(' ', 1)[1] if ' ' in rest else ""

        return qty, unit, rest.strip()

    return None, None, item_str.strip()

def get_meal_type():
    hour = datetime.datetime.now(est).hour
    if hour < 11: return "Breakfast"
    if hour < 15: return "Lunch"
    if hour < 20: return "Dinner"
    return "Snack"

@bot.message_handler(commands=['groceries'])
def add_groceries(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/groceries', '').strip()
    if not text:
        safe_send(message.chat.id, "Try: /groceries 6 eggs, 1 gallon milk")
        return

    items = [i.strip() for i in text.split(',')]
    added = []

    for item_str in items:
        if not item_str: continue
        qty, unit, name = parse_food_item(item_str)
        if not name: name = item_str

        res = write_food_to_notion(name, "Pantry", qty=qty, unit=unit, status="In Stock")
        if res:
            get_state(message.chat.id)['last_food_id'] = res['id']
            added.append(name)

    safe_send(message.chat.id, f"Added {len(added)} items to Pantry.")

@bot.message_handler(commands=['ate'])
def log_ate(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/ate', '').strip()
    if not text:
        safe_send(message.chat.id, "Try: /ate 2 croissants, 2% milk")
        return

    items = [i.strip() for i in text.split(',')]
    meal_type = get_meal_type()
    pantry = get_pantry_from_notion()

    logged = []
    deducted = []

    for item_str in items:
        if not item_str: continue
        qty, unit, name = parse_food_item(item_str)
        if not name: name = item_str

        res = write_food_to_notion(name, "Meal Log", qty=qty, unit=unit, meal_type=meal_type)
        if res:
            get_state(message.chat.id)['last_food_id'] = res['id']
            logged.append(name)

        if qty is not None:
            name_lower = name.lower()
            for p_item in pantry:
                if name_lower in p_item['name'].lower() or p_item['name'].lower() in name_lower:
                    if p_item['qty'] is not None:
                        new_qty = max(0, p_item['qty'] - qty)
                        new_status = "In Stock"
                        if new_qty == 0:
                            new_status = "Out"
                        elif new_qty <= 2:
                            new_status = "Low"

                        if update_pantry_item_in_notion(p_item['id'], new_qty, new_status):
                            deducted.append(f"{p_item['name']} ({new_qty} left)")
                    break

    reply = f"Logged {len(logged)} items for {meal_type}."
    if deducted:
        reply += "\n\nPantry updated:\n- " + "\n- ".join(deducted)

    safe_send(message.chat.id, reply)

@bot.message_handler(commands=['fridge'])
def fridge_handler(message):
    set_last_chat_id(message.chat.id)
    items = get_pantry_from_notion()

    if not items:
        safe_send(message.chat.id, "Pantry is empty!")
        return

    lines = []
    for item in items:
        qty_str = f"{item['qty']} " if item['qty'] is not None else ""
        unit_str = f"{item['unit']} " if item['unit'] else ""
        status_str = " Low" if item['status'] == "Low" else ""
        lines.append(f"- {qty_str}{unit_str}{item['name']}{status_str}")

    safe_send(message.chat.id, "YOUR PANTRY:\n" + "\n".join(lines))

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    set_last_chat_id(message.chat.id)
    help_text = (
        "WEFT OS COMMANDS\n\n"
        "FINANCIALS\n"
        "/log Depop $45 jeans - Log income\n"
        "/spent - Log expense (shorthand or guided flow -> Notion)\n"
        "/backfill - Batch-log old bank statement expenses\n"
        "[photo] - Send a receipt photo to auto-log it\n"
        "/sub Netflix $15 June24 - Log subscription\n"
        "/weekcheck - Weekly breakdown\n\n"
        "PRODUCTIVITY\n"
        "/morning - Daily briefing\n"
        "/focus [task] - Start focus mode\n"
        "/plan - Sunday planning session\n"
        "/tasks - List your tasks\n"
        "/top3 - Your 3 most urgent pending tasks\n"
        "/transition - One concrete next action\n"
        "/addtask [task] - Add a task (supports multiple lines)\n"
        "/done 1 3 4 - Mark tasks done\n"
        "/brain [thoughts] - Dump and organize\n"
        "/stuck - ADHD reset\n"
        "/hype - Get motivated\n"
        "/habit - Daily checklist\n"
        "/wins [win] - Log a win\n\n"
        "FOOD & PANTRY\n"
        "/groceries 6 eggs, 1 gallon milk - Add items to Pantry\n"
        "/ate 2 eggs, coffee - Log a meal + deduct from Pantry\n"
        "/fridge - See what's in stock\n"
        "/workout - Today's workout\n\n"
        "CONTENT\n"
        "/publer_all - Post to all platforms\n"
    )
    safe_send(message.chat.id, help_text)

@bot.message_handler(commands=['morning'])
def morning_briefing(message):
    set_last_chat_id(message.chat.id)
    safe_send(message.chat.id, "Grabbing your briefing, Keem...")

    query = "is:unread category:primary -from:google.com -from:noreply -from:no-reply -from:alerts -from:notifications -from:tiktok.com -from:shopify.com"
    email_res = execute_proxy(f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=10&q={requests.utils.quote(query)}")
    emails = []
    skip_keywords = ['security alert', 'sign-in', 'verification', 'confirm your', 'welcome to', 'unsubscribe', 'reposted', 'notification', 'new follower', 'liked your']
    skip_domains = ['tiktok.com', 'shopify.com']
    if 'data' in email_res and 'messages' in email_res.get('data', {}):
        for msg in email_res['data']['messages'][:8]:
            m = execute_proxy(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}")
            msg_data = m.get('data', {})
            subject = "No Subject"
            sender = "Unknown"
            for header in msg_data.get('payload', {}).get('headers', []):
                if header['name'] == 'Subject':
                    subject = header['value']
                if header['name'] == 'From':
                    sender = header['value']
            if any(k in subject.lower() for k in skip_keywords):
                continue
            if any(d in sender.lower() for d in skip_domains):
                continue
            emails.append(f"- {subject} (from {sender})")
            if len(emails) >= 3:
                break

    now_dt = datetime.datetime.now(est)
    start_of_day = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    time_min = start_of_day.strftime("%Y-%m-%dT%H:%M:%S-05:00")
    cal_res = execute_proxy(f"https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin={time_min}&maxResults=5&singleEvents=true&orderBy=startTime")
    events = []
    if 'data' in cal_res and 'items' in cal_res.get('data', {}):
        for item in cal_res['data']['items']:
            start = item['start'].get('dateTime', item['start'].get('date', ''))
            events.append(f"- {item.get('summary', 'Untitled')} ({start})")

    income_res = sheets_get("Sheet1", "D:D")
    total_income = 0
    if "data" in income_res and "values" in income_res["data"]:
        for row in income_res["data"]["values"][1:]:
            try:
                total_income += float(row[0])
            except:
                pass

    expense_res = sheets_get("Spent", "D:D")
    total_spent = 0
    if "data" in expense_res and "values" in expense_res["data"]:
        for row in expense_res["data"]["values"][1:]:
            try:
                total_spent += float(row[0])
            except:
                pass

    net = total_income - total_spent

    quotes = [
        "Japanese craftsmanship, Arabic identity. Build the legacy.",
        "ADHD is a superpower when you have a system. Let's go.",
        "WEFT is more than denim. It is culture.",
        "Focused bursts today. Sewing, gym, brand.",
        "Success is handmade. Keep sewing, keep growing.",
        "60 pairs. Handmade. That is not a flex, that is a fact."
    ]

    briefing = "MORNING BRIEFING\n\n"
    briefing += "Top Emails:\n"
    briefing += ("\n".join(emails) if emails else "No important emails today.") + "\n\n"
    briefing += "Today's Schedule:\n"
    briefing += ("\n".join(events) if events else "Free day!") + "\n\n"
    briefing += f"Weekly Financials:\nIncome: ${total_income:.2f} | Spent: ${total_spent:.2f} | Net: ${net:.2f}\n\n"
    briefing += f"Motivation: {random.choice(quotes)}\n\n"
    briefing += "Focus for today:"

    safe_send(message.chat.id, briefing)

CATEGORIES_LOWER = {
    "fabric", "hardware", "tools/equipment", "software",
    "travel", "marketing", "packaging/shipping", "food", "personal", "other"
}
CATEGORY_DISPLAY = {
    "fabric": "Fabric", "hardware": "Hardware", "tools/equipment": "Tools/Equipment",
    "software": "Software", "travel": "Travel", "marketing": "Marketing",
    "packaging/shipping": "Packaging/Shipping", "food": "Food",
    "personal": "Personal", "other": "Other"
}

def parse_expense_shorthand(text):
    text = text.strip()
    if not text:
        return None
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        amount = float(parts[0].replace('$', ''))
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    vendor = parts[1]
    remaining = parts[2:]
    category = "Other"
    cat_index = None
    for i, token in enumerate(remaining):
        if token.lower() in CATEGORIES_LOWER:
            category = CATEGORY_DISPLAY[token.lower()]
            cat_index = i
            break
    if cat_index is not None:
        note_parts = remaining[cat_index + 1:]
    else:
        note_parts = remaining
    note = ' '.join(note_parts)
    full_lower = text.lower()
    biz_or_personal = "Business" if "business" in full_lower else "Personal"
    return {
        'amount': amount,
        'vendor': vendor,
        'category': category,
        'note': note,
        'biz_or_personal': biz_or_personal
    }

@bot.message_handler(commands=['spent'])
def log_expense(message):
    set_last_chat_id(message.chat.id)
    chat_id = message.chat.id
    args = message.text.replace('/spent', '').strip()

    if args:
        parsed = parse_expense_shorthand(args)
        if not parsed:
            safe_send(chat_id, "Couldn't parse that. Try:\n/spent 13.84 Walmart Food groceries\n\nOr just /spent for the guided flow.")
            return
        clear_state(chat_id)
        state = get_state(chat_id)
        state.update({
            'flow': 'spent',
            'source': 'Manual Entry',
            'date': datetime.datetime.now(est).strftime("%Y-%m-%d"),
            'amount': parsed['amount'],
            'vendor': parsed['vendor'],
            'category': parsed['category'],
            'biz_or_personal': parsed['biz_or_personal'],
            'note': parsed['note']
        })
        finalize_expense(chat_id)
        return

    clear_state(chat_id)
    state = get_state(chat_id)
    state['flow'] = 'spent'
    state['source'] = 'Manual Entry'
    state['date'] = datetime.datetime.now(est).strftime("%Y-%m-%d")
    msg = bot.send_message(chat_id, "How much did you spend? (e.g. 12.50)")
    bot.register_next_step_handler(msg, process_amount)

def process_amount(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    amount_str = message.text.replace('$', '').strip()
    try:
        state['amount'] = float(amount_str)
        msg = bot.send_message(chat_id, "Where? (Vendor name)")
        bot.register_next_step_handler(msg, process_vendor)
    except ValueError:
        msg = bot.send_message(chat_id, "Please enter a valid number (e.g. 12.50). How much?")
        bot.register_next_step_handler(msg, process_amount)

def process_vendor(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    state['vendor'] = message.text.strip()
    bot.send_message(chat_id, "Select a category:", reply_markup=build_category_keyboard("exp"))

@bot.callback_query_handler(func=lambda call: call.data.startswith('exp_cat_'))
def process_category(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state['category'] = call.data[len('exp_cat_'):]
    bot.edit_message_text(f"Category: {state['category']}", chat_id, call.message.message_id)
    bot.send_message(chat_id, "Business or Personal?", reply_markup=build_biz_personal_keyboard("exp"))

@bot.callback_query_handler(func=lambda call: call.data.startswith('exp_type_'))
def process_type(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state['biz_or_personal'] = call.data[len('exp_type_'):]
    bot.edit_message_text(f"Type: {state['biz_or_personal']}", chat_id, call.message.message_id)
    msg = bot.send_message(chat_id, "Short note? (or type 'skip')")
    bot.register_next_step_handler(msg, process_note)

def process_note(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    note = message.text.strip()
    state['note'] = '' if note.lower() == 'skip' else note
    finalize_expense(chat_id)

def finalize_expense(chat_id):
    state = get_state(chat_id)
    bot.send_message(chat_id, "Writing to Notion...")
    res = write_expense_to_notion(
        state.get('date'),
        state.get('vendor'),
        state.get('amount'),
        state.get('category'),
        state.get('biz_or_personal'),
        state.get('note', ''),
        state.get('source'),
        state.get('image_url')
    )
    if res:
        state['last_expense_id'] = res['id']
        notion_url = res.get('url', '')
        confirm = (
            f"Logged ${state.get('amount')} at {state.get('vendor')}\n"
            f"Category: {state.get('category')} | {state.get('biz_or_personal')}\n"
            f"Source: {state.get('source')}"
        )
        if notion_url:
            confirm += f"\nView: {notion_url}"
        if state.get('flow') == 'backfill':
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("Add Another", callback_data="backfill_another"))
            bot.send_message(chat_id, confirm, reply_markup=markup)
        else:
            bot.send_message(chat_id, confirm)
    else:
        bot.send_message(chat_id, "Error writing to Notion. Check Railway logs.")
    clear_state(chat_id)

@bot.message_handler(commands=['backfill'])
def backfill_expense(message):
    set_last_chat_id(message.chat.id)
    chat_id = message.chat.id
    args = message.text.replace('/backfill', '').strip()

    if args:
        parts = args.split()
        date_str = None
        rest = args
        if parts and re.match(r'\d{4}-\d{2}-\d{2}', parts[0]):
            date_str = parts[0]
            rest = ' '.join(parts[1:])
        elif parts and parts[0].lower() == 'today':
            date_str = datetime.datetime.now(est).strftime("%Y-%m-%d")
            rest = ' '.join(parts[1:])
        else:
            pass

        if date_str and rest:
            parsed = parse_expense_shorthand(rest)
            if not parsed:
                safe_send(chat_id, "Couldn't parse that. Try:\n/backfill 2025-06-15 13.84 Walmart Food groceries\n\nOr just /backfill for the guided flow.")
                return
            clear_state(chat_id)
            state = get_state(chat_id)
            state.update({
                'flow': 'backfill',
                'source': 'Bank Statement Backfill',
                'date': date_str,
                'amount': parsed['amount'],
                'vendor': parsed['vendor'],
                'category': parsed['category'],
                'biz_or_personal': parsed['biz_or_personal'],
                'note': parsed['note']
            })
            finalize_expense(chat_id)
            return

    clear_state(chat_id)
    state = get_state(chat_id)
    state['flow'] = 'backfill'
    state['source'] = 'Bank Statement Backfill'
    msg = bot.send_message(chat_id, "Backfill mode.\nDate of the transaction? (YYYY-MM-DD or 'today')")
    bot.register_next_step_handler(msg, process_backfill_date)

def process_backfill_date(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    date_str = message.text.strip()
    if date_str.lower() == 'today':
        date_str = datetime.datetime.now(est).strftime("%Y-%m-%d")
    if not re.match(r'\d{4}-\d{2}-\d{2}', date_str):
        msg = bot.send_message(chat_id, "Use YYYY-MM-DD format (e.g. 2025-06-15):")
        bot.register_next_step_handler(msg, process_backfill_date)
        return
    state['date'] = date_str
    msg = bot.send_message(chat_id, "Amount? (e.g. 12.50)")
    bot.register_next_step_handler(msg, process_amount)

@bot.callback_query_handler(func=lambda call: call.data == 'backfill_another')
def backfill_another(call):
    chat_id = call.message.chat.id
    last_date = get_state(chat_id).get('date', datetime.datetime.now(est).strftime("%Y-%m-%d"))
    clear_state(chat_id)
    state = get_state(chat_id)
    state['flow'] = 'backfill'
    state['source'] = 'Bank Statement Backfill'
    state['date'] = last_date
    bot.edit_message_text("Starting next entry...", chat_id, call.message.message_id)
    msg = bot.send_message(chat_id, f"Date kept as {last_date}.\nAmount? (e.g. 12.50)")
    bot.register_next_step_handler(msg, process_amount)

@bot.message_handler(commands=['log'])
def log_income(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/log', '').strip()

    match = re.search(r'(.+?)\s+\$(\d+(?:\.\d+)?)\s+(.+)', text)
    if match:
        source = match.group(1).strip()
        amount = match.group(2)
        item = match.group(3).strip()
    else:
        match2 = re.search(r'\$(\d+(?:\.\d+)?)\s+(.+)', text)
        if match2:
            source = "Income"
            amount = match2.group(1)
            item = match2.group(2)
        else:
            safe_send(message.chat.id, "Try: /log Depop $45 jeans")
            return

    date = datetime.datetime.now(est).strftime("%Y-%m-%d")
    res = sheets_append("Sheet1", [[date, source, item, amount, "Logged via bot"]])
    if 'data' in res or 'error' not in res:
        safe_send(message.chat.id, f"Logged - {source} ${amount} for {item} on {date}")
    else:
        safe_send(message.chat.id, f"Error logging income: {res.get('error', 'unknown')}")

@bot.message_handler(commands=['weekcheck'])
def week_check(message):
    set_last_chat_id(message.chat.id)
    safe_send(message.chat.id, "Calculating your week...")

    income_res = sheets_get("Sheet1")
    expenses_res = sheets_get("Spent")

    now = datetime.datetime.now(est)
    week_start = (now - datetime.timedelta(days=now.weekday())).strftime("%Y-%m-%d")

    income_by_source = {}
    total_income = 0
    if "data" in income_res and "values" in income_res["data"]:
        for row in income_res["data"]["values"][1:]:
            if len(row) >= 4:
                date, source, item, amount = row[0], row[1], row[2], row[3]
                if date >= week_start:
                    try:
                        amt = float(amount)
                        income_by_source[source] = income_by_source.get(source, 0) + amt
                        total_income += amt
                    except:
                        pass

    total_spent = 0
    if "data" in expenses_res and "values" in expenses_res["data"]:
        for row in expenses_res["data"]["values"][1:]:
            if len(row) >= 4:
                date, amount = row[0], row[3]
                if date >= week_start:
                    try:
                        total_spent += float(amount)
                    except:
                        pass

    net = total_income - total_spent
    response = "WEEK CHECK\n\n"
    response += f"Income: ${total_income:.2f}\n"
    for source, amt in income_by_source.items():
        response += f"  {source}: ${amt:.2f}\n"
    response += f"Spent: ${total_spent:.2f}\n"
    response += f"Net: ${net:.2f}"

    safe_send(message.chat.id, response)

@bot.message_handler(commands=['sub'])
def log_subscription(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/sub', '').strip()
    match = re.search(r'(.+?)\s+\$(\d+(?:\.\d+)?)\s+(.+)', text)
    if match:
        name = match.group(1).strip()
        amount = match.group(2)
        date_str = match.group(3).strip()
        date = datetime.datetime.now(est).strftime("%Y-%m-%d")
        res = sheets_append("Subscriptions", [[name, amount, date, "", date_str]])
        if 'data' in res or 'error' not in res:
            safe_send(message.chat.id, f"Logged subscription: {name} ${amount}")
        else:
            safe_send(message.chat.id, "Error logging subscription.")
    else:
        safe_send(message.chat.id, "Try: /sub Manus $60 June24")

@bot.message_handler(commands=['wins'])
def log_win(message):
    set_last_chat_id(message.chat.id)
    win_text = message.text.replace('/wins', '').strip()
    if not win_text:
        safe_send(message.chat.id, "What did you win today? Try: /wins Finished the sample")
        return
    date = datetime.datetime.now(est).strftime("%Y-%m-%d")
    sheets_append("Wins", [[date, win_text, "Logged via bot"]])
    safe_send(message.chat.id, f"Win logged: {win_text}")

@bot.message_handler(commands=['addtask'])
def add_task(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/addtask', '').strip()
    if not text:
        safe_send(message.chat.id, "Try: /addtask Finish the mockup\n(Or send multiple lines to add multiple tasks)")
        return

    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if len(lines) == 1:
        write_task_to_notion(lines[0])
        safe_send(message.chat.id, f"Added: {lines[0]}. Get it done.")
    else:
        for line in lines:
            write_task_to_notion(line)
        safe_send(message.chat.id, f"Added {len(lines)} tasks to Notion. Get them done.")

@bot.message_handler(commands=['tasks'])
def list_tasks(message):
    set_last_chat_id(message.chat.id)
    tasks = get_open_tasks_from_notion()

    if not tasks:
        safe_send(message.chat.id, "No open tasks. Add one with /addtask")
        return

    lines = []
    for i, t in enumerate(tasks, 1):
        prefix = f"[{t['priority']}] " if t['priority'] else ""
        lines.append(f"{i}. {prefix}{t['name']}")

    response = "OPEN TASKS\n\n" + "\n".join(lines)
    safe_send(message.chat.id, response)

@bot.message_handler(commands=['top3'])
def top3_tasks(message):
    set_last_chat_id(message.chat.id)
    tasks = get_open_tasks_from_notion()

    if not tasks:
        safe_send(message.chat.id, "All tasks done. Add new ones with /addtask")
        return

    sorted_tasks = sorted(tasks, key=lambda x: x["priority_weight"], reverse=True)
    top = sorted_tasks[:3]

    lines = []
    for j, t in enumerate(top, 1):
        prefix = f"[{t['priority']}] " if t['priority'] else ""
        lines.append(f"{j}. {prefix}{t['name']}")

    safe_send(message.chat.id, f"TOP 3\n\n{chr(10).join(lines)}\n\nLock in. Get these done.")

@bot.message_handler(commands=['done'])
def mark_done(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/done', '').strip()
    try:
        nums = [int(n) for n in text.split()]
    except ValueError:
        safe_send(message.chat.id, "Try: /done 1 3 4")
        return

    tasks = get_open_tasks_from_notion()
    if not tasks:
        safe_send(message.chat.id, "No open tasks to mark done.")
        return

    page_ids_to_mark = []
    completed_names = []
    for num in nums:
        if 1 <= num <= len(tasks):
            t = tasks[num-1]
            page_ids_to_mark.append(t["id"])
            completed_names.append(t["name"])

    if page_ids_to_mark:
        success_count = mark_tasks_done_in_notion(page_ids_to_mark)

        remaining = [t for i, t in enumerate(tasks, 1) if i not in nums]

        response = f"Checked off {success_count} tasks - lets go Keem.\n\n"
        if remaining:
            lines = [f"  {i+1}. {t['name']}" for i, t in enumerate(remaining)]
            response += f"{len(remaining)} left:\n" + "\n".join(lines) + "\n\nKeep pushing."
        else:
            response += "All done! Great work today."

        safe_send(message.chat.id, response)
    else:
        safe_send(message.chat.id, "No valid task numbers found.")

@bot.message_handler(commands=['transition'])
def transition_action(message):
    set_last_chat_id(message.chat.id)
    tasks = get_open_tasks_from_notion()

    if not tasks:
        safe_send(message.chat.id, "Next: take a breath. No open tasks right now.")
        return

    sorted_tasks = sorted(tasks, key=lambda x: x["priority_weight"], reverse=True)
    next_task = sorted_tasks[0]

    safe_send(message.chat.id, f"Next: {next_task['name']}")

@bot.message_handler(commands=['focus'])
def focus_mode(message):
    set_last_chat_id(message.chat.id)
    task = message.text.replace('/focus', '').strip()
    if not task:
        task = "Deep Work"
    safe_send(message.chat.id, f"FOCUS MODE: {task.upper()}\n\nPhone down. Lock in. Legacy in progress.\n\n90 minutes. Go.")

@bot.message_handler(commands=['brain'])
def brain_dump(message):
    set_last_chat_id(message.chat.id)
    thoughts = message.text.replace('/brain', '').strip()
    if not thoughts:
        safe_send(message.chat.id, "Dump everything on your mind in one message after /brain")
        return
    safe_send(message.chat.id, f"Got it. Organizing...\n\nNOW: {thoughts.split('.')[0] if '.' in thoughts else thoughts}\n\nLATER: Everything else\n\nFORGET IT: Anything not on this list")

@bot.message_handler(commands=['stuck'])
def stuck_reset(message):
    set_last_chat_id(message.chat.id)
    tips = [
        "1. Stand up and drink water\n2. Do the smallest possible version of the task\n3. Set a 10 minute timer and start",
        "1. Put on music\n2. Clear your desk of everything except one thing\n3. Work on that one thing for 10 minutes",
        "1. Walk around for 2 minutes\n2. Write down the ONE next action\n3. Do only that"
    ]
    safe_send(message.chat.id, f"ADHD RESET\n\n{random.choice(tips)}\n\nYou got this Keem.")

@bot.message_handler(commands=['hype'])
def hype(message):
    set_last_chat_id(message.chat.id)
    lines = [
        "Success is handmade. Keep sewing, keep growing.",
        "ADHD is a superpower when you have the right systems.",
        "Drop 001 is coming. Every stitch counts.",
        "You built this from nothing. Keep going.",
        "The brand is real. The work is real. You are real.",
        "60 pairs. Handmade. That is not a flex, that is a fact.",
        "Japanese craftsmanship. Arabic identity. WEFT Studios."
    ]
    safe_send(message.chat.id, random.choice(lines))

@bot.message_handler(commands=['habit'])
def habit_check(message):
    set_last_chat_id(message.chat.id)
    safe_send(message.chat.id,
        "Daily Habits:\n"
        "- Morning walk\n"
        "- Pull-ups\n"
        "- Smoothie\n"
        "- Read before you hit the cart\n"
        "- Log income or expenses\n"
        "- One WEFT task done"
    )

@bot.message_handler(commands=['read'])
def read_reminder(message):
    set_last_chat_id(message.chat.id)
    safe_send(message.chat.id, "You said no cart until you read. Open a book or article first. Then come back.")

@bot.message_handler(commands=['workout'])
def get_workout(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/workout', '').strip().lower()

    workouts = {
        "monday": "PUSH DAY\n- Bench Press 4x8\n- Incline Dumbbell Press 3x10\n- Shoulder Press 3x10\n- Lateral Raises 3x12\n- Tricep Pushdowns 3x12\n- Chest Flyes 3x12",
        "tuesday": "PULL DAY\n- Deadlift 4x6\n- Pull-Ups 4x8\n- Barbell Row 3x10\n- Cable Row 3x12\n- Face Pulls 3x15\n- Bicep Curls 3x12",
        "thursday": "LEG DAY\n- Squat 4x8\n- Romanian Deadlift 3x10\n- Leg Press 3x12\n- Walking Lunges 3x12\n- Leg Curl 3x12\n- Calf Raises 4x15",
        "friday": "FULL BODY\n- Squat 3x8\n- Bench Press 3x8\n- Deadlift 3x6\n- Pull-Ups 3x8\n- Shoulder Press 3x10\n- Plank 3x45sec",
        "wednesday": "Rest day. Walk outside. That is your anchor. Come back ready to work.",
        "saturday": "Rest day. Walk outside. That is your anchor. Come back ready to work.",
        "sunday": "Rest day. Walk outside. That is your anchor. Come back ready to work."
    }

    day_map = {
        "mon": "monday", "tue": "tuesday", "wed": "wednesday",
        "thu": "thursday", "fri": "friday", "sat": "saturday", "sun": "sunday"
    }

    if text in workouts:
        day = text
    elif text in day_map:
        day = day_map[text]
    else:
        day = datetime.datetime.now(est).strftime("%A").lower()

    workout = workouts.get(day, "No workout found for that day.")
    safe_send(message.chat.id, f"{day.upper()}\n\n{workout}\n\nLock in. No distractions.")

@bot.message_handler(commands=['plan'])
def planning_session(message):
    set_last_chat_id(message.chat.id)
    user_state[message.chat.id] = {'state': 'plan_q1'}
    safe_send(message.chat.id, "WEEKLY PLANNING\n\nWhat are your top 3 priorities this week? (comma separated)")

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.chat.id), dict))
def handle_state(message):
    set_last_chat_id(message.chat.id)
    state = user_state.get(message.chat.id, {})
    current = state.get('state')

    if current == 'plan_q1':
        user_state[message.chat.id] = {'state': 'plan_q2', 'q1': message.text}
        safe_send(message.chat.id, "Anything carrying over from last week that's unfinished? (comma separated, or 'none')")

    elif current == 'plan_q2':
        q1 = state.get('q1', '')
        q2 = message.text

        priorities = [p.strip() for p in q1.split(',') if p.strip()]
        for p in priorities:
            write_task_to_notion(f"[This Week] {p}", priority="High")

        carryovers = []
        if q2.lower() not in ['none', 'no', 'skip']:
            carryovers = [c.strip() for c in q2.split(',') if c.strip()]
            for c in carryovers:
                write_task_to_notion(f"[Carryover] {c}", priority="Medium")

        summary = "WEEK LOCKED IN\n\n"
        summary += "GYM SPLIT\n"
        summary += "Mon: Push\nTue: Pull\nWed: Rest/Walk\nThu: Legs\nFri: Full Body\nSat/Sun: Rest/Walk\n\n"

        summary += "TOP PRIORITIES\n"
        for i, p in enumerate(priorities, 1):
            summary += f"{i}. {p}\n"

        if carryovers:
            summary += "\nCARRYOVERS\n"
            for i, c in enumerate(carryovers, 1):
                summary += f"- {c}\n"

        summary += "\nGo make it happen. This week counts."
        safe_send(message.chat.id, summary)
        del user_state[message.chat.id]

EMAIL_SKIP_KEYWORDS = ['security alert', 'sign-in', 'verification', 'confirm your', 'welcome to',
                       'unsubscribe', 'reposted', 'notification', 'new follower', 'liked your']
EMAIL_SKIP_DOMAINS  = ['tiktok.com', 'shopify.com']
DAD_IMPORTANT_KEYWORDS = ['government', 'irs', 'tax', 'bank', 'chase', 'wells fargo', 'credit',
                           'legal', 'court', 'attorney', 'lawyer', 'medical', 'doctor', 'hospital',
                           'insurance', 'bill', 'utility', 'electric', 'water', 'rent', 'mortgage',
                           'notice', 'overdue', 'payment due', 'statement', 'immigration', 'visa',
                           'passport', 'social security', 'medicare', 'medicaid', 'dmv', 'license']

COMPOSIO_PROXY = "https://backend.composio.dev/api/v3.1/tools/execute/proxy"

def _composio_headers():
    return {"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"}

def _gmail_get(connection_id, endpoint):
    payload = {"connected_account_id": connection_id, "endpoint": endpoint, "method": "GET"}
    try:
        return requests.post(COMPOSIO_PROXY, headers=_composio_headers(), json=payload, timeout=15).json()
    except Exception as e:
        return {"error": str(e)}

def _parse_headers(msg_data):
    subject, sender = "No Subject", "Unknown"
    for h in msg_data.get('payload', {}).get('headers', []):
        if h['name'] == 'Subject':
            subject = h['value']
        elif h['name'] == 'From':
            sender = h['value']
    return subject, sender

def fetch_top_emails(connection_id, max_results=8):
    if not connection_id:
        return ["Not connected."]
    query = ("is:unread category:primary "
             "-from:google.com -from:noreply -from:no-reply "
             "-from:alerts -from:notifications "
             "-from:tiktok.com -from:shopify.com")
    res = _gmail_get(connection_id,
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={max_results}&q={requests.utils.quote(query)}")
    if 'data' not in res or 'messages' not in res.get('data', {}):
        return ["Inbox clear or error."]

    emails = []
    for msg in res['data']['messages']:
        m = _gmail_get(connection_id,
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}")
        subject, sender = _parse_headers(m.get('data', {}))
        if any(k in subject.lower() for k in EMAIL_SKIP_KEYWORDS):
            continue
        if any(d in sender.lower() for d in EMAIL_SKIP_DOMAINS):
            continue
        emails.append(f"{len(emails)+1}. {subject}\n   from {sender}")
        if len(emails) >= 3:
            break
    return emails if emails else ["Inbox clear."]

def fetch_dad_emails(connection_id, max_results=20):
    if not connection_id:
        return ["Not connected."]
    query = "is:unread -from:noreply -from:no-reply -from:alerts -from:notifications"
    res = _gmail_get(connection_id,
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={max_results}&q={requests.utils.quote(query)}")
    if 'data' not in res or 'messages' not in res.get('data', {}):
        return ["No important emails found."]

    important = []
    for msg in res['data']['messages']:
        if len(important) >= 3:
            break
        m = _gmail_get(connection_id,
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}")
        subject, sender = _parse_headers(m.get('data', {}))
        subj_lower = subject.lower()
        send_lower = sender.lower()
        if not any(k in subj_lower or k in send_lower for k in DAD_IMPORTANT_KEYWORDS):
            continue
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=80,
                messages=[{"role": "user", "content":
                    f"Summarise this email subject in ONE plain sentence a non-native English speaker can easily understand. "
                    f"Be direct and factual. Subject: '{subject}' From: '{sender}'. "
                    f"Reply with only the sentence, nothing else."}]
            )
            summary = resp.content[0].text.strip()
        except Exception:
            summary = subject
        important.append(f"{len(important)+1}. {summary}\n   from {sender}")

    return important if important else ["No important emails found."]

@bot.message_handler(commands=['emails'])
def multi_inbox(message):
    set_last_chat_id(message.chat.id)
    safe_send(message.chat.id, "Checking all 4 inboxes...")

    weft_emails   = fetch_top_emails(WEFT_GMAIL_ID)
    karim_emails  = fetch_top_emails(KARIM_CONNECTION_ID)
    old_emails    = fetch_top_emails(OLD_CONNECTION_ID)
    dad_emails    = fetch_dad_emails(DAD_CONNECTION_ID)

    def fmt(lines):
        return "\n".join(lines) if lines else "Inbox clear."

    response = (
        "WEFT INBOX (byweftstudios)\n" + fmt(weft_emails) + "\n\n"
        "PERSONAL INBOX (karimidrisofficial)\n" + fmt(karim_emails) + "\n\n"
        "OLD INBOX (2008karimidris)\n" + fmt(old_emails) + "\n\n"
        "DAD'S IMPORTANT EMAILS (omarsudan007)\n" + fmt(dad_emails)
    )
    safe_send(message.chat.id, response)

@bot.message_handler(commands=['ginger_morning'])
def ginger_morning(message):
    set_last_chat_id(message.chat.id)
    safe_send(message.chat.id, "Ginger shot time. Take it now. After this shot - work mode begins. No excuses.")

@bot.message_handler(commands=['ginger_night'])
def ginger_night(message):
    set_last_chat_id(message.chat.id)
    safe_send(message.chat.id, "Night ginger shot. Take it, do your face routine, brush your teeth. That is your signal - wind down begins now. Phone goes down after this.")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    set_last_chat_id(message.chat.id)
    text = message.text.lower()
    if any(word in text for word in ['hi', 'hello', 'hey', 'sup', 'yo']):
        safe_send(message.chat.id, "WEFT OS online. Type /help to see all commands.")
    else:
        safe_send(message.chat.id, "Type /help to see all commands.")

def send_weekly_email():
    chat_id = get_last_chat_id()
    now = datetime.datetime.now(est)
    week_start = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    today_str = now.strftime("%Y-%m-%d")

    income_res = sheets_get("Sheet1")
    income_by_source = {}
    total_income = 0
    if "data" in income_res and "values" in income_res["data"]:
        for row in income_res["data"]["values"][1:]:
            if len(row) >= 4 and row[0] >= week_start:
                try:
                    amt = float(row[3])
                    src = row[1] if len(row) > 1 else "Other"
                    income_by_source[src] = income_by_source.get(src, 0) + amt
                    total_income += amt
                except:
                    pass

    expense_res = sheets_get("Spent")
    expense_by_cat = {}
    total_spent = 0
    if "data" in expense_res and "values" in expense_res["data"]:
        for row in expense_res["data"]["values"][1:]:
            if len(row) >= 4 and row[0] >= week_start:
                try:
                    amt = float(row[3])
                    cat = row[2] if len(row) > 2 else "Other"
                    expense_by_cat[cat] = expense_by_cat.get(cat, 0) + amt
                    total_spent += amt
                except:
                    pass

    tasks_res = sheets_get("Tasks")
    completed_count = 0
    carried_tasks = []
    if "data" in tasks_res and "values" in tasks_res["data"]:
        for row in tasks_res["data"]["values"][1:]:
            status = row[2] if len(row) > 2 else ""
            task = row[1] if len(row) > 1 else ""
            if status == "Done":
                completed_count += 1
            elif task:
                carried_tasks.append(task)

    wins_res = sheets_get("Wins")
    wins_count = 0
    if "data" in wins_res and "values" in wins_res["data"]:
        wins_count = sum(1 for row in wins_res["data"]["values"][1:] if len(row) > 0 and row[0] >= week_start)

    food_res = sheets_get("Food Log")
    food_days = set()
    if "data" in food_res and "values" in food_res["data"]:
        for row in food_res["data"]["values"][1:]:
            if len(row) > 0 and row[0] >= week_start:
                food_days.add(row[0])

    launch_date = datetime.datetime(2025, 9, 1, tzinfo=est)
    days_left = (launch_date - now).days

    net = total_income - total_spent

    income_lines = "\n".join([f"  {src}: ${amt:.2f}" for src, amt in income_by_source.items()]) or "  None"
    expense_lines = "\n".join([f"  {cat}: ${amt:.2f}" for cat, amt in expense_by_cat.items()]) or "  None"
    carried_lines = ", ".join(carried_tasks) if carried_tasks else "None"

    body = (
        f"WEFT WEEKLY REPORT\n\n"
        f"MONEY\n"
        f"Income this week: ${total_income:.2f}\n{income_lines}\n"
        f"Spent this week: ${total_spent:.2f}\n{expense_lines}\n"
        f"Net: ${net:.2f}\n\n"
        f"TASKS\n"
        f"Completed: {completed_count}\n"
        f"Carried over: {len(carried_tasks)} - {carried_lines}\n\n"
        f"HABITS\n"
        f"Wins logged: {wins_count} days\n"
        f"Food logged: {len(food_days)} days\n\n"
        f"Drop 001 launches in {days_left} days. Keep going Keem."
    )

    subject = f"WEFT Weekly Report - {today_str}"

    gmail_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    import email.mime.text
    import email.mime.multipart
    msg = email.mime.multipart.MIMEMultipart()
    msg['To'] = REPORT_EMAIL
    msg['Subject'] = subject
    msg.attach(email.mime.text.MIMEText(body, 'plain'))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    execute_proxy(gmail_url, method="POST", body={"raw": raw})

    if chat_id:
        safe_send(chat_id, f"Weekly report sent to {REPORT_EMAIL}.")
    print(f"Weekly email sent for {today_str}")

def sunday_reset_reminder():
    chat_id = get_last_chat_id()
    if chat_id:
        safe_send(chat_id, "Time for your Sunday reset. Run /plan now - build your week before you sleep. Tomorrow starts tonight.")

def ginger_batch_reminder():
    chat_id = get_last_chat_id()
    if chat_id:
        safe_send(chat_id, "Make your ginger shot batch for the week. Blend fresh ginger, lemon, water. Store in the fridge. Costs nothing and hits different.")

def sunday_grocery_list():
    """Send a grocery list of low/out-of-stock pantry items — reads from Notion."""
    chat_id = get_last_chat_id()
    if not chat_id:
        return
    items = get_pantry_from_notion()
    low = [
        f"- {item['name']} ({item['qty']} left)" if item.get('qty') is not None else f"- {item['name']}"
        for item in items if item.get('status') in ('Low', 'Out')
    ]
    if low:
        msg = "Grocery list for this week:\n\n" + "\n".join(low) + "\n\nPick these up before Sunday ends."
    else:
        msg = "Pantry looks good - nothing critically low this week."
    safe_send(chat_id, msg)

def sunday_meal_prep_check():
    chat_id = get_last_chat_id()
    if not chat_id:
        return
    today_str = datetime.datetime.now(est).strftime("%Y-%m-%d")
    food_res = sheets_get("Food Log")
    logged_today = False
    if "data" in food_res and "values" in food_res["data"]:
        for row in food_res["data"]["values"][1:]:
            if len(row) > 0 and row[0] == today_str:
                logged_today = True
                break
    if not logged_today:
        safe_send(chat_id, "Have you prepped your food for the week? Season your chicken, cook your rice, prep your burritos. Future you will thank you.")

def check_subscriptions():
    chat_id = get_last_chat_id()
    if not chat_id:
        return
    res = sheets_get("Subscriptions", "A:E")
    if "data" not in res or "values" not in res["data"]:
        return
    today = datetime.datetime.now(est).date()
    rows = res["data"]["values"]
    for row in rows[1:]:
        if len(row) < 2:
            continue
        name = row[0]
        amount = row[1]
        renewal_str = row[4] if len(row) > 4 else ""
        if not renewal_str:
            continue
        renewal_date = None
        for fmt in ("%Y-%m-%d", "%b%d", "%B%d", "%b %d", "%B %d"):
            try:
                parsed = datetime.datetime.strptime(renewal_str.strip(), fmt)
                renewal_date = parsed.replace(year=today.year).date()
                if renewal_date < today:
                    renewal_date = renewal_date.replace(year=today.year + 1)
                break
            except:
                continue
        if renewal_date:
            days_until = (renewal_date - today).days
            if 0 <= days_until <= 3:
                safe_send(chat_id, f"Heads up - {name} ${amount} charges in {days_until} days. Cancel now if you want to stop it.")

def daily_reminder(msg_text, days_of_week=None):
    now = datetime.datetime.now(est)
    if days_of_week and now.strftime("%A") not in days_of_week:
        return
    chat_id = get_last_chat_id()
    if chat_id:
        safe_send(chat_id, msg_text)

def reminder_7am():
    daily_reminder("Ginger shot time. Take it now. After this shot - work mode begins. No excuses.")

def reminder_730am():
    daily_reminder("Eat before you leave. Eggs or yogurt. In your mouth before you walk out the door.")

def reminder_8am_gym():
    daily_reminder("Time to go. Gym now. Lock in.", days_of_week=["Monday", "Tuesday", "Thursday", "Friday"])

def reminder_8am_rest():
    daily_reminder("Rest day. Walk first thing. That is your anchor. Come back ready to work.", days_of_week=["Wednesday", "Saturday", "Sunday"])

def reminder_1pm():
    daily_reminder("Lunch time Keem. You have food at home. Eat something real.")

def reminder_7pm():
    daily_reminder("Last call to eat tonight. Don't skip dinner.")

def reminder_945pm():
    chat_id = get_last_chat_id()
    if not chat_id:
        return
    try:
        logged_today = has_expense_today()
    except Exception:
        logged_today = False
    if logged_today:
        safe_send(chat_id, "Night ginger shot. Expenses already logged today - good work. Do your face routine, brush your teeth. Wind down begins now.")
    else:
        safe_send(chat_id, "Night ginger shot. Take it, do your face routine, brush your teeth. That is your signal - wind down begins now. Phone goes down after this.\n\nAlso: did you log your expenses today? Use /spent or send a receipt photo.")

def reminder_10pm():
    daily_reminder("Phone down. No screen. Read something. Tomorrow starts tonight.")

def reminder_11am_win():
    daily_reminder("Log a win - what went right today so far? Use /wins")

def reminder_3pm_win():
    daily_reminder("Quick one - anything worth logging as a win right now? Use /wins")

def reminder_8pm_win():
    daily_reminder("Before you wind down - log today's wins with /wins")

def reminder_830am_meal():
    daily_reminder("Log breakfast - don't forget your smoothie + Greek yogurt.")

def reminder_130pm_meal():
    daily_reminder("Log lunch.")

def reminder_730pm_meal():
    daily_reminder("Log dinner.")

def reminder_830am_tasks():
    chat_id = get_last_chat_id()
    if not chat_id:
        return
    tasks = get_unchecked_tasks()
    if tasks:
        top = tasks[:3]
        lines = "\n".join([f"  {i+1}. {t[1]}" for i, t in enumerate(top)])
        safe_send(chat_id, f"Top tasks for today:\n\n{lines}\n\nLock in. Get them done.")
    else:
        safe_send(chat_id, "No open tasks. Add something with /addtask and make the day count.")

HEALTH_PORT = int(os.environ.get("PORT", 8099))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"WEFT OS alive")

    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    print(f"Health check server running on port {HEALTH_PORT}")
    server.serve_forever()

def _utc(hour_et, minute_et=0):
    now_et = datetime.datetime.now(est)
    dt_et  = now_et.replace(hour=hour_et, minute=minute_et, second=0, microsecond=0)
    dt_utc = dt_et.astimezone(pytz.utc)
    shifted = dt_utc.date() > dt_et.date()
    return dt_utc.strftime("%H:%M"), shifted

def run_scheduler():
    tz_name = datetime.datetime.now(est).strftime("%Z")
    offset  = int(datetime.datetime.now(est).utcoffset().total_seconds() / 3600)

    t_7am,    _       = _utc(7,  0)
    t_730am,  _       = _utc(7, 30)
    t_8am,    _       = _utc(8,  0)
    t_830am,  _       = _utc(8, 30)
    t_9am,    _       = _utc(9,  0)
    t_1pm,    _       = _utc(13, 0)
    t_7pm,    _       = _utc(19, 0)
    t_945pm,  _       = _utc(21, 45)
    t_10pm,   _       = _utc(22, 0)
    t_11am,   _       = _utc(11, 0)
    t_3pm,    _       = _utc(15, 0)
    t_8pm,    _       = _utc(20, 0)
    t_830am_meal, _   = _utc(8, 30)
    t_130pm,  _       = _utc(13, 30)
    t_730pm,  _       = _utc(19, 30)
    t_mon8am, _       = _utc(8,  0)
    t_sat12,  _       = _utc(12, 0)
    t_sun15,  _       = _utc(15, 0)
    t_sun16,  _       = _utc(16, 0)
    t_reset,  shifted = _utc(21, 30)

    schedule.every().day.at(t_7am).do(reminder_7am)
    schedule.every().day.at(t_730am).do(reminder_730am)
    schedule.every().day.at(t_8am).do(reminder_8am_gym)
    schedule.every().day.at(t_8am).do(reminder_8am_rest)
    schedule.every().day.at(t_830am).do(reminder_830am_tasks)
    schedule.every().day.at(t_9am).do(check_subscriptions)
    schedule.every().day.at(t_1pm).do(reminder_1pm)
    schedule.every().day.at(t_7pm).do(reminder_7pm)
    schedule.every().day.at(t_945pm).do(reminder_945pm)
    schedule.every().day.at(t_10pm).do(reminder_10pm)

    schedule.every().day.at(t_11am).do(reminder_11am_win)
    schedule.every().day.at(t_3pm).do(reminder_3pm_win)
    schedule.every().day.at(t_8pm).do(reminder_8pm_win)

    schedule.every().day.at(t_830am_meal).do(reminder_830am_meal)
    schedule.every().day.at(t_130pm).do(reminder_130pm_meal)
    schedule.every().day.at(t_730pm).do(reminder_730pm_meal)

    schedule.every().monday.at(t_mon8am).do(send_weekly_email)
    schedule.every().saturday.at(t_sat12).do(ginger_batch_reminder)
    schedule.every().sunday.at(t_sun15).do(sunday_grocery_list)
    schedule.every().sunday.at(t_sun16).do(sunday_meal_prep_check)
    if shifted:
        schedule.every().monday.at(t_reset).do(sunday_reset_reminder)
    else:
        schedule.every().sunday.at(t_reset).do(sunday_reset_reminder)
    reset_day_label = "Mon" if shifted else "Sun"

    print(f"\nScheduler running - 19 jobs ({tz_name} = UTC{offset:+d})")
    print(f"  {t_7am} UTC  = 07:00 {tz_name}  Daily ginger shot")
    print(f"  {t_730am} UTC  = 07:30 {tz_name}  Eat breakfast")
    print(f"  {t_8am} UTC  = 08:00 {tz_name}  Gym / walk")
    print(f"  {t_830am} UTC  = 08:30 {tz_name}  Top 3 tasks")
    print(f"  {t_9am} UTC  = 09:00 {tz_name}  Subscription check")
    print(f"  {t_11am} UTC  = 11:00 {tz_name}  Log a win (morning)")
    print(f"  {t_1pm} UTC  = 13:00 {tz_name}  Lunch")
    print(f"  {t_3pm} UTC  = 15:00 {tz_name}  Log a win (afternoon)")
    print(f"  {t_7pm} UTC  = 19:00 {tz_name}  Dinner")
    print(f"  {t_8pm} UTC  = 20:00 {tz_name}  Log a win (evening)")
    print(f"  {t_945pm} UTC  = 21:45 {tz_name}  Night ginger shot + expense check")
    print(f"  {t_10pm} UTC  = 22:00 {tz_name}  Phone down")
    print(f"  {t_830am_meal} UTC  = 08:30 {tz_name}  Log breakfast")
    print(f"  {t_130pm} UTC  = 13:30 {tz_name}  Log lunch")
    print(f"  {t_730pm} UTC  = 19:30 {tz_name}  Log dinner")
    print(f"  Mon {t_mon8am} UTC  = Mon 08:00 {tz_name}  Weekly email")
    print(f"  Sat {t_sat12} UTC  = Sat 12:00 {tz_name}  Ginger batch")
    print(f"  Sun {t_sun15} UTC  = Sun 15:00 {tz_name}  Grocery list")
    print(f"  Sun {t_sun16} UTC  = Sun 16:00 {tz_name}  Meal prep check")
    print(f"  {reset_day_label} {t_reset} UTC  = Sun 21:30 {tz_name}  Sunday reset")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"Scheduler job error: {e}")
        time.sleep(30)

_poll_alive = threading.Event()

def watchdog():
    time.sleep(90)
    while True:
        time.sleep(60)
        try:
            bot.get_me()
        except Exception as e:
            print(f"[WATCHDOG] Telegram unreachable: {e} - forcing polling restart")
            try:
                bot.stop_polling()
            except Exception:
                pass

def scheduler_supervisor():
    while True:
        try:
            run_scheduler()
        except Exception as e:
            print(f"[SCHEDULER] Crashed: {e} - restarting in 10 s")
            time.sleep(10)

print("WEFT OS starting...")

health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()

scheduler_thread = threading.Thread(target=scheduler_supervisor, daemon=True)
scheduler_thread.start()

watchdog_thread = threading.Thread(target=watchdog, daemon=True)
watchdog_thread.start()

def send_startup_message():
    time.sleep(4)
    chat_id = get_last_chat_id()
    if chat_id:
        tz_name = datetime.datetime.now(est).strftime("%Z")
        now_et  = datetime.datetime.now(est).strftime("%I:%M %p")
        safe_send(chat_id,
            f"WEFT OS is live.\n"
            f"Timezone: Atlanta ({tz_name})\n"
            f"Current time: {now_et} ET\n"
            f"19 jobs scheduled. Watchdog active.\n"
            f"All reminders will fire at the correct Eastern Time."
        )

startup_msg_thread = threading.Thread(target=send_startup_message, daemon=True)
startup_msg_thread.start()

def start_polling():
    first_boot = True
    last_recovery_sent = 0
    recovery_cooldown = 600

    while True:
        try:
            if not first_boot:
                now = time.time()
                if now - last_recovery_sent >= recovery_cooldown:
                    print("[POLLING] Restarting after crash - sending recovery message")
                    chat_id = get_last_chat_id()
                    if chat_id:
                        safe_send(chat_id, "WEFT OS is back online.")
                    last_recovery_sent = now
                else:
                    print("[POLLING] Restarting (within cooldown - no message sent)")
            first_boot = False
            print("[POLLING] Started.")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"[POLLING] Error: {e} - retrying in 5 s")
            time.sleep(5)

start_polling()
