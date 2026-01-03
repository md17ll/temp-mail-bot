import os
import re
import secrets
import string
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import asyncpg
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

# رابط تطبيقك على Railway مثال:
# https://web-production-5256.up.railway.app
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")

TG_WEBHOOK_PATH = os.environ.get("TG_WEBHOOK_PATH", "/telegram").strip()
if not TG_WEBHOOK_PATH.startswith("/"):
    TG_WEBHOOK_PATH = "/" + TG_WEBHOOK_PATH

TG_SECRET_TOKEN = os.environ.get("TG_SECRET_TOKEN", "").strip()
MAILGUN_WEBHOOK_SECRET = os.environ.get("MAILGUN_WEBHOOK_SECRET", "").strip()

OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
OWNER_ID: Optional[int] = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

# RAM فقط للأشياء المؤقتة (مثل انتظار الاسم)
waiting_for_name: Set[int] = set()

app = FastAPI()
tg_app: Optional[Application] = None
db_pool: Optional[asyncpg.Pool] = None


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


def _normalize_db_url(url: str) -> str:
    """
    Railway DATABASE_URL قد يأتي مع sslmode=require
    asyncpg لا يحب sslmode كـ query param أحيانًا، فبنحذفه ونخلي SSL بالاتصال.
    """
    if not url:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.pop("sslmode", None)
    new_query = urlencode(q)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_query, u.fragment))


async def db_init():
    global db_pool
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL env var (Railway Postgres connection string)")

    clean_url = _normalize_db_url(DATABASE_URL)

    # نحاول SSL require (آمن على Railway)
    db_pool = await asyncpg.create_pool(
        dsn=clean_url,
        ssl="require",
        min_size=1,
        max_size=5,
    )

    async with db_pool.acquire() as conn:
        # جدول المستخدمين (آخر بريد)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS tg_users (
            user_id BIGINT PRIMARY KEY,
            last_email TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        # جدول البريدات
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS tg_emails (
            email TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        # فهرس للمستخدم لتسريع جلب بريداته
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tg_emails_user_id ON tg_emails(user_id);
        """)


async def db_get_last_email(user_id: int) -> Optional[str]:
    if not db_pool:
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_email FROM tg_users WHERE user_id=$1", user_id)
        return row["last_email"] if row and row["last_email"] else None


async def db_get_my_emails(user_id: int) -> List[str]:
    if not db_pool:
        return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT email FROM tg_emails WHERE user_id=$1 ORDER BY created_at DESC",
            user_id
        )
        return [r["email"] for r in rows]


async def db_remember_email(user_id: int, email: str) -> None:
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # خزّن البريد (لو موجود ما يعيد إدخال)
            await conn.execute(
                "INSERT INTO tg_emails(email, user_id) VALUES($1,$2) ON CONFLICT (email) DO NOTHING",
                email, user_id
            )
            # حدّث آخر بريد للمستخدم
            await conn.execute("""
            INSERT INTO tg_users(user_id, last_email) VALUES($1,$2)
            ON CONFLICT (user_id) DO UPDATE
            SET last_email=EXCLUDED.last_email, updated_at=NOW()
            """, user_id, email)


async def db_get_owner_by_email(email: str) -> Optional[int]:
    if not db_pool:
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM tg_emails WHERE email=$1", email)
        return int(row["user_id"]) if row else None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    last = await db_get_last_email(uid)
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
        await db_remember_email(uid, email)
        await q.edit_message_text(
            f"تم إنشاء بريد إلكتروني جديد ✅\n\n- البريد الإلكتروني الجديد:\n`{email}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="back")]]),
        )
        return

    if data == "copy_email":
        last = await db_get_last_email(uid)
        if not last:
            await q.edit_message_text(
                "❌ لم يتم إنشاء بريد بعد",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="back")]]),
            )
            return
        await q.message.reply_text(f"`{last}`", parse_mode=ParseMode.MARKDOWN)
        return

    if data == "my_emails":
        emails = await db_get_my_emails(uid)
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
        last = await db_get_last_email(uid)
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
    await db_remember_email(uid, email)
    await update.message.reply_text(
        f"تم إنشاء بريد إلكتروني جديد ✅\n\n- البريد الإلكتروني الجديد:\n`{email}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 عودة", callback_data="back")]]),
    )


@app.on_event("startup")
async def startup():
    global tg_app

    # ✅ DB init (يخلق الجداول تلقائياً)
    await db_init()

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await tg_app.initialize()
    await tg_app.start()

    # ✅ تعيين Webhook تلقائياً (إذا PUBLIC_URL موجود)
    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL}{TG_WEBHOOK_PATH}"
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=TG_SECRET_TOKEN if TG_SECRET_TOKEN else None,
            drop_pending_updates=True,
        )

    # ✅ (اختياري) رسالة تأكيد للمالك
    if OWNER_ID:
        try:
            msg = "✅ Bot started"
            if PUBLIC_URL:
                msg += f"\nWebhook: `{PUBLIC_URL}{TG_WEBHOOK_PATH}`"
            await tg_app.bot.send_message(
                chat_id=OWNER_ID,
                text=msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


@app.on_event("shutdown")
async def shutdown():
    global db_pool
    if tg_app:
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        await tg_app.stop()
        await tg_app.shutdown()

    if db_pool:
        try:
            await db_pool.close()
        except Exception:
            pass
        db_pool = None


@app.get("/")
async def root():
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}


@app.post(TG_WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if not tg_app:
        raise HTTPException(status_code=500, detail="Bot not ready")

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
    if not tg_app:
        return {"ok": True}

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

    owner_id = await db_get_owner_by_email(to_email)
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
