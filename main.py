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

# ✅ مهم: للهروب من مشاكل Markdown
from telegram.helpers import escape_markdown

# ✅ تخزين دائم على Volume (/data)
import json
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"


def load_state() -> None:
    global user_emails, user_last_email, email_owner, blocked_users
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            user_emails = {int(k): v for k, v in (data.get("user_emails") or {}).items()}
            user_last_email = {int(k): v for k, v in (data.get("user_last_email") or {}).items()}
            email_owner = (data.get("email_owner") or {})
            blocked_users = set(int(x) for x in (data.get("blocked_users") or []))
    except Exception as e:
        print("load_state error:", repr(e))
        user_emails = {}
        user_last_email = {}
        email_owner = {}
        blocked_users = set()


def save_state() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "user_emails": {str(k): v for k, v in user_emails.items()},
            "user_last_email": {str(k): v for k, v in user_last_email.items()},
            "email_owner": email_owner,
            "blocked_users": sorted(list(blocked_users)),
        }
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("save_state error:", repr(e))


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DOMAIN = os.environ.get("DOMAIN", "mg.abdr.tax").strip().lower()

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")

TG_WEBHOOK_PATH = os.environ.get("TG_WEBHOOK_PATH", "/telegram").strip()
if not TG_WEBHOOK_PATH.startswith("/"):
    TG_WEBHOOK_PATH = "/" + TG_WEBHOOK_PATH

TG_SECRET_TOKEN = os.environ.get("TG_SECRET_TOKEN", "").strip()
MAILGUN_WEBHOOK_SECRET = os.environ.get("MAILGUN_WEBHOOK_SECRET", "").strip()

OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
OWNER_ID: Optional[int] = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

user_emails: Dict[int, List[str]] = {}
user_last_email: Dict[int, str] = {}
waiting_for_name: Set[int] = set()
email_owner: Dict[str, int] = {}

# ✅ (إضافة فقط) حظر/فك حظر
blocked_users: Set[int] = set()
admin_waiting_block: Set[int] = set()
admin_waiting_unblock: Set[int] = set()


def is_admin(user_id: int) -> bool:
    return bool(OWNER_ID) and user_id == OWNER_ID


def is_blocked(user_id: int) -> bool:
    return user_id in blocked_users


def parse_target_user_id(text: str) -> Optional[int]:
    t = (text or "").strip()
    m = re.search(r"\d{5,}", t)
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 حظر شخص", callback_data="admin_block")],
        [InlineKeyboardButton("✅ فك حظر شخص", callback_data="admin_unblock")],
        [InlineKeyboardButton("🔙 عودة", callback_data="back")],
    ])


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
    save_state()


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


