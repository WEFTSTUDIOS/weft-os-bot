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

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SPREADSHEET_ID = "1A9xUO-6pyn7z8_yadwkyXtyVXcuGbkvPR_IZp9wQ7lg"
REPORT_EMAIL = "byweftstudios@gmail.com"

# --- CONNECTIONS ---
# Google Sheets — all Sheets reads/writes
SHEETS_CONNECTION_ID = "ca_IiHAEZge9MFQ"
MAIN_CONNECTION_ID   = SHEETS_CONNECTION_ID  # kept for compatibility
# Google Drive — backup copies to WEFT Studios folder
DRIVE_CONNECTION_ID  = "ca_DtO3TQJSSycg"

# Gmail connections (verified email addresses via Composio API)
WEFT_GMAIL_ID       = "ca_jGjDU1VkI0nt"   # byweftstudios@gmail.com
KARIM_CONNECTION_ID = "ca_MPxmaIWiL6Kh"   # karimidrisofficial@gmail.com
OLD_CONNECTION_ID   = "ca_-8JPIJXZII1P"   # 2008karimidris@gmail.com
DAD_CONNECTION_ID   = "ca_R0AvyogLME_t"   # omarsudan007@gmail.com

_WEFT_STUDIOS_FOLDER_ID = None  # cached at first use

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

# --- HELPERS ---
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

# --- DRIVE BACKUP ---

def _get_weft_studios_folder_id():
    """Find and cache the WEFT Studios folder ID in Drive."""
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
    """Create a JSON log entry in the WEFT Studios Drive folder."""
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

# --- SHEETS WEBHOOK (Apps Script) — primary Sheets transport ---
# Set GOOGLE_SHEETS_WEBHOOK secret to the deployed Apps Script /exec URL.
# Falls back to Composio proxy if the secret is not set.

SHEETS_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK", "")

def _webhook_post(action, tab, **kwargs):
    """POST to the Apps Script webhook. Returns parsed JSON dict."""
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
        # Normalise to match the Composio proxy shape: {"data": {"values": [...]}}
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
    res = sheets_get("Tasks")
    unchecked = []
    if "data" in res and "values" in res["data"]:
        rows = res["data"]["values"]
        for i, row in enumerate(rows[1:], 1):
            status = row[2] if len(row) > 2 else ""
            task = row[1] if len(row) > 1 else ""
            if status != "Done" and task:
                unchecked.append((i, task))
    return unchecked

# --- FEATURE 1: PANTRY HELPERS ---

def get_pantry():
    res = sheets_get("Pantry", "A:C")
    items = {}
    row_map = {}
    if "data" in res and "values" in res["data"]:
        rows = res["data"]["values"]
        for i, row in enumerate(rows, 0):
            if len(row) >= 2 and row[0]:
                name = row[0].strip().lower()
                try:
                    qty = int(row[1])
                except:
                    qty = 0
                items[name] = qty
                row_map[name] = i + 1  # 1-indexed sheet row
    return items, row_map

def update_pantry_item(item_name, quantity, row_num=None):
    item_lower = item_name.strip().lower()
    items, row_map = get_pantry()
    date = datetime.datetime.now(est).strftime("%Y-%m-%d")
    if item_lower in row_map:
        rn = row_map[item_lower]
        updates = [
            {"range": f"Pantry!B{rn}", "values": [[str(quantity)]]},
            {"range": f"Pantry!C{rn}", "values": [[date]]}
        ]
        sheets_batch_update(updates)
    else:
        sheets_append("Pantry", [[item_name.strip(), str(quantity), date]])

