import os
import re
import secrets
import string
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, Request, HTTPException
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DOMAIN = os.environ.get("DOMAIN", "mg.abdr.tax").strip().lower()
TG_WEBHOOK_PATH = os.environ.get("TG_WEBHOOK_PATH", "/telegram").strip()
TG_SECRET_TOKEN = os.environ.get("TG_SECRET_TOKEN", "").strip()
MAILGUN_WEBHOOK_SECRET = os.environ.get("MAILGUN_WEBHOOK_SECRET", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

# بدون تخزين دائم (RAM فقط)
user_emails: Dict[int, List[str]] = {}
user_last_email: Dict[int, str] = {}
waiting_for_name: Set[int] = set()
email_owner: Dict[str, int] = {}

def sanitize_local_part(raw: str) -> str:
    s = raw.strip().lower()
    s = re.sub(r"\s+", ".", s)
    s = re.sub(r"[^a-z0-9._-]", "", s)
    s = re.sub(r"\.+", ".", s).strip(".")
    return s[:32]

def random_local_part(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def make_email(local_part: str) -> str:
    return f"{local_part}@{DOMAIN}"

def remember_email(user_id: int, email: str) -> None:
    lst = user_emails.setdefault(user_id, [])
    if email not in lst:
        lst.append(email)
    user_last_email[user_id] = email
    email_owner[email] = user_id

def start_text(last_email: Optional[str]) -> str:
    base = (
        "مرحباً بك في بوت البريد المؤقت ✉️\n"
        "استخدم هذا البوت لإنشاء بريد إلكتروني مؤقت للتسجيل في المواقع دون الكشف عن بريدك الحقيقي."
    )
    if last_email:
        return f"{base}\n\nبريدك الحالي:\n`{last_email}`"
    return base

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ اختر اسم", callback_data="choose_name")],
        [InlineKeyboardButton("🎲 إنشاء بريد عشوائي", callback_data="random_email")],
        [InlineKeyboardButton("📋 انسخ البريد الإلكتروني", callback_data="copy_email")],
        [InlineKeyboardButton("📁 بريدي الخاص", callback_data="my_emails")],
    ])

def format_my_emails(emails: List[str]) -> str:
    lines = ["📁 بريداتي:"]
    for e in emails:
        lines.append(f"• `{e}`")
    return "\n".join(lines)

def format_inbound_message(to_email: str, sender: str, subject: str, body: str) -> str:
    body = (body or "").strip()
    if len(body) > 3500:
        body = body[:3500] + "\n…"
    return (
        "📩 وصلت رسالة جديدة\n\n"
        f"إلى: `{to_email}`\n"
        f"من: {sender}\n"
        f"العنوان: {subject}\n\n"
        f"{body if body else '(بدون نص)'}"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    last = user_last_email.get(uid)
    await update.message.reply_text(
        start_text(last),
        reply_markup=main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "choose_name":
        waiting_for_name.add(uid)
        await q.edit_message_text("✏️ اكتب الاسم الذي تريد استخدامه للبريد:")
        return

    if data == "random_email":
        email = make_email(random_local_part())
        remember_email(uid, email)
        await q.edit_message_text(
            f"تم إنشاء بريد إلكتروني جديد ✅\n\n- البريد الإلكتروني الجديد:\n`{email}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="back")]]),
        )
        return

    if data == "copy_email":
        last = user_last_email.get(uid)
        if not last:
            await q.edit_message_text(
                "❌ لم يتم إنشاء بريد بعد",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="back")]]),
            )
            return
        # يرسل آخر بريد تم إنشاؤه فقط
        await q.message.reply_text(f"`{last}`", parse_mode=ParseMode.MARKDOWN)
        return

    if data == "my_emails":
        emails = user_emails.get(uid, [])
        if not emails:
            await q.edit_message_text(
                "📁 لا يوجد بريدات تم إنشاؤها بعد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="back")]]),
            )
            return
        await q.edit_message_text(
            format_my_emails(emails),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="back")]]),
        )
        return

    if data == "back":
        last = user_last_email.get(uid)
        await q.edit_message_text(
            start_text(last),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in waiting_for_name:
        return
    raw = update.message.text or ""
    local = sanitize_local_part(raw)
    if not local:
        await update.message.reply_text("❌ الاسم غير صالح. حاول مرة أخرى:")
        return
    waiting_for_name.discard(uid)
    email = make_email(local)
    remember_email(uid, email)
    await update.message.reply_text(
        f"تم إنشاء بريد إلكتروني جديد ✅\n\n- البريد الإلكتروني الجديد:\n`{email}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="back")]]),
    )

app = FastAPI()
tg_app: Optional[Application] = None

@app.on_event("startup")
async def startup():
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    await tg_app.initialize()
    await tg_app.start()

@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()

@app.get("/health")
async def health():
    return {"ok": True}

@app.post(TG_WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if TG_SECRET_TOKEN:
        hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if hdr != TG_SECRET_TOKEN:
            raise HTTPException(status_code=403, detail="Bad telegram secret token")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.update_queue.put(update)
    return {"ok": True}

@app.post("/mailgun")
async def mailgun_inbound(request: Request):
    if MAILGUN_WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != MAILGUN_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Bad mailgun secret")

    form = await request.form()
    to_email = str(form.get("recipient", "")).strip().lower()
    sender = str(form.get("sender", "")).strip()
    subject = str(form.get("subject", "")).strip()
    body = str(form.get("stripped-text") or form.get("body-plain") or "").strip()

    if not to_email:
        return {"ok": True}

    owner_id = email_owner.get(to_email)
    if not owner_id:
        return {"ok": True}

    msg = format_inbound_message(to_email, sender, subject, body)
    try:
        await tg_app.bot.send_message(
            chat_id=owner_id,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    return {"ok": True}
