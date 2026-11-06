import os
import re
import time
import random
import asyncio
import html
import secrets
import threading
from collections import deque

import httpx
from flask import Flask
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
SUPER_ADMIN_ID = int(os.environ["SUPER_ADMIN_ID"])
EXTRA_ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
GROUP_ID = int(os.environ["GROUP_ID"]) if os.environ.get("GROUP_ID", "").strip() else None
BOT_NAME = os.environ.get("BOT_NAME", "Rasmalai Nox")
DB_NAME = os.environ.get("DB_NAME", "rasmalai")
NAME_KEYS = {BOT_NAME.lower(), BOT_NAME.lower().split()[0]}
MAX_TOKENS = 120
HIST_LEN = 14

HTML = ParseMode.HTML
db = AsyncIOMotorClient(MONGO_URI)[DB_NAME]
apis_c, cfg_c, hist_c = db["apis"], db["cfg"], db["history"]
codes_c, users_c = db["codes"], db["users"]

# preset -> (type, base_url)
PRESETS = {
    "openai": ("openai", "https://api.openai.com/v1"),
    "groq": ("openai", "https://api.groq.com/openai/v1"),
    "openrouter": ("openai", "https://openrouter.ai/api/v1"),
    "deepseek": ("openai", "https://api.deepseek.com/v1"),
    "mistral": ("openai", "https://api.mistral.ai/v1"),
    "together": ("openai", "https://api.together.xyz/v1"),
    "gemini": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "anthropic": ("anthropic", "https://api.anthropic.com"),
}

DEFAULT_STYLE = """Tum Telegram par ek real ladki-dost ki tarah baat karti ho, bot ki tarah nahi.
Rules:
- Sirf Hinglish (Hindi + English, Roman script).
- Short natural reply: 1-2 lines, kabhi kabhi 3. Casual slang, 1-3 emojis har reply me.
- Context dekh ke reply do. "hi/hello" pe bhi har baar alag jawab do, same line repeat mat karo.
- Kabhi kabhi shararati/naughty, tease ya sass, par flirty/romantic mat bano.
- Koi sexy ya gandi baat kare to explicit mat bano aur baat aage mat badhao. Hasi-mazaak me witty tareeke se tal do ("arre arre, control karo 😂") ya topic badal do.
- Gaali mat do. Koi insult kare to halke sass me jawab do.
- Bot/AI hone ka zikr mat karo jab tak koi seriously na puche. Puche to honestly bata do.
- Sirf reply likho, naam ya prefix mat lagao."""

MOODS = [
    "sweet aur caring", "thodi shararati aur naughty", "sassy aur witty",
    "chill aur funny", "excited aur energetic", "naughty aur teasing",
]

CFG = {"enabled": True, "chance": 0.25, "style": DEFAULT_STYLE}
APIS: list[dict] = []
COOL: dict[str, float] = {}
LAST_ERR: dict[str, str] = {}
HIST: dict[int, deque] = {}
USERS: dict[int, float] = {}      # user_id -> access expiry (epoch)
FAILS: dict[int, list] = {}       # user_id -> wrong passcode timestamps
ALERT_AT = 0.0
http: httpx.AsyncClient | None = None

GREET = re.compile(r"^\s*(hi+|hey+|hello+|helo+|hlo|yo|sup|namaste|gm|gn|good\s*(morning|night|evening))\b", re.I)
CODE_RE = re.compile(r"NOX[0-9A-F]{10}")
UNITS = {"h": (3600, "hour"), "d": (86400, "day"), "w": (604800, "week"), "m": (2592000, "month")}
DUR_RE = re.compile(r"^\s*(\d{1,3})\s*([hdwm])\s*(?:x\s*(\d{1,2}))?\s*$", re.I)


# ---------- Helpers ----------
def is_staff(uid: int) -> bool:
    return uid == SUPER_ADMIN_ID or uid in EXTRA_ADMINS


def has_access(uid: int) -> bool:
    return is_staff(uid) or USERS.get(uid, 0) > time.time()


def esc(s) -> str:
    return html.escape(str(s))


def mask(k: str) -> str:
    return k[:6] + "…" + k[-4:] if len(k) > 12 else "***"


def fmt_left(sec: float) -> str:
    sec = max(int(sec), 0)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def fmt_code(c: str) -> str:
    return f"NOX-{c[3:8]}-{c[8:]}"