def deduct_pantry_item(chat_id, item_name):
    item_lower = item_name.strip().lower()
    items, row_map = get_pantry()
    if item_lower not in items:
        return
    current = items[item_lower]
    new_qty = max(0, current - 1)
    date = datetime.datetime.now(est).strftime("%Y-%m-%d")
    rn = row_map[item_lower]
    updates = [
        {"range": f"Pantry!B{rn}", "values": [[str(new_qty)]]},
        {"range": f"Pantry!C{rn}", "values": [[date]]}
    ]
    sheets_batch_update(updates)
    if new_qty == 1:
        safe_send(chat_id, f"Low stock: {item_name} — 1 left. Pick some up soon.")
    elif new_qty == 0:
        safe_send(chat_id, f"{item_name} is out. Added to your grocery list.")

# --- FEATURE 1: RECEIPT PHOTO SCANNING ---

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    set_last_chat_id(message.chat.id)
    safe_send(message.chat.id, "Got your photo. Scanning receipt...")

    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        img_data = requests.get(file_url, timeout=15).content
        img_b64 = base64.standard_b64encode(img_data).decode("utf-8")

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-opus-4-5",
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
                            "This is a receipt photo. Extract and return ONLY a JSON object with these fields:\n"
                            "- store: store name (string)\n"
                            "- total: total amount as a number (no $ sign)\n"
                            "- category: one of food, clothing, supplies, tech, other\n"
                            "- food_items: array of objects with 'name' and 'quantity' for any food/grocery items found\n"
                            "Return only valid JSON, no explanation."
                        )
                    }
                ]
            }]
        )

        raw = resp.content[0].text.strip()
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            safe_send(message.chat.id, "Could not read the receipt. Try a clearer photo.")
            return

        data = json.loads(json_match.group())
        store = data.get("store", "Unknown store")
        total = data.get("total", 0)
        category = data.get("category", "other")
        food_items = data.get("food_items", [])

        date = datetime.datetime.now(est).strftime("%Y-%m-%d")
        sheets_append("Spent", [[date, store, category, str(total), "Receipt photo"]])

        added_items = []
        for fi in food_items:
            name = fi.get("name", "")
            qty = fi.get("quantity", 1)
            if name:
                update_pantry_item(name, qty)
                added_items.append(f"{name} x{qty}")

        reply = f"Logged — ${total} at {store} ({category})."
        if added_items:
            reply += f" Added to pantry: {', '.join(added_items)}."
        safe_send(message.chat.id, reply)

    except json.JSONDecodeError:
        safe_send(message.chat.id, "Scanned but could not parse the receipt. Try a clearer photo.")
    except Exception as e:
        print(f"Receipt scan error: {e}")
        safe_send(message.chat.id, f"Error scanning receipt: {e}")

# --- FEATURE 7: SMART PANTRY COMMANDS ---

@bot.message_handler(commands=['pantry'])
def pantry_handler(message):
    set_last_chat_id(message.chat.id)
    arg = message.text.replace('/pantry', '').strip().lower()
    items, _ = get_pantry()

    if not items:
        safe_send(message.chat.id, "Pantry is empty. Add items with /addpantry [item] [qty]")
        return

    if arg == 'low':
        low = {k: v for k, v in items.items() if v <= 1}
        if not low:
            safe_send(message.chat.id, "Nothing low. Pantry is stocked.")
        else:
            lines = [f"- {k}: {v} left" for k, v in sorted(low.items())]
            safe_send(message.chat.id, "LOW STOCK\n\n" + "\n".join(lines))
    else:
        lines = [f"- {k}: {v}" for k, v in sorted(items.items())]
        safe_send(message.chat.id, "PANTRY\n\n" + "\n".join(lines))

