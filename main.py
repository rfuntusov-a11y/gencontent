import os
import time
import json
import sqlite3
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
import requests

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "")  # without @
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "") or f"https://t.me/{BOT_USERNAME}"
DB_PATH = os.getenv("DB_PATH", "bot.db")
FIRST_FREE = 1  # first request free (no ad)

app = FastAPI()

# --------- Database helpers ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        first_seen INTEGER,
        username TEXT,
        requests_count INTEGER DEFAULT 0,
        premium_until INTEGER DEFAULT 0,
        referrer_id INTEGER DEFAULT 0,
        referrals_count INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    conn.close()

def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, first_seen, username, requests_count, premium_until, referrer_id, referrals_count FROM users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    conn.close()
    return row

def ensure_user(uid, username="", ref=0):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if not get_user(uid):
        cur.execute("INSERT INTO users(user_id, first_seen, username, referrer_id) VALUES(?,?,?,?)", (uid, int(time.time()), username or "", ref))
        if ref:
            cur.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id=?", (ref,))
        conn.commit()
    conn.close()

def inc_request(uid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET requests_count = requests_count + 1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def set_premium(uid, days):
    until = int((datetime.utcnow() + timedelta(days=days)).timestamp())
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, uid))
    conn.commit()
    conn.close()
    return until

def revoke_premium(uid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET premium_until=0 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def is_premium(uid):
    row = get_user(uid)
    if not row: return False
    premium_until = row[4]
    return premium_until and premium_until > int(time.time())

# ---------- Telegram helpers ----------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(API_URL + "sendMessage", json=payload, timeout=15)
    return resp

def send_share_button(chat_id, uid):
    bot_link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
    keyboard = {
        "inline_keyboard": [
            [{"text":"Поделиться ботом", "url": bot_link}],
            [{"text":"Канал", "url": CHANNEL_LINK or f"https://t.me/{BOT_USERNAME}"}]
        ]
    }
    send_message(chat_id, "📤 Поделись с другом — ему понравится!", reply_markup=keyboard)

# ---------- Simple story generator ----------
import random
TEMPLATES = [
    "Это произошло ночью. {hero} и {other} вышли из подъезда — никто не ожидал, что всё закончится так. {detail}",
    "{hero} написал первое сообщение: «{line}». Ответ {other} был неожидан: «{reply}». Так началась цепочка.",
    "{hero} проснулся и вспомнил ту ночь, где {detail}. Он решил написать {other}: «{line}» — и его ждало удивление."
]

def gen_story(prompt):
    hero = "Ты"
    other = "он/она"
    parts = [p.strip() for p in prompt.split(",")]
    if len(parts) >= 2:
        hero = parts[0]
        other = parts[1]
    detail = random.choice(["всё пошло не по плану", "они смеялись до утра", "появилась неожиданная драка", "всё обернулось романом"])
    line = "нужно встретиться"
    reply = "ладно, приди"
    tmpl = random.choice(TEMPLATES)
    return tmpl.format(hero=hero, other=other, detail=detail, line=line, reply=reply)

# ---------- App startup ----------
@app.on_event("startup")
def startup():
    init_db()

# ---------- Webhook ----------
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    if "message" not in data:
        return {"ok": True}
    msg = data["message"]
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    user = msg.get("from", {})
    uid = user.get("id")
    username = user.get("username", "") or ""
    text = msg.get("text", "") or ""

    # handle /start with ref
    if text.startswith("/start"):
        parts = text.split()
        ref = 0
        if len(parts) > 1 and parts[1].startswith("ref"):
            try:
                ref = int(parts[1][3:])
            except:
                ref = 0
        ensure_user(uid, username, ref)
        send_message(chat_id, "🔥 Привет! Я генерирую истории и переписки. Первый запрос — бесплатно. Пиши тему.")
        send_share_button(chat_id, uid)
        return {"ok": True}

    ensure_user(uid, username, 0)

    # admin commands
    if text.startswith("/grant") and uid == ADMIN_ID:
        try:
            _, target, days = text.split()
            tid = int(target); days = int(days)
            set_premium(tid, days)
            send_message(chat_id, f"Выдал премиум на {days} дней юзеру {tid}.")
        except Exception as e:
            send_message(chat_id, "Ошибка. Использование: /grant <user_id> <days>")
        return {"ok": True}

    if text.startswith("/revoke") and uid == ADMIN_ID:
        try:
            _, target = text.split()
            tid = int(target)
            revoke_premium(tid)
            send_message(chat_id, f"Отозвал премиум у {tid}.")
        except:
            send_message(chat_id, "Ошибка. Использование: /revoke <user_id>")
        return {"ok": True}

    if text.startswith("/status"):
        row = get_user(uid)
        if not row:
            send_message(chat_id, "Нет данных о пользователе.")
            return {"ok": True}
        premium = is_premium(uid)
        rc = row[3]
        until = datetime.utcfromtimestamp(row[4]).isoformat() if row[4] else "—"
        send_message(chat_id, f"requests: {rc}\\npremium: {premium}\\npremium_until: {until}\\nreferrals: {row[6]}")
        return {"ok": True}

    if text.startswith("/premium"):
        msg = "Оформить Premium: "
        if PAYMENT_LINK:
            msg += f"<a href=\\\"{PAYMENT_LINK}\\\">Оплатить</a>\\n\\nПосле оплаты пришли админу или подожди верификацию."
        else:
            msg += "ссылка на оплату не задана."
        send_message(chat_id, msg)
        return {"ok": True}

    # generation logic
    row = get_user(uid)
    if not row:
        ensure_user(uid, username, 0)
        row = get_user(uid)
    requests_count = row[3]
    premium = is_premium(uid)

    story = gen_story(text or "короткая история")

    # first request free -> no ad
    if requests_count < FIRST_FREE or premium:
        inc_request(uid)
        send_message(chat_id, f"📖 <b>Твоя история</b>:\\n\\n{story}")
        send_share_button(chat_id, uid)
        return {"ok": True}
    else:
        inc_request(uid)
        ad_block = ("—\\nХотите без рекламы и длиннее истории? Оформите Premium.\\n"
                    "Команда: /premium")
        send_message(chat_id, f"📖 <b>Твоя история</b>:\\n\\n{story}\\n\\n{ad_block}")
        send_share_button(chat_id, uid)
        return {"ok": True}

@app.get("/")
def root():
    return {"ok": True}