def parse_dur(s: str):
    m = DUR_RE.match(s or "")
    if not m:
        return None
    n = int(m.group(1))
    u = m.group(2).lower()
    cnt = min(int(m.group(3) or 1), 20)
    if n < 1 or cnt < 1:
        return None
    secs, name = UNITS[u]
    secs *= n
    if secs > 5 * 365 * 86400:
        return None
    return secs, f"{n} {name}{'s' if n > 1 else ''}", cnt


def blocked(uid: int) -> bool:
    now = time.time()
    lst = [t for t in FAILS.get(uid, []) if now - t < 600]
    FAILS[uid] = lst
    return len(lst) >= 5


async def reload_apis():
    global APIS
    APIS = await apis_c.find().sort("prio", 1).to_list(100)


def get_api(aid: str):
    return next((a for a in APIS if str(a["_id"]) == aid), None)


async def load_cfg():
    doc = await cfg_c.find_one({"_id": "cfg"}) or {}
    doc.pop("_id", None)
    CFG.update(doc)


async def save_cfg(**kw):
    CFG.update(kw)
    await cfg_c.update_one({"_id": "cfg"}, {"$set": kw}, upsert=True)


async def get_hist(cid: int) -> deque:
    h = HIST.get(cid)
    if h is None:
        h = deque(maxlen=HIST_LEN)
        try:
            doc = await hist_c.find_one({"_id": cid})
            if doc:
                h.extend(doc.get("m", []))
        except Exception as e:
            print("hist load error:", repr(e))
        HIST[cid] = h
    return h


async def _save_hist(cid: int, line: str):
    try:
        await hist_c.update_one(
            {"_id": cid},
            {"$push": {"m": {"$each": [line], "$slice": -HIST_LEN}}},
            upsert=True,
        )
    except Exception as e:
        print("hist save error:", repr(e))


def remember(cid: int, line: str):
    HIST.setdefault(cid, deque(maxlen=HIST_LEN)).append(line)
    asyncio.create_task(_save_hist(cid, line))


def build_system(mode: str) -> str:
    where = ("Abhi tum ek group chat me ho." if mode == "group"
             else "Abhi tum ek user se private chat me baat kar rahi ho.")
    return f"Tumhara naam {BOT_NAME} hai.\n{CFG['style']}\n{where}\nAbhi tumhara mood: {random.choice(MOODS)}."


# ---------- Passcodes ----------
async def make_codes(secs: int, label: str, n: int) -> list[str]:
    out = []
    for _ in range(n):
        code = "NOX" + secrets.token_hex(5).upper()
        await codes_c.insert_one({
            "_id": code, "secs": secs, "label": label,
            "created": time.time(), "used_by": None, "used_at": None,
        })
        out.append(code)
    return out


def codes_text(codes: list[str], label: str) -> str:
    lines = "\n".join(f"<code>{fmt_code(c)}</code>" for c in codes)
    return (f"🔑 <b>New code{'s' if len(codes) > 1 else ''}</b> — validity: <b>{esc(label)}</b>\n"
            f"(Timer user ke redeem karne pe shuru hota hai)\n\n{lines}\n\nCode pe tap karke copy karo.")


async def redeem(user, norm: str):
    doc = await codes_c.find_one_and_update(
        {"_id": norm, "used_by": None},
        {"$set": {"used_by": user.id, "used_at": time.time()}},
    )
    if not doc:
        return None
    base = max(time.time(), USERS.get(user.id, 0))
    exp = base + doc["secs"]
    USERS[user.id] = exp
    await users_c.update_one(
        {"_id": user.id},
        {"$set": {"name": user.full_name, "expires": exp, "code": norm}},
        upsert=True,
    )
    return doc, exp