@bot.message_handler(commands=['addpantry'])
def add_pantry(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/addpantry', '').strip()
    parts = text.rsplit(' ', 1)
    if len(parts) == 2:
        item, qty_str = parts[0].strip(), parts[1].strip()
        try:
            qty = int(qty_str)
        except:
            item, qty = text, 1
    else:
        item, qty = text, 1

    if not item:
        safe_send(message.chat.id, "Try: /addpantry chicken breast 4")
        return

    update_pantry_item(item, qty)
    safe_send(message.chat.id, f"Pantry updated: {item} — {qty}")

@bot.message_handler(commands=['used'])
def used_item(message):
    set_last_chat_id(message.chat.id)
    item = message.text.replace('/used', '').strip()
    if not item:
        safe_send(message.chat.id, "Try: /used chicken breast")
        return
    deduct_pantry_item(message.chat.id, item)
    safe_send(message.chat.id, f"Deducted 1 from {item}.")

# --- EXISTING COMMANDS ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    set_last_chat_id(message.chat.id)
    help_text = (
        "WEFT OS COMMANDS\n\n"
        "FINANCIALS\n"
        "/log Depop $45 jeans - Log income\n"
        "/spent $12 chipotle - Log expense\n"
        "/sub Netflix $15 June24 - Log subscription\n"
        "/weekcheck - Weekly breakdown\n\n"
        "PRODUCTIVITY\n"
        "/morning - Daily briefing\n"
        "/focus [task] - Start focus mode\n"
        "/plan - Sunday planning session\n"
        "/tasks - List your tasks\n"
        "/addtask [task] - Add a task\n"
        "/done 1 3 4 - Mark tasks done\n"
        "/brain [thoughts] - Dump and organize\n"
        "/stuck - ADHD reset\n"
        "/hype - Get motivated\n"
        "/habit - Daily checklist\n"
        "/wins [win] - Log a win\n\n"
        "HEALTH\n"
        "/eat [meal] - Log food\n"
        "/fridge [item] [amount] - Update pantry\n"
        "/workout - Today's workout\n\n"
        "PANTRY\n"
        "/pantry - Show all pantry items\n"
        "/pantry low - Show low/out items\n"
        "/addpantry [item] [qty] - Add to pantry\n"
        "/used [item] - Deduct 1 from pantry\n\n"
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

@bot.message_handler(commands=['spent'])
def log_expense(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/spent', '').strip()

    if text.lower().startswith('cash left') or text.lower().startswith('account balance'):
        match = re.search(r'\$?(\d+(?:\.\d+)?)', text)
        if match:
            amount = match.group(1)
            date = datetime.datetime.now(est).strftime("%Y-%m-%d")
            label = "Cash balance" if 'cash' in text.lower() else "Account balance"
            sheets_append("Spent", [[date, "Balance", label, amount, "Daily balance update"]])
            safe_send(message.chat.id, f"Balance noted - ${amount} on {date}")
        return

    match = re.search(r'\$(\d+(?:\.\d+)?)\s+(.+)', text)
    if match:
        amount = match.group(1)
        item = match.group(2)
        date = datetime.datetime.now(est).strftime("%Y-%m-%d")
        res = sheets_append("Spent", [[date, "Expense", item, amount, "Logged via bot"]])
        if 'data' in res or 'error' not in res:
            safe_send(message.chat.id, f"Logged - ${amount} at {item} on {date}")
        else:
            safe_send(message.chat.id, f"Error logging expense: {res.get('error', 'unknown')}")
    else:
        safe_send(message.chat.id, "Try: /spent $12 chipotle\nOr: /spent Cash left $47")

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

@bot.message_handler(commands=['eat'])
def log_food(message):
    set_last_chat_id(message.chat.id)
    meal = message.text.replace('/eat', '').strip()
    if not meal:
        safe_send(message.chat.id, "What did you eat? Try: /eat Chicken and rice")
        return
    date = datetime.datetime.now(est).strftime("%Y-%m-%d")
    time_str = datetime.datetime.now(est).strftime("%H:%M")
    sheets_append("Food Log", [[date, time_str, meal, "", "Logged via bot"]])
    safe_send(message.chat.id, f"Meal logged: {meal} at {time_str}")
    # Deduct from pantry for primary ingredient (first word match)
    first_word = meal.split()[0].lower() if meal.split() else ""
    if first_word:
        deduct_pantry_item(message.chat.id, first_word)

@bot.message_handler(commands=['fridge'])
def fridge_handler(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/fridge', '').strip()

    if text.lower() == 'check' or text == '':
        res = sheets_get("Pantry", "A:C")
        if "data" in res and "values" in res["data"]:
            rows = res["data"]["values"]
            if len(rows) > 1:
                items = []
                for row in rows[1:]:
                    if len(row) >= 2:
                        items.append(f"- {row[0]}: {row[1]}")
                safe_send(message.chat.id, "Your pantry:\n" + "\n".join(items) if items else "Pantry is empty!")
            else:
                safe_send(message.chat.id, "Pantry is empty!")
        else:
            safe_send(message.chat.id, "Error checking pantry.")
        return

    date = datetime.datetime.now(est).strftime("%Y-%m-%d")
    parts = text.rsplit(' ', 1)
    if len(parts) == 2:
        item, amount = parts[0], parts[1]
    else:
        item, amount = text, "1"

    sheets_append("Pantry", [[item, amount, date]])
    safe_send(message.chat.id, f"Pantry updated: {item} - {amount}")

@bot.message_handler(commands=['addtask'])
def add_task(message):
    set_last_chat_id(message.chat.id)
    task_name = message.text.replace('/addtask', '').strip()
    if not task_name:
        safe_send(message.chat.id, "Try: /addtask Finish the mockup")
        return
    date = datetime.datetime.now(est).strftime("%Y-%m-%d")
    sheets_append("Tasks", [[date, task_name, "Pending", "", "FALSE"]])
    safe_send(message.chat.id, f"Added: {task_name}. Get it done.")

@bot.message_handler(commands=['tasks'])
def list_tasks(message):
    set_last_chat_id(message.chat.id)
    today_str = datetime.datetime.now(est).strftime("%Y-%m-%d")
    res = sheets_get("Tasks")

    rows = res.get("data", {}).get("values", []) if "data" in res else []
    if not rows or len(rows) <= 1:
        safe_send(message.chat.id, "No tasks yet. Add one with /addtask")
        return

    today_tasks = []
    carried = []
    done_tasks = []

    for i, row in enumerate(rows[1:], 1):
        date_added = row[0] if len(row) > 0 else ""
        task = row[1] if len(row) > 1 else ""
        status = row[2] if len(row) > 2 else ""
        carried_over = row[4] if len(row) > 4 else ""

        if not task:
            continue
        if status == "Done":
            done_tasks.append(f"  {i}. {task}")
        elif date_added == today_str:
            today_tasks.append(f"  {i}. {task}")
        elif carried_over == "TRUE":
            carried.append(f"  {i}. {task}")

    response = f"TODAY'S TASKS - {today_str}\n\n"
    if today_tasks:
        response += "Pending:\n" + "\n".join(today_tasks) + "\n"
    if carried:
        response += "\nCarried over:\n" + "\n".join(carried) + "\n"
    if done_tasks:
        response += "\nCompleted today:\n" + "\n".join(done_tasks) + "\n"
    if not today_tasks and not carried and not done_tasks:
        response += "No tasks. Add one with /addtask"

    safe_send(message.chat.id, response)

@bot.message_handler(commands=['done'])
def mark_done(message):
    set_last_chat_id(message.chat.id)
    text = message.text.replace('/done', '').strip()
    try:
        nums = [int(n) for n in text.split()]
    except ValueError:
        safe_send(message.chat.id, "Try: /done 1 3 4")
        return

    res = sheets_get("Tasks")
    if "data" not in res or "values" not in res.get("data", {}):
        safe_send(message.chat.id, "Error retrieving tasks.")
        return

    rows = res["data"]["values"]
    today_str = datetime.datetime.now(est).strftime("%Y-%m-%d")
    updates = []

    for num in nums:
        if 1 <= num < len(rows):
            row_index = num + 1
            updates.append({"range": f"Tasks!C{row_index}", "values": [["Done"]]})
            updates.append({"range": f"Tasks!D{row_index}", "values": [[today_str]]})

    if updates:
        sheets_batch_update(updates)

        remaining = []
        for i, row in enumerate(rows[1:], 1):
            status = row[2] if len(row) > 2 else ""
            task = row[1] if len(row) > 1 else ""
            if status != "Done" and i not in nums and task:
                remaining.append(f"  {i}. {task}")

        response = f"Checked off {', '.join(map(str, nums))} - lets go Keem.\n\n"
        if remaining:
            response += f"{len(remaining)} left:\n" + "\n".join(remaining) + "\n\nKeep pushing."
        else:
            response += "All done! Great work today."

        safe_send(message.chat.id, response)
    else:
        safe_send(message.chat.id, "No valid task numbers found.")

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
    unchecked = get_unchecked_tasks()
    if unchecked:
        user_state[message.chat.id] = {
            'state': 'reviewing_tasks',
            'tasks': unchecked,
            'index': 0
        }
        task_list = "\n".join([f"- {t[1]}" for t in unchecked])
        safe_send(message.chat.id, f"You did not check off these tasks:\n\n{task_list}\n\nWhy did you not complete '{unchecked[0][1]}'?")
    else:
        user_state[message.chat.id] = {'state': 'sunday_q1'}
        safe_send(message.chat.id, "SUNDAY PLANNING SESSION\n\nQ1: What did you finish last week?")

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.chat.id), dict))
def handle_state(message):
    set_last_chat_id(message.chat.id)
    state = user_state.get(message.chat.id, {})
    current = state.get('state')

    if current == 'reviewing_tasks':
        tasks = state['tasks']
        idx = state['index'] + 1
        if idx < len(tasks):
            state['index'] = idx
            safe_send(message.chat.id, f"Why did you not complete '{tasks[idx][1]}'?")
        else:
            user_state[message.chat.id] = {'state': 'sunday_q1'}
            safe_send(message.chat.id, "Q1: What did you finish last week?")

    elif current == 'sunday_q1':
        user_state[message.chat.id] = {'state': 'sunday_q2', 'q1': message.text}
        safe_send(message.chat.id, "Q2: What did not get done?")

    elif current == 'sunday_q2':
        user_state[message.chat.id] = {'state': 'sunday_q3', 'q1': state.get('q1'), 'q2': message.text}
        safe_send(message.chat.id, "Q3: What is your number one priority this week?")

    elif current == 'sunday_q3':
        user_state[message.chat.id] = {'state': 'sunday_q4', 'q1': state.get('q1'), 'q2': state.get('q2'), 'q3': message.text}
        safe_send(message.chat.id, "Q4: What content are you posting this week?")

    elif current == 'sunday_q4':
        user_state[message.chat.id] = {'state': 'sunday_q5', 'q1': state.get('q1'), 'q2': state.get('q2'), 'q3': state.get('q3'), 'q4': message.text}
        safe_send(message.chat.id, "Q5: Any events, appointments, or deadlines this week?")

    elif current == 'sunday_q5':
        q1 = state.get('q1', '')
        q2 = state.get('q2', '')
        q3 = state.get('q3', '')
        q4 = state.get('q4', '')
        q5 = message.text

        date = datetime.datetime.now(est).strftime("%Y-%m-%d")
        sheets_append("Planning", [[date, q1, q2, q3, q4]])

        summary = (
            "WEEK LOCKED IN\n\n"
            f"Finished last week: {q1}\n"
            f"Did not finish: {q2}\n"
            f"Priority 1: {q3}\n"
            f"Content: {q4}\n"
            f"Deadlines: {q5}\n\n"
            "Go make it happen. This week counts."
        )
        safe_send(message.chat.id, summary)
        del user_state[message.chat.id]

# --- /emails COMMAND — MULTI-INBOX (4 accounts) ---

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
    """Fetch top 3 important emails, filtered. Returns list of formatted strings."""
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
    """Fetch only truly important emails for Dad, summarised in plain English."""
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
        # Summarise with Claude in one plain sentence
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

    weft_emails   = fetch_top_emails(WEFT_GMAIL_ID)         # byweftstudios
    karim_emails  = fetch_top_emails(KARIM_CONNECTION_ID)   # karimidrisofficial
    old_emails    = fetch_top_emails(OLD_CONNECTION_ID)     # 2008karimidris
    dad_emails    = fetch_dad_emails(DAD_CONNECTION_ID)     # omarsudan007

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

# --- FEATURE 2: WEEKLY EMAIL REPORT ---

def send_weekly_email():
    chat_id = get_last_chat_id()
    now = datetime.datetime.now(est)
    week_start = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    today_str = now.strftime("%Y-%m-%d")

    # Income
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

    # Expenses
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

    # Tasks
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

    # Wins / Food days logged this week
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

    # Days until launch (placeholder — set your own target date)
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
        f"Carried over: {len(carried_tasks)} — {carried_lines}\n\n"
        f"HABITS\n"
        f"Wins logged: {wins_count} days\n"
        f"Food logged: {len(food_days)} days\n\n"
        f"Drop 001 launches in {days_left} days. Keep going Keem."
    )

    subject = f"WEFT Weekly Report — {today_str}"

    # Send via Gmail through Composio
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

