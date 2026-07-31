import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Request
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
from telegram.helpers import escape_markdown

# ✅ تخزين دائم على Volume (/data)
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"

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

# البيانات الأصلية
user_emails: Dict[int, List[str]] = {}
user_last_email: Dict[int, str] = {}
waiting_for_name: Set[int] = set()
email_owner: Dict[str, int] = {}
blocked_users: Set[int] = set()

# الاشتراكات والإدارة
subscriptions: Dict[int, Dict[str, Any]] = {}
known_users: Dict[int, Dict[str, Any]] = {}
admin_reply_targets: Dict[int, int] = {}
notification_settings: Dict[str, bool] = {
    "new_users": True,
    "forward_messages": True,
    "expiry_warning": True,
}

# حالات مؤقتة لا تحتاج حفظاً دائماً
admin_pending: Dict[int, str] = {}
admin_pending_target: Dict[int, int] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def str_to_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def load_state() -> None:
    global user_emails, user_last_email, email_owner, blocked_users
    global subscriptions, known_users, admin_reply_targets, notification_settings

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not STATE_FILE.exists():
            return

        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        user_emails = {
            int(k): list(v or []) for k, v in (data.get("user_emails") or {}).items()
        }
        user_last_email = {
            int(k): str(v) for k, v in (data.get("user_last_email") or {}).items()
        }
        email_owner = {
            str(k).lower(): int(v) for k, v in (data.get("email_owner") or {}).items()
        }
        blocked_users = set(int(x) for x in (data.get("blocked_users") or []))

        subscriptions = {
            int(k): dict(v or {}) for k, v in (data.get("subscriptions") or {}).items()
        }
        known_users = {
            int(k): dict(v or {}) for k, v in (data.get("known_users") or {}).items()
        }
        admin_reply_targets = {
            int(k): int(v) for k, v in (data.get("admin_reply_targets") or {}).items()
        }

        saved_settings = data.get("notification_settings") or {}
        notification_settings.update(
            {
                "new_users": bool(saved_settings.get("new_users", True)),
                "forward_messages": bool(saved_settings.get("forward_messages", True)),
                "expiry_warning": bool(saved_settings.get("expiry_warning", True)),
            }
        )
    except Exception as exc:
        print("load_state error:", repr(exc))
        user_emails = {}
        user_last_email = {}
        email_owner = {}
        blocked_users = set()
        subscriptions = {}
        known_users = {}
        admin_reply_targets = {}
        notification_settings = {
            "new_users": True,
            "forward_messages": True,
            "expiry_warning": True,
        }


def save_state() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "user_emails": {str(k): v for k, v in user_emails.items()},
            "user_last_email": {str(k): v for k, v in user_last_email.items()},
            "email_owner": email_owner,
            "blocked_users": sorted(blocked_users),
            "subscriptions": {str(k): v for k, v in subscriptions.items()},
            "known_users": {str(k): v for k, v in known_users.items()},
            "admin_reply_targets": {
                str(k): v for k, v in list(admin_reply_targets.items())[-2000:]
            },
            "notification_settings": notification_settings,
        }
        temp_file = STATE_FILE.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_file.replace(STATE_FILE)
    except Exception as exc:
        print("save_state error:", repr(exc))


def add_admin_log(action: str) -> None:
    """وظيفة توافقية بلا تخزين."""
    return


def is_admin(user_id: int) -> bool:
    return bool(OWNER_ID) and user_id == OWNER_ID


def is_blocked(user_id: int) -> bool:
    return user_id in blocked_users


def subscription_expiry(user_id: int) -> Optional[datetime]:
    record = subscriptions.get(user_id)
    if not record:
        return None
    return str_to_dt(record.get("expires_at"))


def is_permanent_subscription(user_id: int) -> bool:
    record = subscriptions.get(user_id)
    return bool(record) and record.get("expires_at") is None