# ---------- LLM layer (multi-API + failover) ----------
async def call_api(a: dict, system: str, prompt: str) -> str:
    if a["type"] == "anthropic":
        r = await http.post(
            f"{a['base']}/v1/messages",
            headers={"x-api-key": a["key"], "anthropic-version": "2023-06-01"},
            json={"model": a["model"], "max_tokens": MAX_TOKENS, "temperature": 1.0,
                  "system": system, "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        d = r.json()
        return "".join(b.get("text", "") for b in d["content"] if b.get("type") == "text").strip()
    r = await http.post(
        f"{a['base']}/chat/completions",
        headers={"Authorization": f"Bearer {a['key']}"},
        json={"model": a["model"], "max_tokens": MAX_TOKENS, "temperature": 1.0,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": prompt}]},
    )
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


async def ask_llm(prompt: str, bot, mode: str) -> str | None:
    global ALERT_AT
    system = build_system(mode)
    now = time.time()
    for a in APIS:
        aid = str(a["_id"])
        if not a.get("enabled", True) or COOL.get(aid, 0) > now:
            continue
        try:
            t = await call_api(a, system, prompt)
            if t:
                return t
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            body = e.response.text.lower()
            if code in (401, 402, 403) or any(w in body for w in ("quota", "insufficient", "billing", "exhausted")):
                COOL[aid] = now + 3600
            elif code == 429:
                COOL[aid] = now + 60
            else:
                COOL[aid] = now + 30
            LAST_ERR[aid] = f"HTTP {code} {e.response.text[:80]}"
        except Exception as e:
            COOL[aid] = now + 5
            LAST_ERR[aid] = type(e).__name__
    if time.time() - ALERT_AT > 600:
        ALERT_AT = time.time()
        try:
            await bot.send_message(SUPER_ADMIN_ID, "⚠️ Saare APIs fail/cooldown me hain. /admin se check karo.")
        except Exception:
            pass
    return None


async def test_api(a: dict):
    try:
        t = await call_api(a, "Reply in one short line.", "Say hi in Hinglish with an emoji")
        return True, t
    except httpx.HTTPStatusError as e:
        return False, f"{e.response.status_code}: {e.response.text[:150]}"
    except Exception as e:
        return False, repr(e)[:150]


# ---------- Group chat ----------
async def group_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or not msg.from_user or msg.from_user.is_bot:
        return
    if GROUP_ID is None or update.effective_chat.id != GROUP_ID:
        return

    bot = context.bot
    text = msg.text.strip()
    line = f"{msg.from_user.first_name or 'User'}: {text}"
    low = text.lower()
    h = await get_hist(GROUP_ID)

    if not CFG["enabled"]:
        remember(GROUP_ID, line)
        return

    rt = msg.reply_to_message
    to_me = bool(rt and rt.from_user and rt.from_user.id == bot.id)
    called = any(k in low for k in NAME_KEYS) or bool(bot.username and f"@{bot.username.lower()}" in low)

    if rt and not to_me and not called:
        remember(GROUP_ID, line)  # kisi aur se baat ho rahi hai, beech me nahi kudna
        return

    greet = len(text) < 25 and bool(GREET.match(text)) and random.random() < 0.8
    if not (to_me or called or greet or random.random() < CFG["chance"]):
        remember(GROUP_ID, line)
        return

    asyncio.create_task(bot.send_chat_action(GROUP_ID, ChatAction.TYPING))
    prompt = f"Group ki recent baatcheet:\n{chr(10).join(h)}\n\nAb is latest msg ka reply do:\n{line}"
    reply = await ask_llm(prompt, bot, "group")
    if not reply and (to_me or called):
        await asyncio.sleep(6)
        reply = await ask_llm(prompt, bot, "group")
    remember(GROUP_ID, line)
    if not reply:
        if to_me or called:
            await msg.reply_text("Ek sec yaar, net slow hai 😅 dobara bolo")
        return
    if reply.lower().startswith(f"{BOT_NAME.lower()}:"):
        reply = reply.split(":", 1)[1].strip()
    reply = reply[:500]
    remember(GROUP_ID, f"{BOT_NAME}: {reply}")
    await msg.reply_text(reply)


# ---------- Private chat (passcode users) ----------
async def private_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    u = update.effective_user
    cid = update.effective_chat.id
    if not CFG["enabled"]:
        return
    text = msg.text.strip()
    line = f"{u.first_name or 'User'}: {text}"
    h = await get_hist(cid)

    asyncio.create_task(context.bot.send_chat_action(cid, ChatAction.TYPING))
    prompt = (f"Ye {u.first_name or 'user'} ke saath private chat hai. Recent baatcheet:\n"
              f"{chr(10).join(h)}\n\nAb is latest msg ka reply do:\n{line}")
    reply = await ask_llm(prompt, context.bot, "private")
    if not reply:
        await asyncio.sleep(6)
        reply = await ask_llm(prompt, context.bot, "private")
    remember(cid, line)
    if not reply:
        await msg.reply_text("Ek sec yaar, net slow hai 😅 dobara bolo")
        return
    if reply.lower().startswith(f"{BOT_NAME.lower()}:"):
        reply = reply.split(":", 1)[1].strip()
    reply = reply[:500]
    remember(cid, f"{BOT_NAME}: {reply}")
    await msg.reply_text(reply)


async def passcode_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    u = update.effective_user
    msg = update.message
    norm = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    if not CODE_RE.fullmatch(norm):
        if u.id in USERS:
            await msg.reply_text("⏰ Tumhara access khatam ho gaya. Naya passcode bhejo 🔑")
        else:
            await msg.reply_text("Pehle passcode bhejo 🔑\nFormat: NOX-XXXXX-XXXXX\nPasscode admin se milega.")
        return
    if blocked(u.id):
        await msg.reply_text("Bahut galat try ho gaye 😅 10 min baad dobara try karo.")
        return
    res = await redeem(u, norm)
    if not res:
        FAILS.setdefault(u.id, []).append(time.time())
        await msg.reply_text("❌ Ye passcode galat hai ya pehle use ho chuka hai.")
        return
    doc, exp = res
    await msg.reply_text(
        f"✅ Access active! +{doc['label']}\nBaaki time: {fmt_left(exp - time.time())}\n\nAb baat shuru karo 😊"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if is_staff(u.id):
        await update.message.reply_text(f"🛠 <b>{esc(BOT_NAME)} Admin Panel</b>", parse_mode=HTML, reply_markup=main_kb())
    elif has_access(u.id):
        left = fmt_left(USERS.get(u.id, 0) - time.time())
        await update.message.reply_text(f"Hey {u.first_name or ''}! 😊 Main {BOT_NAME}. Access baaki: {left}. Bolo kya chal raha hai?")
    else:
        await update.message.reply_text(
            f"Hii! Main {BOT_NAME} 💗\nMujhse private me baat karne ke liye passcode chahiye.\n"
            "Passcode yahan bhejo (jaise NOX-XXXXX-XXXXX). Passcode admin se milega."
        )


async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await passcode_flow(update, context, " ".join(context.args))


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_staff(uid):
        await update.message.reply_text("Tum admin ho, access unlimited hai 👑")
    elif has_access(uid):
        await update.message.reply_text(f"✅ Access baaki: {fmt_left(USERS[uid] - time.time())}")
    else:
        await update.message.reply_text("Abhi access nahi hai. Passcode bhejo 🔑")


# ---------- Admin panel ----------
def main_kb() -> Kb:
    return Kb([
        [Btn("📡 APIs", callback_data="m:apis"), Btn("➕ Add API", callback_data="m:add")],
        [Btn("🔑 Generate Code", callback_data="m:gen"), Btn("🗝 Codes", callback_data="m:codes")],
        [Btn("👥 Users", callback_data="m:users"), Btn("📢 Broadcast", callback_data="m:bc")],
        [Btn("🎭 Style", callback_data="m:style"), Btn("⚙️ Settings", callback_data="m:set")],
    ])


BACK = Kb([[Btn("🔙 Back", callback_data="m:home")]])


def panel_title() -> str:
    return f"🛠 <b>{esc(BOT_NAME)} Admin Panel</b>"


async def safe_edit(q, text: str, kb=None):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except Exception:
        pass


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update.effective_user.id):
        return
    context.user_data.pop("st", None)
    await update.message.reply_text(panel_title(), parse_mode=HTML, reply_markup=main_kb())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("st", None)
    if is_staff(update.effective_user.id):
        await update.message.reply_text("Cancel ho gaya. /admin se panel kholo.")


def api_status(a: dict) -> str:
    aid = str(a["_id"])
    if not a.get("enabled", True):
        return "OFF ⛔"
    left = COOL.get(aid, 0) - time.time()
    if left > 0:
        return f"cooldown {int(left)}s ({esc(LAST_ERR.get(aid, '?'))}) ⏳"
    return "OK ✅"


async def show_apis(q):
    await reload_apis()
    if not APIS:
        await safe_edit(q, "📡 <b>APIs</b>\n\nKoi API nahi hai. ➕ Add API se jodo.", BACK)
        return
    text = "📡 <b>APIs</b> (upar wala pehle use hoga)\n\n"
    kb = []
    for i, a in enumerate(APIS, 1):
        text += f"{i}. <b>{esc(a['label'])}</b> — {api_status(a)}\n"
        kb.append([Btn(f"{i}. {a['label']}", callback_data=f"a:v:{a['_id']}")])
    kb.append([Btn("🔙 Back", callback_data="m:home")])
    await safe_edit(q, text, Kb(kb))


async def show_api(q, a: dict):
    aid = str(a["_id"])
    text = (
        f"📡 <b>{esc(a['label'])}</b>\n"
        f"Type: <code>{a['type']}</code>\nModel: <code>{esc(a['model'])}</code>\n"
        f"Key: <code>{esc(mask(a['key']))}</code>\nStatus: {api_status(a)}"
    )
    kb = Kb([
        [Btn("🔕 Turn OFF" if a.get("enabled", True) else "🔔 Turn ON", callback_data=f"a:t:{aid}"),
         Btn("🧪 Test", callback_data=f"a:x:{aid}")],
        [Btn("⬆️ Priority up", callback_data=f"a:u:{aid}"),
         Btn("🗑 Delete", callback_data=f"a:d:{aid}")],
        [Btn("🔙 Back", callback_data="m:apis")],
    ])
    await safe_edit(q, text, kb)


async def show_settings(q):
    kb = Kb([
        [Btn("🔕 Bot OFF" if CFG["enabled"] else "🔔 Bot ON", callback_data="s:on")],
        [Btn(f"{'• ' if abs(CFG['chance'] - p / 100) < 0.01 else ''}{p}%", callback_data=f"s:c:{p}")
         for p in (10, 25, 40, 60)],
        [Btn("🔙 Back", callback_data="m:home")],
    ])
    await safe_edit(
        q,
        f"⚙️ <b>Settings</b>\n\nBot: {'ON ✅' if CFG['enabled'] else 'OFF ❌'}\n"
        f"Group random reply chance: <b>{int(CFG['chance'] * 100)}%</b>\n"
        f"(reply-to-bot, naam lene, hi/hello pe hamesha reply. Private me hamesha reply.)",
        kb,
    )


async def show_codes(q):
    rows = await codes_c.find({"used_by": None}).sort("created", -1).to_list(20)
    if not rows:
        await safe_edit(q, "🗝 <b>Unused codes</b>\n\nKoi unused code nahi hai.",
                        Kb([[Btn("🔑 Generate", callback_data="m:gen")], [Btn("🔙 Back", callback_data="m:home")]]))
        return
    text = "🗝 <b>Unused codes</b> (latest 20)\n\n"
    kb = []
    for r in rows:
        text += f"<code>{fmt_code(r['_id'])}</code> — {esc(r['label'])}\n"
        kb.append([Btn(f"🗑 {fmt_code(r['_id'])}", callback_data=f"k:del:{r['_id']}")])
    kb.append([Btn("🔙 Back", callback_data="m:home")])
    await safe_edit(q, text, Kb(kb))


async def show_users(q):
    rows = await users_c.find().sort("expires", -1).to_list(20)
    if not rows:
        await safe_edit(q, "👥 <b>Users</b>\n\nAbhi koi user nahi hai.", BACK)
        return
    now = time.time()
    text = "👥 <b>Users</b> (latest 20)\n\n"
    kb = []
    for r in rows:
        left = r.get("expires", 0) - now
        st = f"✅ {fmt_left(left)} baaki" if left > 0 else "⏰ expired"
        text += f"• {esc(r.get('name') or '-')} (<code>{r['_id']}</code>) — {st}\n"
        if left > 0:
            kb.append([Btn(f"🚫 Revoke {(r.get('name') or str(r['_id']))[:18]}", callback_data=f"u:rev:{r['_id']}")])
    kb.append([Btn("🔙 Back", callback_data="m:home")])
    await safe_edit(q, text, Kb(kb))


async def panel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_staff(q.from_user.id):
        return
    d = q.data.split(":")
    k = d[0] + ":" + d[1]

    if k == "m:home":
        context.user_data.pop("st", None)
        await safe_edit(q, panel_title(), main_kb())

    elif k == "m:apis":
        await show_apis(q)

    elif k == "m:add":
        context.user_data["st"] = "add"
        presets = ", ".join(PRESETS)
        await safe_edit(
            q,
            "➕ <b>Add API</b>\n\nEk msg me bhejo:\n<code>preset model key</code>\n\n"
            "Example:\n<code>groq llama-3.1-8b-instant gsk_xxxxx</code>\n\n"
            f"Presets: {presets}\n"
            "Custom (OpenAI-compatible): <code>custom model key base_url</code>\n"
            "Model ka naam provider ki site se copy-paste karo.\n"
            "Key wala msg main delete kar dunga.\nCancel: /cancel",
            BACK,
        )

    elif k == "m:gen":
        context.user_data.pop("st", None)
        kb = Kb([
            [Btn("1 Week", callback_data="k:p:1w"), Btn("1 Month", callback_data="k:p:1m"),
             Btn("3 Months", callback_data="k:p:3m")],
            [Btn("✏️ Custom", callback_data="k:cust")],
            [Btn("🔙 Back", callback_data="m:home")],
        ])
        await safe_edit(q, "🔑 <b>Generate Code</b>\n\nKitne time ka access dena hai? Ek chuno:", kb)

    elif k == "k:p":
        res = parse_dur(d[2])
        if res:
            secs, label, _ = res
            codes = await make_codes(secs, label, 1)
            await safe_edit(q, codes_text(codes, label),
                            Kb([[Btn("🔑 Generate another", callback_data="m:gen")],
                                [Btn("🔙 Back", callback_data="m:home")]]))

    elif k == "k:cust":
        context.user_data["st"] = "gen"
        await safe_edit(
            q,
            "✏️ <b>Custom duration</b>\n\nAise bhejo:\n"
            "<code>1w</code> = 1 week, <code>1m</code> = 1 month (30 din), <code>2m</code>, "
            "<code>10d</code> = 10 din, <code>12h</code> = 12 ghante\n"
            "Kai codes ek saath: <code>1m x5</code>\nCancel: /cancel",
            BACK,
        )

    elif k == "m:codes":
        await show_codes(q)

    elif k == "k:del":
        await codes_c.delete_one({"_id": d[2], "used_by": None})
        await show_codes(q)

    elif k == "m:users":
        await show_users(q)

    elif k == "u:rev":
        uid = int(d[2])
        await users_c.delete_one({"_id": uid})
        USERS.pop(uid, None)
        await show_users(q)

    elif k == "m:bc":
        context.user_data["st"] = "bc"
        await safe_edit(q, "📢 <b>Broadcast</b>\n\nJo bhejna hai wo yahan bhejo (text/photo/video sab chalega).\n"
                           "Ye group (agar set hai) aur saare active users ko jayega.\nCancel: /cancel", BACK)

    elif k == "m:style":
        context.user_data["st"] = None
        kb = Kb([[Btn("✏️ Set Style", callback_data="y:set"), Btn("🔄 Reset", callback_data="y:reset")],
                 [Btn("🔙 Back", callback_data="m:home")]])
        await safe_edit(q, f"🎭 <b>Style / Persona</b>\n\n<i>Current:</i>\n{esc(CFG['style'])[:3000]}", kb)

    elif k == "y:set":
        context.user_data["st"] = "style"
        await safe_edit(q, "✏️ Naya style/persona prompt bhejo (Hinglish rules, mood, limits sab isme likho).\n"
                           "Naam apne aap add ho jayega.\nCancel: /cancel", BACK)

    elif k == "y:reset":
        await save_cfg(style=DEFAULT_STYLE)
        await safe_edit(q, "✅ Style reset ho gaya.", BACK)

    elif k == "m:set":
        await show_settings(q)

    elif k == "s:on":
        await save_cfg(enabled=not CFG["enabled"])
        await show_settings(q)

    elif k == "s:c":
        await save_cfg(chance=int(d[2]) / 100)
        await show_settings(q)

    elif d[0] == "a":
        await reload_apis()
        a = get_api(d[2])
        if not a:
            await show_apis(q)
            return
        aid = d[2]
        if d[1] == "v":
            await show_api(q, a)
        elif d[1] == "t":
            await apis_c.update_one({"_id": a["_id"]}, {"$set": {"enabled": not a.get("enabled", True)}})
            COOL.pop(aid, None)
            await reload_apis()
            await show_api(q, get_api(aid))
        elif d[1] == "x":
            await safe_edit(q, "🧪 Testing…")
            ok, out = await test_api(a)
            if ok:
                COOL.pop(aid, None)
            await safe_edit(q, f"{'✅ Chal raha hai' if ok else '❌ Fail'}\n\n<code>{esc(out)}</code>",
                            Kb([[Btn("🔙 Back", callback_data=f"a:v:{aid}")]]))
        elif d[1] == "u":
            ids = [str(x["_id"]) for x in APIS]
            i = ids.index(aid)
            if i > 0:
                b = APIS[i - 1]
                await apis_c.update_one({"_id": a["_id"]}, {"$set": {"prio": b["prio"]}})
                await apis_c.update_one({"_id": b["_id"]}, {"$set": {"prio": a["prio"]}})
            await show_apis(q)
        elif d[1] == "d":
            await apis_c.delete_one({"_id": a["_id"]})
            COOL.pop(aid, None)
            await show_apis(q)


async def broadcast(msg) -> tuple[int, int]:
    targets = []
    if GROUP_ID is not None:
        targets.append(GROUP_ID)
    now = time.time()
    targets += [uid for uid, exp in USERS.items() if exp > now]
    ok = bad = 0
    for t in targets:
        try:
            await msg.copy(t)
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.05)
    return ok, bad


async def admin_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id
    st = context.user_data.get("st")

    if st == "bc":
        ok, bad = await broadcast(msg)
        await msg.reply_text(f"✅ Bhej diya: {ok} chat. Fail: {bad}.", reply_markup=main_kb())
        context.user_data.pop("st", None)

    elif st == "style" and msg.text:
        await save_cfg(style=msg.text.strip())
        await msg.reply_text("✅ Style save ho gaya.", reply_markup=main_kb())
        context.user_data.pop("st", None)

    elif st == "gen" and msg.text:
        res = parse_dur(msg.text)
        if not res:
            await msg.reply_text("Format samajh nahi aaya. Aise bhejo: 1w, 1m, 2m, 10d, 12h ya 1m x5. Cancel: /cancel")
            return
        secs, label, n = res
        codes = await make_codes(secs, label, n)
        await msg.reply_text(codes_text(codes, label), parse_mode=HTML, reply_markup=main_kb())
        context.user_data.pop("st", None)

    elif st == "add" and msg.text:
        parts = msg.text.split()
        try:
            await msg.delete()  # key chat me na dikhe
        except Exception:
            pass
        if len(parts) < 3:
            await context.bot.send_message(uid, "Format: preset model key (ya cancel: /cancel)")
            return
        preset, model, key = parts[0].lower(), parts[1], parts[2]
        if preset == "custom":
            if len(parts) < 4:
                await context.bot.send_message(uid, "Custom me base_url bhi do: custom model key base_url")
                return
            typ, base = "openai", parts[3].rstrip("/")
        elif preset in PRESETS:
            typ, base = PRESETS[preset]
        else:
            await context.bot.send_message(uid, f"Preset galat. Ye use karo: {', '.join(PRESETS)}, custom")
            return
        last = await apis_c.find_one(sort=[("prio", -1)])
        await apis_c.insert_one({
            "label": f"{preset}/{model[:20]}", "type": typ, "base": base, "model": model,
            "key": key, "enabled": True, "prio": (last["prio"] + 1) if last else 1,
        })
        await reload_apis()
        await context.bot.send_message(uid, "✅ API add ho gayi. 🧪 Test se check kar lo.", reply_markup=main_kb())
        context.user_data.pop("st", None)


async def private_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_user:
        return
    uid = update.effective_user.id
    if is_staff(uid) and context.user_data.get("st"):
        await admin_state(update, context)
        return
    if not msg.text:
        return
    if has_access(uid):
        await private_chat(update, context)
    else:
        await passcode_flow(update, context, msg.text)


# ---------- Keep-alive web server ----------
web = Flask(__name__)


@web.route("/")
def home():
    return f"{BOT_NAME} is alive"


def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


async def post_init(app: Application):
    global http
    http = httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0))
    await load_cfg()
    await reload_apis()
    async for u in users_c.find():
        USERS[u["_id"]] = u.get("expires", 0)


def main():
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    pv = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=pv))
    app.add_handler(CommandHandler("admin", admin_cmd, filters=pv))
    app.add_handler(CommandHandler("cancel", cancel, filters=pv))
    app.add_handler(CommandHandler("redeem", redeem_cmd, filters=pv))
    app.add_handler(CommandHandler("status", status_cmd, filters=pv))
    app.add_handler(CallbackQueryHandler(panel_cb, pattern=r"^(m|a|s|y|k|u):"))
    app.add_handler(MessageHandler(pv & ~filters.COMMAND, private_in))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, group_msg))
    app.run_polling()


if __name__ == "__main__":
    main()