# --- FEATURE 3: SUNDAY RESET REMINDER ---

def sunday_reset_reminder():
    chat_id = get_last_chat_id()
    if chat_id:
        safe_send(chat_id, "Time for your Sunday reset. Run /plan now — build your week before you sleep. Tomorrow starts tonight.")

# --- FEATURE 4: GINGER SHOT BATCH REMINDER ---

def ginger_batch_reminder():
    chat_id = get_last_chat_id()
    if chat_id:
        safe_send(chat_id, "Make your ginger shot batch for the week. Blend fresh ginger, lemon, water. Store in the fridge. Costs nothing and hits different.")

# --- FEATURE 1: SUNDAY GROCERY LIST ---

def sunday_grocery_list():
    chat_id = get_last_chat_id()
    if not chat_id:
        return
    items, _ = get_pantry()
    low = [f"- {k} ({v} left)" for k, v in sorted(items.items()) if v <= 1]
    if low:
        msg = "Grocery list for this week:\n\n" + "\n".join(low) + "\n\nPick these up before Sunday ends."
    else:
        msg = "Pantry looks good — nothing critically low this week."
    safe_send(chat_id, msg)

# --- FEATURE 1: SUNDAY MEAL PREP REMINDER ---

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

# --- FEATURE 6: SUBSCRIPTION RENEWAL ALERTS ---

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
        # Try to parse renewal date (format like "June24" or "2025-06-24" or "Jun 24")
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
                safe_send(chat_id, f"Heads up — {name} ${amount} charges in {days_until} days. Cancel now if you want to stop it.")