# ✅ (إضافة فقط) نفس الكيبورد + زر الأدمن يظهر للأدمن فقط
def main_keyboard_for(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ اختر اسم", callback_data="choose_name")],
        [InlineKeyboardButton("🎲 إنشاء بريد عشوائي", callback_data="random_email")],
        [InlineKeyboardButton("📋 انسخ البريد الإلكتروني", callback_data="copy_email")],
        [InlineKeyboardButton("📁 بريدي الخاص", callback_data="my_emails")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠️ Admin", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)


def format_my_emails(emails: List[str]) -> str:
    lines = ["📁 بريداتي:"]
    for e in emails:
        lines.append(f"• `{e}`")
    return "\n".join(lines)


def format_inbound_message(to_email: str, sender: str, subject: str, body: str) -> str:
    body = (body or "").strip()
    if len(body) > 3500:
        body = body[:3500] + "\n…"

    # ✅ Escape Markdown حتى ما تفشل الرسالة
    to_email_e = escape_markdown(to_email or "", version=2)
    sender_e = escape_markdown(sender or "", version=2)
    subject_e = escape_markdown(subject or "", version=2)
    body_e = escape_markdown(body or "(بدون نص)", version=2)

    return (
        "📩 وصلت رسالة جديدة\n\n"
        f"إلى: `{to_email_e}`\n"
        f"من: {sender_e}\n"
        f"العنوان: {subject_e}\n\n"
        f"{body_e}"
    )


_EMAIL_RE = re.compile(r"([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})", re.IGNORECASE)


def extract_emails(text: str) -> List[str]:
    if not text:
        return []
    found = _EMAIL_RE.findall(text)
    seen = set()
    out: List[str] = []
    for e in found:
        e2 = e.strip().lower()
        if e2 and e2 not in seen:
            seen.add(e2)
            out.append(e2)
    return out


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # ✅ (إضافة فقط) منع المحظورين
    if is_blocked(uid) and not is_admin(uid):
        await update.message.reply_text("🚫 أنت محظور من استخدام البوت.")
        return

    last = user_last_email.get(uid)
    await update.message.reply_text(
        start_text(last),
        reply_markup=main_keyboard_for(uid),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    # ✅ (إضافة فقط) منع المحظورين
    if is_blocked(uid) and not is_admin(uid):
        await q.answer("🚫 أنت محظور.", show_alert=True)
        return

    # ✅ (إضافة فقط) قائمة الأدمن
    if data == "admin_menu":
        if not is_admin(uid):
            await q.answer("غير مصرح", show_alert=True)
            return
        await q.edit_message_text("🛠️ لوحة الأدمن:", reply_markup=admin_keyboard())
        return

    # ✅ (إضافة فقط) حظر شخص
    if data == "admin_block":
        if not is_admin(uid):
            await q.answer("غير مصرح", show_alert=True)
            return
        admin_waiting_block.add(uid)
        admin_waiting_unblock.discard(uid)
        await q.edit_message_text(
            "🚫 أرسل الآن ID الشخص المراد حظره:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_menu")]]),
        )
        return

    # ✅ (إضافة فقط) فك حظر شخص
    if data == "admin_unblock":
        if not is_admin(uid):
            await q.answer("غير مصرح", show_alert=True)
            return
        admin_waiting_unblock.add(uid)
        admin_waiting_block.discard(uid)
        await q.edit_message_text(
            "✅ أرسل الآن ID الشخص المراد فك حظره:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_menu")]]),
        )
        return

    if data == "choose_name":
        waiting_for_name.add(uid)
        await q.edit_message_text("✏️ اكتب الاسم الذي تريد استخدامه للبريد:")
        return

    if data == "random_email":
        email = make_email(random_local_part())

        # ✅ (إضافة فقط) منع التصادم/سرقة الإيميل: إذا محجوز لغيرك ولّد غيره
        while True:
            existing_owner = email_owner.get(email)
            if not existing_owner or existing_owner == uid:
                break
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
            reply_markup=main_keyboard_for(uid),
            disable_web_page_preview=True,
        )
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # ✅ (إضافة فقط) منع المحظورين
    if is_blocked(uid) and not is_admin(uid):
        return

    # ✅ (إضافة فقط) استقبال ID للحظر/فك الحظر من الأدمن
    if uid in admin_waiting_block:
        target_id = parse_target_user_id(update.message.text or "")
        if not target_id:
            await update.message.reply_text("❌ ارسل ID صحيح (أرقام فقط).")
            return
        blocked_users.add(target_id)
        save_state()
        admin_waiting_block.discard(uid)
        await update.message.reply_text(f"✅ تم حظر المستخدم: `{target_id}`", parse_mode=ParseMode.MARKDOWN)
        return

    if uid in admin_waiting_unblock:
        target_id = parse_target_user_id(update.message.text or "")
        if not target_id:
            await update.message.reply_text("❌ ارسل ID صحيح (أرقام فقط).")
            return
        if target_id in blocked_users:
            blocked_users.discard(target_id)
            save_state()
            admin_waiting_unblock.discard(uid)
            await update.message.reply_text(f"✅ تم فك حظر المستخدم: `{target_id}`", parse_mode=ParseMode.MARKDOWN)
            return
        admin_waiting_unblock.discard(uid)
        await update.message.reply_text("ℹ️ هذا المستخدم غير محظور أساساً.")
        return

    if uid not in waiting_for_name:
        return
    raw = update.message.text or ""
    local = sanitize_local_part(raw)
    if not local:
        await update.message.reply_text("❌ الاسم غير صالح. حاول مرة أخرى:")
        return
    waiting_for_name.discard(uid)
    email = make_email(local)

    # ✅ (إضافة فقط) منع استخدام نفس الإيميل لشخص ثاني
    existing_owner = email_owner.get(email)
    if existing_owner and existing_owner != uid:
        await update.message.reply_text("❌ هذا البريد محجوز لشخص آخر. اختر اسم مختلف.")
        return

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
    load_state()

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await tg_app.initialize()
    await tg_app.start()

    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL}{TG_WEBHOOK_PATH}"
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=TG_SECRET_TOKEN if TG_SECRET_TOKEN else None,
            drop_pending_updates=True,
        )
        print("Telegram webhook set to:", webhook_url)
    else:
        print("WARNING: PUBLIC_URL is empty, webhook not set!")

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
        except Exception as e:
            print("Owner notify error:", repr(e))


@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            print("delete_webhook error:", repr(e))
        await tg_app.stop()
        await tg_app.shutdown()


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

    recipient_raw = str(form.get("recipient", "") or "")
    to_raw = str(form.get("To", "") or form.get("to", "") or "")
    envelope_to_raw = str(form.get("envelope", "") or "")

    candidates_text = " , ".join([recipient_raw, to_raw, envelope_to_raw]).strip()
    recipients = extract_emails(candidates_text)

    sender = str(form.get("sender", "")).strip()
    subject = str(form.get("subject", "")).strip()
    body = str(form.get("stripped-text") or form.get("body-plain") or "").strip()

    print("MAILGUN INBOUND recipients:", recipients, "sender:", sender, "subject:", subject)

    if not recipients:
        return {"ok": True}

    sent_any = False
    for to_email in recipients:
        owner_id = email_owner.get(to_email)
        if not owner_id:
            print("No owner for:", to_email)
            continue

        # ✅ (إضافة فقط) إذا صاحب الإيميل محظور لا يتم إرسال الرسائل له
        if owner_id in blocked_users and (not OWNER_ID or owner_id != OWNER_ID):
            print("Blocked owner, skip deliver to:", owner_id, "email:", to_email)
            continue

        msg = format_inbound_message(to_email, sender, subject, body)
        try:
            await tg_app.bot.send_message(
                chat_id=owner_id,
                text=msg,
                parse_mode=ParseMode.MARKDOWN_V2,  # ✅ صار آمن بعد escape
                disable_web_page_preview=True,
            )
            sent_any = True
        except Exception as e:
            print("Telegram send_message error:", repr(e))

    return {"ok": True, "delivered": sent_any}