def has_active_subscription(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    if is_blocked(user_id):
        return False

    record = subscriptions.get(user_id)
    if not record:
        return False

    expires_at = str_to_dt(record.get("expires_at"))
    return expires_at is None or expires_at > now_utc()


def remaining_days(user_id: int) -> Optional[int]:
    expires_at = subscription_expiry(user_id)
    if expires_at is None:
        return None
    seconds = (expires_at - now_utc()).total_seconds()
    return max(0, math.ceil(seconds / 86400))


def subscription_status_text(user_id: int) -> str:
    if is_admin(user_id):
        return "أدمن — مفتوح دائماً"
    if is_blocked(user_id):
        return "محظور"
    record = subscriptions.get(user_id)
    if not record:
        return "غير مسموح"
    if record.get("expires_at") is None:
        return "مفتوح بدون مدة"

    expires_at = str_to_dt(record.get("expires_at"))
    if not expires_at:
        return "بيانات اشتراك غير صالحة"
    if expires_at <= now_utc():
        return f"منتهي منذ {expires_at.strftime('%Y-%m-%d')}"
    return f"فعال — باقي {remaining_days(user_id)} يوم — ينتهي {expires_at.strftime('%Y-%m-%d')}"


def parse_target_user_id(text: str) -> Optional[int]:
    match = re.search(r"\d{5,}", (text or "").strip())
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def make_button(
    text: str,
    callback_data: str,
    style: Optional[str] = None,
) -> InlineKeyboardButton:
    """يدعم ألوان تيليجرام الجديدة، مع توافق الإصدارات الأقدم من المكتبة."""
    if not style:
        return InlineKeyboardButton(text, callback_data=callback_data)
    try:
        return InlineKeyboardButton(text, callback_data=callback_data, style=style)
    except TypeError:
        return InlineKeyboardButton(
            text,
            callback_data=callback_data,
            api_kwargs={"style": style},
        )


def back_button(callback_data: str = "back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[make_button("🔙 عودة", callback_data, "primary")]]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    """القائمة الرئيسية للأدمن بشكل هرمي ومقسمة حسب الوظائف."""
    return InlineKeyboardMarkup(
        [
            [make_button("👑 إدارة الاشتراكات", "admin_section_subscriptions", "success")],
            [
                make_button("🔎 البحث والبريد", "admin_section_search", "primary"),
                make_button("📊 المتابعة", "admin_section_monitor", "primary"),
            ],
            [
                make_button("📣 التواصل", "admin_section_communication", "success"),
                make_button("🛡️ الحماية", "admin_section_security", "danger"),
            ],
            [make_button("🔙 عودة", "back", "primary")],
        ]
    )


def admin_subscriptions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [make_button("➕ إضافة مستخدم", "admin_add_user", "success")],
            [
                make_button("⏳ تمديد اشتراك", "admin_extend", "primary"),
                make_button("👥 المستخدمون", "admin_users", "primary"),
            ],
            [
                make_button("⌛ قرب الانتهاء", "admin_expiring", "primary"),
                make_button("❌ إلغاء اشتراك", "admin_cancel_sub", "danger"),
            ],
            [make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def admin_search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [make_button("🔍 بحث عن مستخدم", "admin_search", "primary")],
            [
                make_button("📧 بحث عن بريد", "admin_search_email", "success"),
                make_button("📬 إدارة البريدات", "admin_manage_emails", "primary"),
            ],
            [make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def admin_monitor_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [make_button("📊 الإحصائيات", "admin_stats", "primary")],
            [make_button("👥 المستخدمون الفعالون", "admin_users", "success")],
            [make_button("⌛ قرب الانتهاء", "admin_expiring", "primary")],
            [make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def admin_communication_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [make_button("📢 رسالة جماعية", "admin_broadcast", "success")],
            [make_button("🔔 إعدادات التنبيهات", "admin_notifications", "primary")],
            [make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def admin_security_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                make_button("🚫 حظر شخص", "admin_block", "danger"),
                make_button("✅ فك الحظر", "admin_unblock", "success"),
            ],
            [make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def subscription_duration_keyboard(action: str, target_id: int) -> InlineKeyboardMarkup:
    prefix = f"subscription:{action}:{target_id}:"
    return InlineKeyboardMarkup(
        [
            [
                make_button("1 يوم", prefix + "1", "primary"),
                make_button("1 أسبوع", prefix + "7", "primary"),
            ],
            [
                make_button("1 شهر", prefix + "30", "primary"),
                make_button("1 سنة", prefix + "365", "primary"),
            ],
            [make_button("غير محدد", prefix + "permanent", "success")],
            [make_button("🔙 إدارة الاشتراكات", "admin_section_subscriptions", "primary")],
        ]
    )


def notifications_keyboard() -> InlineKeyboardMarkup:
    def label(title: str, key: str) -> str:
        return f"{'🟢' if notification_settings.get(key, True) else '🔴'} {title}"

    return InlineKeyboardMarkup(
        [
            [
                make_button(
                    label("إشعار دخول جديد", "new_users"),
                    "notify_toggle:new_users",
                    "success" if notification_settings["new_users"] else "danger",
                )
            ],
            [
                make_button(
                    label("تحويل رسائل المستخدمين", "forward_messages"),
                    "notify_toggle:forward_messages",
                    "success"
                    if notification_settings["forward_messages"]
                    else "danger",
                )
            ],
            [
                make_button(
                    label("تنبيه 3 أيام للمستخدم", "expiry_warning"),
                    "notify_toggle:expiry_warning",
                    "success"
                    if notification_settings["expiry_warning"]
                    else "danger",
                )
            ],
            [make_button("🔙 التواصل", "admin_section_communication", "primary")],
        ]
    )


def sanitize_local_part(raw: str) -> str:
    value = raw.strip().lower()
    value = re.sub(r"\s+", ".", value)
    value = re.sub(r"[^a-z0-9._-]", "", value)
    value = re.sub(r"\.+", ".", value).strip(".")
    return value[:32]


def random_local_part(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def make_email(local_part: str) -> str:
    return f"{local_part}@{DOMAIN}"


def remember_email(user_id: int, email: str) -> None:
    email = email.lower()
    emails = user_emails.setdefault(user_id, [])
    if email not in emails:
        emails.append(email)
    user_last_email[user_id] = email
    email_owner[email] = user_id
    save_state()


def delete_email(user_id: int, index: int) -> Optional[str]:
    emails = user_emails.get(user_id, [])
    if index < 0 or index >= len(emails):
        return None

    deleted = emails.pop(index)
    if email_owner.get(deleted) == user_id:
        email_owner.pop(deleted, None)

    if emails:
        user_emails[user_id] = emails
        if user_last_email.get(user_id) == deleted:
            user_last_email[user_id] = emails[-1]
    else:
        user_emails.pop(user_id, None)
        user_last_email.pop(user_id, None)

    save_state()
    return deleted


def start_text(last_email: Optional[str]) -> str:
    base = (
        "مرحباً بك في بوت البريد المؤقت ✉️\n"
        "استخدم هذا البوت لإنشاء بريد إلكتروني مؤقت للتسجيل في المواقع دون الكشف عن بريدك الحقيقي."
    )
    if last_email:
        return f"{base}\n\nبريدك الحالي:\n`{last_email}`"
    return base


def main_keyboard() -> InlineKeyboardMarkup:
    return main_keyboard_for(0)


def main_keyboard_for(uid: int) -> InlineKeyboardMarkup:
    # ترتيب هرمي وألوان حسب نوع الإجراء.
    rows = [
        [make_button("🎲 إنشاء بريد عشوائي", "random_email", "success")],
        [
            make_button("✏️ اختر اسم", "choose_name", "primary"),
            make_button("📋 انسخ البريد", "copy_email", "primary"),
        ],
        [make_button("📁 بريدي الخاص", "my_emails", "success")],
    ]
    if is_admin(uid):
        rows.append([make_button("🛠️ Admin", "admin_menu", "primary")])
    return InlineKeyboardMarkup(rows)


def my_emails_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for index, email in enumerate(user_emails.get(user_id, [])):
        rows.append(
            [make_button(f"🗑 حذف {email}", f"email_delete:{index}", "danger")]
        )
    rows.append([make_button("🔙 عودة", "back", "primary")])
    return InlineKeyboardMarkup(rows)


def format_my_emails(emails: List[str]) -> str:
    lines = ["📁 بريداتي:"]
    for index, email in enumerate(emails, start=1):
        lines.append(f"{index}. `{email}`")
    lines.append("\nاضغط زر الحذف الخاص بالبريد المطلوب.")
    return "\n".join(lines)


def format_inbound_message(to_email: str, sender: str, subject: str, body: str) -> str:
    body = (body or "").strip()
    if len(body) > 3500:
        body = body[:3500] + "\n…"

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


_EMAIL_RE = re.compile(
    r"([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})", re.IGNORECASE
)


def extract_emails(text: str) -> List[str]:
    if not text:
        return []
    found = _EMAIL_RE.findall(text)
    seen: Set[str] = set()
    output: List[str] = []
    for email in found:
        normalized = email.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
    return output


def user_display_name(user_id: int) -> str:
    data = known_users.get(user_id, {})
    name = str(data.get("name") or "بدون اسم")
    username = str(data.get("username") or "")
    return f"{name} (@{username})" if username else name


def update_known_user(user: Any) -> bool:
    user_id = int(user.id)
    is_new = user_id not in known_users
    previous = known_users.get(user_id, {})
    known_users[user_id] = {
        "name": user.full_name or previous.get("name") or "بدون اسم",
        "username": user.username or previous.get("username") or "",
        "first_seen": previous.get("first_seen") or dt_to_str(now_utc()),
        "last_seen": dt_to_str(now_utc()),
    }
    return is_new


def trim_reply_targets() -> None:
    while len(admin_reply_targets) > 2000:
        first_key = next(iter(admin_reply_targets))
        admin_reply_targets.pop(first_key, None)


async def relay_user_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or is_admin(user.id) or not OWNER_ID:
        return

    is_new = update_known_user(user)

    if is_new and notification_settings.get("new_users", True):
        username = f"@{user.username}" if user.username else "بدون يوزر"
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "🔔 دخل مستخدم جديد إلى البوت\n\n"
                    f"الاسم: {user.full_name}\n"
                    f"اليوزر: {username}\n"
                    f"ID: {user.id}\n"
                    f"الحالة: {subscription_status_text(user.id)}"
                ),
            )
        except Exception as exc:
            print("new user notify error:", repr(exc))

    if notification_settings.get("forward_messages", True):
        forwarded_message_id: Optional[int] = None
        try:
            forwarded = await context.bot.forward_message(
                chat_id=OWNER_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
            forwarded_message_id = forwarded.message_id
        except Exception as forward_exc:
            print("forward_message error:", repr(forward_exc))
            try:
                header = await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=(
                        "📨 رسالة من مستخدم\n"
                        f"الاسم: {user.full_name}\n"
                        f"اليوزر: @{user.username if user.username else 'بدون يوزر'}\n"
                        f"ID: {user.id}"
                    ),
                )
                copied = await context.bot.copy_message(
                    chat_id=OWNER_ID,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                admin_reply_targets[header.message_id] = user.id
                forwarded_message_id = copied.message_id
            except Exception as copy_exc:
                print("copy_message fallback error:", repr(copy_exc))
                if message.text:
                    fallback = await context.bot.send_message(
                        chat_id=OWNER_ID,
                        text=(
                            f"📨 رسالة من {user.full_name} — ID: {user.id}\n\n"
                            f"{message.text}"
                        ),
                    )
                    forwarded_message_id = fallback.message_id

        if forwarded_message_id is not None:
            admin_reply_targets[forwarded_message_id] = user.id
            trim_reply_targets()

    save_state()


async def handle_admin_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    if not message or not is_admin(update.effective_user.id):
        return False
    if not message.reply_to_message:
        return False

    target_id = admin_reply_targets.get(message.reply_to_message.message_id)
    if not target_id:
        return False

    try:
        await context.bot.copy_message(
            chat_id=target_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        return True
    except Exception as exc:
        print("admin reply error:", repr(exc))
        await message.reply_text("❌ تعذر إرسال الرد إلى المستخدم.")
        return True


async def activate_subscription(
    target_id: int,
    days: Optional[int],
    context: ContextTypes.DEFAULT_TYPE,
    extend: bool,
) -> None:
    current = subscriptions.get(target_id, {})
    current_expiry = str_to_dt(current.get("expires_at"))
    activated_at = now_utc()

    if days is None:
        expires_at_value: Optional[str] = None
    else:
        base = activated_at
        if extend and current_expiry and current_expiry > activated_at:
            base = current_expiry
        expires_at_value = dt_to_str(base + timedelta(days=days))

    warning_duration_days = days
    if days is not None and expires_at_value is not None:
        final_expiry = str_to_dt(expires_at_value)
        if final_expiry is not None:
            warning_duration_days = max(
                days, math.ceil((final_expiry - activated_at).total_seconds() / 86400)
            )

    subscriptions[target_id] = {
        "expires_at": expires_at_value,
        "activated_at": dt_to_str(activated_at),
        "duration_days": warning_duration_days,
        "warned_3d": False,
    }
    blocked_users.discard(target_id)
    save_state()

    operation = "تمديد" if extend else "تفعيل"
    duration_text = "غير محدد" if days is None else f"{days} يوم"
    add_admin_log(f"{operation} اشتراك المستخدم {target_id} لمدة {duration_text}")

    # حسب الطلب: الاشتراك غير المحدد يعمل بصمت بدون إشعار للمستخدم.
    if days is not None:
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "✅ تم تفعيل اشتراكك بنجاح\n"
                    f"المدة: {days} يوم\n"
                    "يمكنك الآن استخدام البوت."
                ),
            )
        except Exception as exc:
            print("subscription activation notify error:", repr(exc))


async def send_user_profile(
    message: Any,
    target_id: int,
) -> None:
    profile = known_users.get(target_id, {})
    name = profile.get("name") or "غير معروف"
    username = profile.get("username") or ""
    first_seen = str_to_dt(profile.get("first_seen"))
    last_seen = str_to_dt(profile.get("last_seen"))
    emails_count = len(user_emails.get(target_id, []))

    text = (
        "👤 معلومات المستخدم\n\n"
        f"الاسم: {name}\n"
        f"اليوزر: @{username if username else 'بدون يوزر'}\n"
        f"ID: {target_id}\n"
        f"الحالة: {subscription_status_text(target_id)}\n"
        f"عدد البريدات: {emails_count}\n"
        f"أول دخول: {first_seen.strftime('%Y-%m-%d %H:%M') if first_seen else 'غير معروف'}\n"
        f"آخر ظهور: {last_seen.strftime('%Y-%m-%d %H:%M') if last_seen else 'غير معروف'}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                make_button(
                    "⏳ تمديد", f"admin_extend_direct:{target_id}", "primary"
                ),
                make_button(
                    "❌ إلغاء", f"admin_cancel_direct:{target_id}", "danger"
                ),
            ],
            [
                make_button(
                    "📧 إدارة البريدات",
                    f"admin_manage_emails_direct:{target_id}",
                    "primary",
                )
            ],
            [make_button("🔙 البحث والبريد", "admin_section_search", "primary")],
        ]
    )
    await message.reply_text(text, reply_markup=keyboard)


def find_user(query: str) -> Optional[int]:
    target_id = parse_target_user_id(query)
    if target_id:
        if (
            target_id in known_users
            or target_id in subscriptions
            or target_id in user_emails
            or target_id in blocked_users
        ):
            return target_id
        return None

    normalized = query.strip().lstrip("@").lower()
    if not normalized:
        return None
    for user_id, data in known_users.items():
        if str(data.get("username") or "").lower() == normalized:
            return user_id
    return None


def normalize_email_query(value: str) -> Optional[str]:
    candidates = extract_emails(value)
    if candidates:
        return candidates[0]
    normalized = (value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", normalized):
        return normalized
    return None


async def send_email_search_result(message: Any, email: str) -> None:
    owner_id = email_owner.get(email)
    if not owner_id:
        await message.reply_text(
            "📧 نتيجة البحث عن البريد\n\n"
            f"البريد: {email}\n"
            "الحالة: غير مستخدم ومتاح للحجز.",
            reply_markup=back_button("admin_section_search"),
        )
        return

    profile = known_users.get(owner_id, {})
    name = profile.get("name") or "غير معروف"
    username = profile.get("username") or ""
    keyboard = InlineKeyboardMarkup(
        [
            [
                make_button(
                    "⏳ تمديد الاشتراك",
                    f"admin_extend_direct:{owner_id}",
                    "primary",
                ),
                make_button(
                    "❌ إلغاء الاشتراك",
                    f"admin_cancel_direct:{owner_id}",
                    "danger",
                ),
            ],
            [
                make_button(
                    "📬 إدارة بريداته",
                    f"admin_manage_emails_direct:{owner_id}",
                    "success",
                )
            ],
            [make_button("🔙 البحث والبريد", "admin_section_search", "primary")],
        ]
    )
    await message.reply_text(
        "📧 نتيجة البحث عن البريد\n\n"
        f"البريد: {email}\n"
        "الحالة: مستخدم حالياً\n"
        f"الاسم: {name}\n"
        f"اليوزر: @{username if username else 'بدون يوزر'}\n"
        f"ID: {owner_id}\n"
        f"الاشتراك: {subscription_status_text(owner_id)}\n"
        f"إجمالي بريداته: {len(user_emails.get(owner_id, []))}",
        reply_markup=keyboard,
    )


async def show_admin_user_emails(message: Any, target_id: int) -> None:
    emails = user_emails.get(target_id, [])
    if not emails:
        await message.reply_text(
            f"📧 المستخدم {target_id} لا يملك أي بريد.",
            reply_markup=back_button("admin_section_search"),
        )
        return

    rows: List[List[InlineKeyboardButton]] = []
    for index, email in enumerate(emails):
        rows.append(
            [
                make_button(
                    f"🗑 حذف {email}",
                    f"admin_email_delete:{target_id}:{index}",
                    "danger",
                )
            ]
        )
    rows.append([make_button("🔙 البحث والبريد", "admin_section_search", "primary")])
    await message.reply_text(
        f"📧 بريدات المستخدم {target_id}:\nاختر البريد المطلوب حذفه.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def process_admin_pending(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    uid = update.effective_user.id
    action = admin_pending.get(uid)
    if not action or not message:
        return False

    if action == "broadcast":
        admin_pending.pop(uid, None)
        success = 0
        failed = 0
        targets = [
            target_id
            for target_id in subscriptions
            if target_id != OWNER_ID and has_active_subscription(target_id)
        ]
        for target_id in targets:
            try:
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                success += 1
            except Exception:
                failed += 1
        add_admin_log(
            f"إرسال رسالة جماعية: نجح {success} وفشل {failed}"
        )
        await message.reply_text(
            f"✅ انتهى الإرسال الجماعي\nنجح: {success}\nفشل: {failed}",
            reply_markup=back_button("admin_section_communication"),
        )
        return True

    if not message.text:
        await message.reply_text("❌ أرسل ID أو نصاً صحيحاً حسب المطلوب.")
        return True

    text = message.text.strip()

    if action in {
        "add_user",
        "extend",
        "cancel_sub",
        "block",
        "unblock",
        "manage_emails",
    }:
        target_id = parse_target_user_id(text)
        if not target_id:
            await message.reply_text("❌ أرسل ID صحيحاً، أرقام فقط.")
            return True

        admin_pending.pop(uid, None)

        if action == "add_user":
            admin_pending_target[uid] = target_id
            await message.reply_text(
                f"اختر مدة اشتراك المستخدم {target_id}:",
                reply_markup=subscription_duration_keyboard("add", target_id),
            )
            return True

        if action == "extend":
            admin_pending_target[uid] = target_id
            await message.reply_text(
                f"اختر مدة التمديد للمستخدم {target_id}:",
                reply_markup=subscription_duration_keyboard("extend", target_id),
            )
            return True

        if action == "cancel_sub":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        make_button(
                            "نعم، إلغاء الاشتراك",
                            f"admin_cancel_confirm:{target_id}",
                            "danger",
                        ),
                        make_button("تراجع", "admin_menu", "primary"),
                    ]
                ]
            )
            await message.reply_text(
                f"هل تريد إلغاء اشتراك المستخدم {target_id}؟",
                reply_markup=keyboard,
            )
            return True

        if action == "block":
            blocked_users.add(target_id)
            waiting_for_name.discard(target_id)
            save_state()
            add_admin_log(f"حظر المستخدم {target_id}")
            await message.reply_text(
                f"✅ تم حظر المستخدم: {target_id}",
                reply_markup=back_button("admin_section_security"),
            )
            return True

        if action == "unblock":
            blocked_users.discard(target_id)
            save_state()
            add_admin_log(f"فك حظر المستخدم {target_id}")
            await message.reply_text(
                f"✅ تم فك حظر المستخدم: {target_id}",
                reply_markup=back_button("admin_section_security"),
            )
            return True

        if action == "manage_emails":
            await show_admin_user_emails(message, target_id)
            return True

    if action == "search_email":
        admin_pending.pop(uid, None)
        email = normalize_email_query(text)
        if not email:
            await message.reply_text(
                "❌ أرسل بريداً إلكترونياً صحيحاً، مثال: name@example.com",
                reply_markup=back_button("admin_section_search"),
            )
            return True
        await send_email_search_result(message, email)
        return True

    if action == "search":
        admin_pending.pop(uid, None)
        target_id = find_user(text)
        if target_id is None:
            await message.reply_text(
                "❌ لم أجد المستخدم.", reply_markup=back_button("admin_section_search")
            )
            return True
        await send_user_profile(message, target_id)
        return True

    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    if is_admin(uid) and await handle_admin_reply(update, context):
        return

    # غير المسموح له يبقى البوت صامتاً تماماً.
    if not has_active_subscription(uid):
        return

    last = user_last_email.get(uid)
    await update.effective_message.reply_text(
        start_text(last),
        reply_markup=main_keyboard_for(uid),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    uid = query.from_user.id
    data = query.data or ""

    # أي زر قديم عند مستخدم غير مسموح له لا يعطي أي رد ظاهر.
    if not has_active_subscription(uid):
        return

    if data == "admin_menu":
        if not is_admin(uid):
            return
        admin_pending.pop(uid, None)
        admin_pending_target.pop(uid, None)
        await query.edit_message_text("🛠️ لوحة الأدمن:", reply_markup=admin_keyboard())
        return

    if data == "admin_section_subscriptions" and is_admin(uid):
        admin_pending.pop(uid, None)
        await query.edit_message_text(
            "👑 إدارة الاشتراكات:",
            reply_markup=admin_subscriptions_keyboard(),
        )
        return

    if data == "admin_section_search" and is_admin(uid):
        admin_pending.pop(uid, None)
        await query.edit_message_text(
            "🔎 البحث والبريد:",
            reply_markup=admin_search_keyboard(),
        )
        return

    if data == "admin_section_monitor" and is_admin(uid):
        admin_pending.pop(uid, None)
        await query.edit_message_text(
            "📊 المتابعة والإحصائيات:",
            reply_markup=admin_monitor_keyboard(),
        )
        return

    if data == "admin_section_communication" and is_admin(uid):
        admin_pending.pop(uid, None)
        await query.edit_message_text(
            "📣 التواصل والتنبيهات:",
            reply_markup=admin_communication_keyboard(),
        )
        return

    if data == "admin_section_security" and is_admin(uid):
        admin_pending.pop(uid, None)
        await query.edit_message_text(
            "🛡️ الحظر والحماية:",
            reply_markup=admin_security_keyboard(),
        )
        return

    if data == "admin_add_user" and is_admin(uid):
        admin_pending[uid] = "add_user"
        await query.edit_message_text(
            "➕ أرسل الآن ID المستخدم الذي تريد إضافته:",
            reply_markup=back_button("admin_section_subscriptions"),
        )
        return

    if data == "admin_extend" and is_admin(uid):
        admin_pending[uid] = "extend"
        await query.edit_message_text(
            "⏳ أرسل ID المستخدم الذي تريد تمديد اشتراكه:",
            reply_markup=back_button("admin_section_subscriptions"),
        )
        return

    if data == "admin_cancel_sub" and is_admin(uid):
        admin_pending[uid] = "cancel_sub"
        await query.edit_message_text(
            "❌ أرسل ID المستخدم الذي تريد إلغاء اشتراكه:",
            reply_markup=back_button("admin_section_subscriptions"),
        )
        return

    if data == "admin_search" and is_admin(uid):
        admin_pending[uid] = "search"
        await query.edit_message_text(
            "🔍 أرسل ID المستخدم أو اسم المستخدم @username:",
            reply_markup=back_button("admin_section_search"),
        )
        return

    if data == "admin_search_email" and is_admin(uid):
        admin_pending[uid] = "search_email"
        await query.edit_message_text(
            "📧 أرسل البريد الإلكتروني الذي تريد معرفة صاحبه:",
            reply_markup=back_button("admin_section_search"),
        )
        return

    if data == "admin_manage_emails" and is_admin(uid):
        admin_pending[uid] = "manage_emails"
        await query.edit_message_text(
            "📧 أرسل ID المستخدم لإدارة بريداته:",
            reply_markup=back_button("admin_section_search"),
        )
        return

    if data == "admin_block" and is_admin(uid):
        admin_pending[uid] = "block"
        await query.edit_message_text(
            "🚫 أرسل ID الشخص المراد حظره:",
            reply_markup=back_button("admin_section_security"),
        )
        return

    if data == "admin_unblock" and is_admin(uid):
        admin_pending[uid] = "unblock"
        await query.edit_message_text(
            "✅ أرسل ID الشخص المراد فك حظره:",
            reply_markup=back_button("admin_section_security"),
        )
        return

    if data == "admin_broadcast" and is_admin(uid):
        admin_pending[uid] = "broadcast"
        await query.edit_message_text(
            "📢 أرسل الآن الرسالة التي تريد إرسالها لكل المشتركين الفعالين.\n"
            "يمكن أن تكون نصاً أو صورة أو ملفاً.",
            reply_markup=back_button("admin_section_communication"),
        )
        return

    if data == "admin_notifications" and is_admin(uid):
        await query.edit_message_text(
            "🔔 إعدادات التنبيهات:", reply_markup=notifications_keyboard()
        )
        return

    if data.startswith("notify_toggle:") and is_admin(uid):
        key = data.split(":", 1)[1]
        if key in notification_settings:
            notification_settings[key] = not notification_settings[key]
            save_state()
            add_admin_log(
                f"تغيير إعداد {key} إلى {notification_settings[key]}"
            )
        await query.edit_message_text(
            "🔔 إعدادات التنبيهات:", reply_markup=notifications_keyboard()
        )
        return

    if data == "admin_stats" and is_admin(uid):
        active = sum(1 for user_id in subscriptions if has_active_subscription(user_id))
        permanent = sum(
            1
            for user_id, record in subscriptions.items()
            if record.get("expires_at") is None and not is_blocked(user_id)
        )
        expired = sum(
            1
            for user_id, record in subscriptions.items()
            if record.get("expires_at") is not None
            and not has_active_subscription(user_id)
            and not is_blocked(user_id)
        )
        await query.edit_message_text(
            "📊 إحصائيات البوت\n\n"
            f"إجمالي من دخلوا البوت: {len(known_users)}\n"
            f"المشتركون الفعالون: {active}\n"
            f"اشتراك غير محدد: {permanent}\n"
            f"الاشتراكات المنتهية: {expired}\n"
            f"المحظورون: {len(blocked_users)}\n"
            f"البريدات المحجوزة: {len(email_owner)}",
            reply_markup=back_button("admin_section_monitor"),
        )
        return

    if data == "admin_users" and is_admin(uid):
        active_users = [
            user_id for user_id in subscriptions if has_active_subscription(user_id)
        ]
        lines = [f"👥 المستخدمون المسموحون: {len(active_users)}"]
        for user_id in active_users[:30]:
            lines.append(
                f"\n• {user_display_name(user_id)}\n"
                f"  ID: {user_id}\n"
                f"  {subscription_status_text(user_id)}"
            )
        if len(active_users) > 30:
            lines.append(f"\n… ويوجد {len(active_users) - 30} مستخدم إضافي.")
        await query.edit_message_text(
            "\n".join(lines), reply_markup=back_button("admin_section_monitor")
        )
        return

    if data == "admin_expiring" and is_admin(uid):
        expiring: List[int] = []
        for user_id, record in subscriptions.items():
            expires_at = str_to_dt(record.get("expires_at"))
            if not expires_at or expires_at <= now_utc() or is_blocked(user_id):
                continue
            days_left = remaining_days(user_id)
            if days_left is not None and days_left <= 3:
                expiring.append(user_id)

        lines = [f"⌛ اشتراكات تنتهي خلال 3 أيام: {len(expiring)}"]
        for user_id in expiring[:30]:
            lines.append(
                f"\n• {user_display_name(user_id)}\n"
                f"  ID: {user_id}\n"
                f"  باقي {remaining_days(user_id)} يوم"
            )
        await query.edit_message_text(
            "\n".join(lines), reply_markup=back_button("admin_section_monitor")
        )
        return

    if data.startswith("subscription:") and is_admin(uid):
        parts = data.split(":")
        if len(parts) != 4:
            return
        _, action, target_raw, duration_raw = parts
        try:
            target_id = int(target_raw)
        except ValueError:
            return
        days = None if duration_raw == "permanent" else int(duration_raw)
        await activate_subscription(
            target_id=target_id,
            days=days,
            context=context,
            extend=(action == "extend"),
        )
        await query.edit_message_text(
            f"✅ تم {'تمديد' if action == 'extend' else 'تفعيل'} اشتراك المستخدم {target_id}\n"
            f"المدة: {'غير محدد' if days is None else f'{days} يوم'}",
            reply_markup=back_button("admin_section_subscriptions"),
        )
        return

    if data.startswith("admin_extend_direct:") and is_admin(uid):
        target_id = int(data.rsplit(":", 1)[1])
        await query.edit_message_text(
            f"اختر مدة التمديد للمستخدم {target_id}:",
            reply_markup=subscription_duration_keyboard("extend", target_id),
        )
        return

    if data.startswith("admin_cancel_direct:") and is_admin(uid):
        target_id = int(data.rsplit(":", 1)[1])
        keyboard = InlineKeyboardMarkup(
            [
                [
                    make_button(
                        "نعم، إلغاء الاشتراك",
                        f"admin_cancel_confirm:{target_id}",
                        "danger",
                    ),
                    make_button("تراجع", "admin_menu", "primary"),
                ]
            ]
        )
        await query.edit_message_text(
            f"هل تريد إلغاء اشتراك المستخدم {target_id}؟",
            reply_markup=keyboard,
        )
        return

    if data.startswith("admin_cancel_confirm:") and is_admin(uid):
        target_id = int(data.rsplit(":", 1)[1])
        subscriptions.pop(target_id, None)
        waiting_for_name.discard(target_id)
        save_state()
        add_admin_log(f"إلغاء اشتراك المستخدم {target_id}")
        await query.edit_message_text(
            f"✅ تم إلغاء اشتراك المستخدم {target_id}.\n"
            "أصبح البوت صامتاً معه، ورسائله تبقى قابلة للتحويل للأدمن.",
            reply_markup=back_button("admin_section_subscriptions"),
        )
        return

    if data.startswith("admin_manage_emails_direct:") and is_admin(uid):
        target_id = int(data.rsplit(":", 1)[1])
        emails = user_emails.get(target_id, [])
        if not emails:
            await query.edit_message_text(
                f"📧 المستخدم {target_id} لا يملك أي بريد.",
                reply_markup=back_button("admin_section_search"),
            )
            return
        rows = [
            [
                make_button(
                    f"🗑 حذف {email}",
                    f"admin_email_delete:{target_id}:{index}",
                    "danger",
                )
            ]
            for index, email in enumerate(emails)
        ]
        rows.append([make_button("🔙 البحث والبريد", "admin_section_search", "primary")])
        await query.edit_message_text(
            f"📧 بريدات المستخدم {target_id}:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("admin_email_delete:") and is_admin(uid):
        _, target_raw, index_raw = data.split(":")
        target_id = int(target_raw)
        index = int(index_raw)
        emails = user_emails.get(target_id, [])
        if index >= len(emails):
            await query.edit_message_text(
                "❌ البريد لم يعد موجوداً.", reply_markup=back_button("admin_section_search")
            )
            return
        email = emails[index]
        keyboard = InlineKeyboardMarkup(
            [
                [
                    make_button(
                        "نعم، احذف",
                        f"admin_email_delete_confirm:{target_id}:{index}",
                        "danger",
                    ),
                    make_button("إلغاء", "admin_section_search", "primary"),
                ]
            ]
        )
        await query.edit_message_text(
            f"هل تريد حذف البريد؟\n{email}", reply_markup=keyboard
        )
        return

    if data.startswith("admin_email_delete_confirm:") and is_admin(uid):
        _, target_raw, index_raw = data.split(":")
        target_id = int(target_raw)
        index = int(index_raw)
        deleted = delete_email(target_id, index)
        if deleted:
            add_admin_log(f"حذف البريد {deleted} من المستخدم {target_id}")
            text = f"✅ تم حذف البريد {deleted} وأصبح متاحاً من جديد."
        else:
            text = "❌ البريد لم يعد موجوداً."
        await query.edit_message_text(text, reply_markup=back_button("admin_section_search"))
        return

    if data == "choose_name":
        waiting_for_name.add(uid)
        await query.edit_message_text(
            "✏️ اكتب الاسم الذي تريد استخدامه للبريد:",
            reply_markup=back_button("back"),
        )
        return

    if data == "random_email":
        email = make_email(random_local_part())
        while True:
            existing_owner = email_owner.get(email)
            if not existing_owner or existing_owner == uid:
                break
            email = make_email(random_local_part())

        remember_email(uid, email)
        await query.edit_message_text(
            f"تم إنشاء بريد إلكتروني جديد ✅\n\n- البريد الإلكتروني الجديد:\n`{email}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_button("back"),
        )
        return

    if data == "copy_email":
        last = user_last_email.get(uid)
        if not last:
            await query.edit_message_text(
                "❌ لم يتم إنشاء بريد بعد", reply_markup=back_button("back")
            )
            return
        await query.message.reply_text(f"`{last}`", parse_mode=ParseMode.MARKDOWN)
        return

    if data == "my_emails":
        emails = user_emails.get(uid, [])
        if not emails:
            await query.edit_message_text(
                "📁 لا يوجد بريدات تم إنشاؤها بعد.",
                reply_markup=back_button("back"),
            )
            return
        await query.edit_message_text(
            format_my_emails(emails),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=my_emails_keyboard(uid),
        )
        return

    if data.startswith("email_delete:"):
        index = int(data.split(":", 1)[1])
        emails = user_emails.get(uid, [])
        if index >= len(emails):
            await query.edit_message_text(
                "❌ البريد لم يعد موجوداً.", reply_markup=back_button("my_emails")
            )
            return
        email = emails[index]
        keyboard = InlineKeyboardMarkup(
            [
                [
                    make_button(
                        "نعم، احذف",
                        f"email_delete_confirm:{index}",
                        "danger",
                    ),
                    make_button("إلغاء", "my_emails", "primary"),
                ]
            ]
        )
        await query.edit_message_text(
            f"🗑 هل أنت متأكد من حذف البريد؟\n\n`{email}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
        return

    if data.startswith("email_delete_confirm:"):
        index = int(data.split(":", 1)[1])
        deleted = delete_email(uid, index)
        if deleted:
            await query.edit_message_text(
                f"✅ تم حذف البريد:\n`{deleted}`\n\nأصبح متاحاً لحساب آخر.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back_button("my_emails"),
            )
        else:
            await query.edit_message_text(
                "❌ البريد لم يعد موجوداً.", reply_markup=back_button("my_emails")
            )
        return

    if data == "back":
        waiting_for_name.discard(uid)
        last = user_last_email.get(uid)
        await query.edit_message_text(
            start_text(last),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard_for(uid),
            disable_web_page_preview=True,
        )
        return


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if is_admin(user.id):
        if await handle_admin_reply(update, context):
            return
        if await process_admin_pending(update, context):
            return
        return

    # كل رسالة من أي مستخدم تصل للأدمن قبل فحص الاشتراك.
    await relay_user_message(update, context)

    # المستخدم غير المسموح له لا يحصل على أي رد إطلاقاً.
    if not has_active_subscription(user.id):
        return

    if user.id not in waiting_for_name:
        return
    if not message.text:
        await message.reply_text("❌ أرسل اسماً نصياً صالحاً.")
        return

    local_part = sanitize_local_part(message.text)
    if not local_part:
        await message.reply_text("❌ الاسم غير صالح. حاول مرة أخرى:")
        return

    email = make_email(local_part)
    existing_owner = email_owner.get(email)
    if existing_owner and existing_owner != user.id:
        await message.reply_text("❌ هذا البريد محجوز لشخص آخر. اختر اسماً مختلفاً.")
        return

    waiting_for_name.discard(user.id)
    remember_email(user.id, email)
    await message.reply_text(
        f"تم إنشاء بريد إلكتروني جديد ✅\n\n- البريد الإلكتروني الجديد:\n`{email}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button("back"),
    )


async def relay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if is_admin(user.id):
        await handle_admin_reply(update, context)
        return
    await relay_user_message(update, context)


async def check_subscription_warnings() -> None:
    if not tg_app or not notification_settings.get("expiry_warning", True):
        return

    changed = False
    current_time = now_utc()
    for user_id, record in list(subscriptions.items()):
        expires_at = str_to_dt(record.get("expires_at"))
        duration_days = record.get("duration_days")
        if not expires_at or expires_at <= current_time:
            continue
        if not isinstance(duration_days, int) or duration_days <= 3:
            continue
        if record.get("warned_3d"):
            continue

        seconds_left = (expires_at - current_time).total_seconds()
        if seconds_left <= 3 * 86400:
            try:
                await tg_app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ تذكير بتجديد اشتراكك\n"
                        "باقي 3 أيام على انتهاء اشتراكك."
                    ),
                )
                record["warned_3d"] = True
                changed = True
            except Exception as exc:
                print("expiry warning error:", user_id, repr(exc))

    if changed:
        save_state()


async def subscription_watcher() -> None:
    while True:
        try:
            await check_subscription_warnings()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("subscription watcher error:", repr(exc))
        await asyncio.sleep(3600)


app = FastAPI()
tg_app: Optional[Application] = None
subscription_task: Optional[asyncio.Task[Any]] = None


@app.on_event("startup")
async def startup() -> None:
    global tg_app, subscription_task
    load_state()

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start), group=0)
    tg_app.add_handler(CallbackQueryHandler(on_button), group=0)
    tg_app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, on_message), group=0
    )
    # مجموعة ثانية حتى يتم تحويل /start وباقي الأوامر أيضاً للأدمن.
    tg_app.add_handler(MessageHandler(filters.COMMAND, relay_command), group=1)

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

    subscription_task = asyncio.create_task(subscription_watcher())

    if OWNER_ID:
        try:
            message = "✅ Bot started"
            if PUBLIC_URL:
                message += f"\nWebhook: `{PUBLIC_URL}{TG_WEBHOOK_PATH}`"
            await tg_app.bot.send_message(
                chat_id=OWNER_ID,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            print("Owner notify error:", repr(exc))


@app.on_event("shutdown")
async def shutdown() -> None:
    global subscription_task
    if subscription_task:
        subscription_task.cancel()
        try:
            await subscription_task
        except asyncio.CancelledError:
            pass
        subscription_task = None

    if tg_app:
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            print("delete_webhook error:", repr(exc))
        await tg_app.stop()
        await tg_app.shutdown()


@app.get("/")
async def root() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/health")
async def health() -> Dict[str, bool]:
    return {"ok": True}


@app.post(TG_WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Dict[str, bool]:
    if not tg_app:
        raise HTTPException(status_code=500, detail="Bot not ready")

    if TG_SECRET_TOKEN:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != TG_SECRET_TOKEN:
            raise HTTPException(status_code=403, detail="Bad telegram secret token")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.update_queue.put(update)
    return {"ok": True}


@app.post("/mailgun")
async def mailgun_inbound(request: Request) -> Dict[str, bool]:
    if not tg_app:
        return {"ok": True}

    if MAILGUN_WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != MAILGUN_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Bad mailgun secret")

    form = await request.form()

    recipient_raw = str(form.get("recipient", "") or "")
    to_raw = str(form.get("To", "") or form.get("to", "") or "")
    envelope_to_raw = str(form.get("envelope", "") or "")

    candidates_text = " , ".join(
        [recipient_raw, to_raw, envelope_to_raw]
    ).strip()
    recipients = extract_emails(candidates_text)

    sender = str(form.get("sender", "")).strip()
    subject = str(form.get("subject", "")).strip()
    body = str(form.get("stripped-text") or form.get("body-plain") or "").strip()

    print(
        "MAILGUN INBOUND recipients:",
        recipients,
        "sender:",
        sender,
        "subject:",
        subject,
    )

    if not recipients:
        return {"ok": True}

    sent_any = False
    for to_email in recipients:
        owner_id = email_owner.get(to_email)
        if not owner_id:
            print("No owner for:", to_email)
            continue

        # المحظور أو المنتهي اشتراكه لا يستقبل أي رد من البوت.
        if not has_active_subscription(owner_id):
            print("Inactive owner, skip deliver to:", owner_id, "email:", to_email)
            continue

        message = format_inbound_message(to_email, sender, subject, body)
        try:
            await tg_app.bot.send_message(
                chat_id=owner_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            sent_any = True
        except Exception as exc:
            print("Telegram send_message error:", repr(exc))

    return {"ok": True, "delivered": sent_any}