# --- FEATURE 5: DAILY REMINDERS ---

def daily_reminder(msg_text, days_of_week=None):
    now = datetime.datetime.now(est)
    if days_of_week and now.strftime("%A") not in days_of_week:
        return
    chat_id = get_last_chat_id()
    if chat_id:
        safe_send(chat_id, msg_text)

def reminder_7am():
    daily_reminder("Ginger shot time. Take it now. After this shot — work mode begins. No excuses.")

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
    daily_reminder("Night ginger shot. Take it, do your face routine, brush your teeth. That is your signal — wind down begins now. Phone goes down after this.")

def reminder_10pm():
    daily_reminder("Phone down. No screen. Read something. Tomorrow starts tonight.")

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

# --- PART 4: HEALTH CHECK SERVER ---

# In a Reserved VM deployment Replit injects PORT; fall back to 8099 for dev
HEALTH_PORT = int(os.environ.get("PORT", 8099))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"WEFT OS alive")

    def log_message(self, format, *args):
        pass  # suppress HTTP access logs

def run_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    print(f"Health check server running on port {HEALTH_PORT}")
    server.serve_forever()

# --- SCHEDULER SETUP ---

def _utc(hour_et, minute_et=0):
    """Convert an Eastern Time to a UTC HH:MM string. Returns (time_str, day_shifted)."""
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
    t_mon8am, _       = _utc(8,  0)
    t_sat12,  _       = _utc(12, 0)
    t_sun15,  _       = _utc(15, 0)
    t_sun16,  _       = _utc(16, 0)
    t_reset,  shifted = _utc(21, 30)

    # Daily reminders — scheduled in UTC
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

    # Weekly
    schedule.every().monday.at(t_mon8am).do(send_weekly_email)
    schedule.every().saturday.at(t_sat12).do(ginger_batch_reminder)
    schedule.every().sunday.at(t_sun15).do(sunday_grocery_list)
    schedule.every().sunday.at(t_sun16).do(sunday_meal_prep_check)
    # Sunday 9:30 PM ET can cross into Monday UTC — schedule on the correct UTC day
    if shifted:
        schedule.every().monday.at(t_reset).do(sunday_reset_reminder)
    else:
        schedule.every().sunday.at(t_reset).do(sunday_reset_reminder)
    reset_day_label = "Mon" if shifted else "Sun"

    print(f"\nScheduler running — 13 jobs ({tz_name} = UTC{offset:+d})")
    print(f"  {t_7am} UTC  = 07:00 {tz_name}  Daily ginger shot")
    print(f"  {t_730am} UTC  = 07:30 {tz_name}  Eat breakfast")
    print(f"  {t_8am} UTC  = 08:00 {tz_name}  Gym / walk")
    print(f"  {t_830am} UTC  = 08:30 {tz_name}  Top 3 tasks")
    print(f"  {t_9am} UTC  = 09:00 {tz_name}  Subscription check")
    print(f"  {t_1pm} UTC  = 13:00 {tz_name}  Lunch")
    print(f"  {t_7pm} UTC  = 19:00 {tz_name}  Dinner")
    print(f"  {t_945pm} UTC  = 21:45 {tz_name}  Night ginger shot")
    print(f"  {t_10pm} UTC  = 22:00 {tz_name}  Phone down")
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

