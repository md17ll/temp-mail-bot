"""Independent, persistent trial permissions; never writes subscription records."""
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardMarkup
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
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing(db)

    def create(self, now):
        token = secrets.token_hex(12)
        with self.connect() as db, db:
            db.execute("INSERT INTO links(token, created) VALUES (?, ?)", (token, now))
        return token

    def disable(self, token):
        with self.connect() as db, db:
            db.execute("UPDATE links SET enabled=0 WHERE token=?", (token,))

    def expiry(self, uid):
        with self.connect() as db:
            row = db.execute("SELECT expires FROM trials WHERE user_id=?", (uid,)).fetchone()
        return row[0] if row else None

    def redeem(self, token, uid, now, subscribed=False):
        # One transaction protects the lifetime user-ID limit and link-disable race.
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            link = db.execute("SELECT enabled FROM links WHERE token=?", (token,)).fetchone()
            if not link:
                return "invalid", None
            db.execute("UPDATE links SET opens=opens+1 WHERE token=?", (token,))
            if subscribed:
                return "subscribed", None
            old = db.execute("SELECT expires FROM trials WHERE user_id=?", (uid,)).fetchone()
            if old:
                return "used", old[0]
            if not link[0]:
                return "disabled", None
            expiry = now + 86400
            db.execute("INSERT INTO trials VALUES (?, ?, ?, ?)", (uid, token, now, expiry))
            return "started", expiry

    def links(self, page):
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            page = max(0, min(page, max(0, (count - 1) // 8)))
            rows = db.execute("SELECT * FROM links ORDER BY created DESC, token LIMIT 8 OFFSET ?", (page * 8,)).fetchall()
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
    return (f"📅 تنتهي: {timestamp(expiry)}\n"
            f"⏳ المتبقي: {days} يوم، {hours} ساعة، {minutes} دقيقة، {seconds} ثانية")


def install(core, subscribed, public_active, public_status):
    store = TrialStore(Path(core.DATA_DIR) / "trial_access.sqlite3")
    original_access = core.has_active_subscription
    original_start = core.cmd_start
    original_button = core.on_button
    original_keyboard = core.main_keyboard_for
    button = core.make_button

    def now():
        return core.now_utc().timestamp()

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
                rows.append([button("🎁 وقت انتهاء تجربتي", "trial_access_info", "primary")])
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
                "✅ بدأت تجربتك المجانية\n\n" + expiry_text(expiry, now()))
        elif result == "used":
            if expiry > now():
                await update.effective_message.reply_text(
                    "🎁 تجربتك مفعّلة مسبقاً؛ إعادة فتح الرابط لا تمددها.\n\n" + expiry_text(expiry, now()))
            elif not access(uid):
                return  # Preserve silence after all access has expired.
        elif result == "subscribed":
            await update.effective_message.reply_text("✅ اشتراكك فعال ولم يتغير. لم تُستهلك فرصتك في التجربة.")
        elif not access(uid):
            await update.effective_message.reply_text("❌ رابط التجربة معطل أو غير صالح.")
            return
        await original_start(update, context)

    async def edit_panel(query, text, **kwargs):
        try:
            await query.edit_message_text(text, **kwargs)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def show_list(query, page):
        links, page, count = store.links(page)
        rows = [[button("➕ إنشاء رابط تجربة ليوم واحد", "trial_admin_create", "success")]]
        for link in links:
            status = "🟢" if link["enabled"] else "🔴"
            rows.append([button(f'{status} {timestamp(link["created"])} · {link["token"][:6]}',
                                f'trial_admin_view:{link["token"]}', "primary")])
        nav = []
        if page:
            nav.append(button("⬅️ السابق", f"trial_admin_list:{page-1}", "primary"))
        if (page + 1) * 8 < count:
            nav.append(button("التالي ➡️", f"trial_admin_list:{page+1}", "primary"))
        if nav:
            rows.append(nav)
        rows.append([button("🔙 لوحة الأدمن", "admin_menu", "primary")])
        await edit_panel(query,
            "🎁 روابط التجربة\n\nمدة التجربة 24 ساعة من لحظة التفعيل، مرة واحدة لكل حساب عبر جميع الروابط.\n"
            "تعطيل الرابط يمنع مستخدمين جدد فقط. الاشتراكات والفتح العام مستقلان عن التجربة.\n\n"
            f"عدد الروابط: {count} — الصفحة {page+1}", reply_markup=InlineKeyboardMarkup(rows))

    async def show_link(query, context, token):
        link, total, active = store.stats(token, now())
        if not link:
            await show_list(query, 0)
            return
        bot_user = await context.bot.get_me()
        rows = [[button("🔄 تحديث الإحصائيات", f"trial_admin_view:{token}", "primary")]]
        if link["enabled"]:
            rows.append([button("🔴 تعطيل الرابط", f"trial_admin_disable:{token}", "danger")])
        rows.append([button("🔙 روابط التجربة", "trial_admin_list:0", "primary")])
        await edit_panel(query,
            "🎁 تفاصيل رابط التجربة\n\n"
            f"https://t.me/{bot_user.username}?start=trial_{token}\n\n"
            f"الحالة: {'فعال' if link['enabled'] else 'معطل'}\n"
            f"تاريخ الإنشاء: {timestamp(link['created'])}\n"
            f"مرات وصول /start عبر الرابط: {link['opens']}\n"
            f"الأشخاص الذين فعّلوا التجربة: {total}\n"
            f"التجارب النشطة: {active}\nالتجارب المنتهية: {total-active}\n\n"
            "المدة: 24 ساعة لكل شخص من تفعيله. تعطيل الرابط لا يوقف التجارب المفعّلة.",
            reply_markup=InlineKeyboardMarkup(rows), disable_web_page_preview=True)

    async def on_button(update, context):
        query = update.callback_query
        if not query:
            return
        data = query.data or ""
        uid = query.from_user.id
        if data in {"public_access_info", "trial_access_info"}:
            await query.answer()
            if not access(uid) or core.is_blocked(uid) or subscribed(uid):
                return
            if data == "public_access_info":
                text = "⏳ وصول مجاني مؤقت\n\n" + public_status()
            else:
                expiry = store.expiry(uid)
                if not expiry or expiry <= now():
                    return
                text = "🎁 تجربتك المجانية\n\n" + expiry_text(expiry, now())
            await query.edit_message_text(text, reply_markup=core.back_button("back"))
            return
        if not data.startswith("trial_admin_"):
            return await original_button(update, context)
        await query.answer()
        if not core.is_admin(uid):
            return
        core.admin_pending.pop(uid, None)
        core.admin_pending_target.pop(uid, None)
        if data.startswith("trial_admin_list:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                page = 0
            await show_list(query, page)
        elif data == "trial_admin_create":
            # Resolve bot identity before persisting a link that must be displayed.
            await context.bot.get_me()
            token = store.create(now())
            await show_link(query, context, token)
        elif data.startswith(("trial_admin_view:", "trial_admin_disable:")):
            token = data.split(":", 1)[1]
            if data.startswith("trial_admin_disable:"):
                store.disable(token)
            await show_link(query, context, token)

    core.has_active_subscription = access
    core.cmd_start = start
    core.on_button = on_button
    core.main_keyboard_for = keyboard
    return store
