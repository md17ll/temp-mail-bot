"""Independent, persistent trial permissions; never writes subscription records."""
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest


class TrialStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS links (
                    token TEXT PRIMARY KEY, created REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, opens INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS trials (
                    user_id INTEGER PRIMARY KEY, token TEXT NOT NULL,
                    started REAL NOT NULL, expires REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trials_token ON trials(token);
                CREATE TABLE IF NOT EXISTS trial_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)
            # Additive migration: existing links keep their original one-day duration.
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(links)")}
            for name, definition in (
                ("duration_days", "INTEGER NOT NULL DEFAULT 1"),
                ("name", "TEXT NOT NULL DEFAULT ''"),
                ("deleted", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE links ADD COLUMN {name} {definition}")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing(db)

    def create(self, now, duration_days=1, name=""):
        if type(duration_days) is not int or not 1 <= duration_days <= 3650:
            raise ValueError("Trial duration must be 1 to 3650 days")
        if not isinstance(name, str) or len(name) > 60:
            raise ValueError("Trial name must be at most 60 characters")
        token = secrets.token_hex(12)
        with self.connect() as db, db:
            db.execute("INSERT INTO links(token, created, duration_days, name) VALUES (?, ?, ?, ?)",
                       (token, now, duration_days, name))
        return token

    def support_username(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM trial_settings WHERE key='support_username'").fetchone()
        return row[0] if row else ""

    def set_support_username(self, value):
        username = value.strip().removeprefix("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", username):
            raise ValueError("Invalid Telegram username")
        with self.connect() as db, db:
            db.execute("INSERT INTO trial_settings(key, value) VALUES ('support_username', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (username,))
        return username

    def disable(self, token):
        with self.connect() as db, db:
            db.execute("UPDATE links SET enabled=0 WHERE token=?", (token,))

    def enable(self, token):
        with self.connect() as db, db:
            db.execute("UPDATE links SET enabled=1 WHERE token=? AND deleted=0", (token,))

    def delete(self, token):
        # Hide/revoke the link, but retain lifetime redemption records and active trials.
        with self.connect() as db, db:
            db.execute("UPDATE links SET deleted=1, enabled=0 WHERE token=?", (token,))

    def beneficiaries(self, token, page):
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM trials WHERE token=?", (token,)).fetchone()[0]
            page = max(0, min(page, max(0, (count - 1) // 8)))
            rows = db.execute("SELECT * FROM trials WHERE token=? ORDER BY started DESC, user_id LIMIT 8 OFFSET ?",
                              (token, page * 8)).fetchall()
        return rows, page, count

    def expiry(self, uid):
        with self.connect() as db:
            row = db.execute("SELECT expires FROM trials WHERE user_id=?", (uid,)).fetchone()
        return row[0] if row else None

    def redeem(self, token, uid, now, subscribed=False):
        # One transaction protects the lifetime user-ID limit and link-disable race.
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            link = db.execute("SELECT enabled, duration_days, deleted FROM links WHERE token=?", (token,)).fetchone()
            if not link:
                return "invalid", None
            if link["deleted"]:
                return "disabled", None
            db.execute("UPDATE links SET opens=opens+1 WHERE token=?", (token,))
            if subscribed:
                return "subscribed", None
            old = db.execute("SELECT expires FROM trials WHERE user_id=?", (uid,)).fetchone()
            if old:
                return "used", old[0]
            if not link[0]:
                return "disabled", None
            expiry = now + link["duration_days"] * 86400
            db.execute("INSERT INTO trials VALUES (?, ?, ?, ?)", (uid, token, now, expiry))
            return "started", expiry

    def links(self, page):
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM links WHERE deleted=0").fetchone()[0]
            page = max(0, min(page, max(0, (count - 1) // 8)))
            rows = db.execute("SELECT * FROM links WHERE deleted=0 ORDER BY created DESC, token LIMIT 8 OFFSET ?", (page * 8,)).fetchall()
        return rows, page, count

    def stats(self, token, now):
        with self.connect() as db:
            row = db.execute("SELECT * FROM links WHERE token=?", (token,)).fetchone()
            counts = db.execute("SELECT COUNT(*), COALESCE(SUM(expires > ?), 0) FROM trials WHERE token=?", (now, token)).fetchone()
        return row, counts[0], counts[1]


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def expiry_text(expiry, now):
    seconds = max(0, int(expiry - now))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    day_text = f"{days} يوم، " if days else ""
    return f"⏳ الوقت المتبقي: {day_text}{hours} ساعة، {minutes} دقيقة، {seconds} ثانية"


def trial_message(expiry, now, title="🎁 تجربتك المجانية"):
    if expiry <= now:
        return ("⌛ انتهت تجربتك المجانية\n\n"
                "نتمنى أن تكون استفدت من الخدمة ✉️\n"
                "لمتابعة استخدام البوت، تواصل مع الأدمن لمعرفة تفاصيل الاشتراك وتفعيله.")
    return (f"{title}\n\n"
            "استمتع بخدمة البريد المؤقت خلال فترة التجربة ✉️\n"
            "للاستمرار باستخدام البوت بعد انتهاء تجربتك، تواصل مع الأدمن "
            "لمعرفة تفاصيل الاشتراك وتفعيله 👇\n\n"
            + expiry_text(expiry, now))


def install(core, subscribed, public_active, public_status):
    store = TrialStore(Path(core.DATA_DIR) / "trial_access.sqlite3")
    original_access = core.has_active_subscription
    original_start = core.cmd_start
    original_button = core.on_button
    original_keyboard = core.main_keyboard_for
    button = core.make_button

    def now():
        return core.now_utc().timestamp()

    def trial_markup(back=False):
        rows = []
        username = store.support_username()
        if username:
            rows.append([InlineKeyboardButton("💬 التواصل مع الأدمن للاشتراك",
                                               url=f"https://t.me/{username}")])
        if back:
            rows.append([button("🔙 عودة", "back", "primary")])
        return InlineKeyboardMarkup(rows) if rows else None

    def access(uid):
        if original_access(uid):
            return True
        if core.is_blocked(uid):
            return False
        expiry = store.expiry(uid)
        return bool(expiry and expiry > now())

    def keyboard(uid):
        rows = [list(row) for row in original_keyboard(uid).inline_keyboard]
        if not core.is_blocked(uid) and not subscribed(uid):
            if public_active():
                rows.append([button("⏳ وصول مجاني مؤقت", "public_access_info", "danger")])
            expiry = store.expiry(uid)
            if expiry and expiry > now():
                rows.append([button("🎁 وقت تجربتك", "trial_access_info", "danger")])
        return InlineKeyboardMarkup(rows)

    async def start(update, context):
        args = context.args or []
        if not args or not args[0].startswith("trial_"):
            return await original_start(update, context)
        uid = update.effective_user.id
        if core.is_blocked(uid):
            return
        token = args[0][6:]
        if not re.fullmatch(r"[0-9a-f]{24}", token):
            return
        result, expiry = store.redeem(token, uid, now(), subscribed(uid))
        if result == "started":
            await update.effective_message.reply_text(
                trial_message(expiry, now(), "✅ بدأت تجربتك المجانية"),
                reply_markup=trial_markup())
        elif result == "used":
            if expiry > now():
                await update.effective_message.reply_text(
                    trial_message(expiry, now(), "🎁 تجربتك مفعّلة مسبقاً؛ إعادة فتح الرابط لا تمددها."),
                    reply_markup=trial_markup())
            elif not access(uid):
                return  # Preserve silence after all access has expired.
        elif result == "subscribed":
            await update.effective_message.reply_text("✅ اشتراكك فعال ولم يتغير. لم تُستهلك فرصتك في التجربة.")
        elif not access(uid):
            await update.effective_message.reply_text("❌ رابط التجربة معطل أو غير صالح.")
            return
        await original_start(update, context)

    async def on_button(update, context):
        query = update.callback_query
        if not query:
            return
        data = query.data or ""
        uid = query.from_user.id
        if data in {"public_access_info", "trial_access_info"}:
            await query.answer()
            if core.is_blocked(uid) or subscribed(uid):
                return
            if data == "public_access_info":
                if not access(uid):
                    return
                text = "⏳ وصول مجاني مؤقت\n\n" + public_status()
                markup = core.back_button("back")
            else:
                expiry = store.expiry(uid)
                if expiry is None:
                    return
                text = trial_message(expiry, now())
                markup = trial_markup(back=access(uid))
            try:
                await query.edit_message_text(text, reply_markup=markup)
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
            return
        if await management.on_button(update, context):
            return
        await original_button(update, context)

    from trial_management import TrialManagement
    management = TrialManagement(core, store, timestamp)

    core.has_active_subscription = access
    core.cmd_start = start
    core.on_button = on_button
    core.main_keyboard_for = keyboard
    return store