# --- WATCHDOG ---

_poll_alive = threading.Event()

def watchdog():
    """Ping Telegram every 60 s. If unreachable, force a polling restart."""
    time.sleep(90)
    while True:
        time.sleep(60)
        try:
            bot.get_me()
        except Exception as e:
            print(f"[WATCHDOG] Telegram unreachable: {e} — forcing polling restart")
            try:
                bot.stop_polling()
            except Exception:
                pass

def scheduler_supervisor():
    """Run the scheduler in a loop — restart it if it ever crashes."""
    while True:
        try:
            run_scheduler()
        except Exception as e:
            print(f"[SCHEDULER] Crashed: {e} — restarting in 10 s")
            time.sleep(10)

# --- START ---
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
            f"13 jobs scheduled. Watchdog active.\n"
            f"All reminders will fire at the correct Eastern Time."
        )

startup_msg_thread = threading.Thread(target=send_startup_message, daemon=True)
startup_msg_thread.start()

def start_polling():
    first_boot = True
    last_recovery_sent = 0  # epoch seconds; 0 = never sent
    recovery_cooldown = 600  # 10 minutes

    while True:
        try:
            if not first_boot:
                now = time.time()
                if now - last_recovery_sent >= recovery_cooldown:
                    print("[POLLING] Restarting after crash — sending recovery message")
                    chat_id = get_last_chat_id()
                    if chat_id:
                        safe_send(chat_id, "WEFT OS is back online.")
                    last_recovery_sent = now
                else:
                    print("[POLLING] Restarting (within cooldown — no message sent)")
            first_boot = False
            print("[POLLING] Started.")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"[POLLING] Error: {e} — retrying in 5 s")
            time.sleep(5)

start_polling()
