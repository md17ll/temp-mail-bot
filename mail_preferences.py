import sqlite3
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardMarkup


MODES = {"all", "codes", "codes_links"}


class PreferenceStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS user_preferences ("
                "user_id INTEGER PRIMARY KEY, receiving INTEGER NOT NULL DEFAULT 1, "
                "mode TEXT NOT NULL DEFAULT 'all', updated_at REAL NOT NULL DEFAULT 0)"
            )

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10)

    def feature_enabled(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='feature_enabled'").fetchone()
        return row is None or row[0] == "1"

    def set_feature_enabled(self, enabled: bool) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES('feature_enabled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if enabled else "0",),
            )

    def get(self, user_id: int):
        with self._connect() as db:
            row = db.execute(
                "SELECT receiving,mode FROM user_preferences WHERE user_id=?", (int(user_id),)
            ).fetchone()
        if not row:
            return True, "all"
        mode = row[1] if row[1] in MODES else "all"
        return bool(row[0]), mode

    def set_receiving(self, user_id: int, receiving: bool) -> None:
        _, mode = self.get(user_id)
        self._save(user_id, receiving, mode)

    def set_mode(self, user_id: int, mode: str) -> None:
        if mode not in MODES:
            return
        receiving, _ = self.get(user_id)
        self._save(user_id, receiving, mode)

    def _save(self, user_id: int, receiving: bool, mode: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO user_preferences(user_id,receiving,mode,updated_at) "
                "VALUES(?,?,?,strftime('%s','now')) ON CONFLICT(user_id) DO UPDATE SET "
                "receiving=excluded.receiving,mode=excluded.mode,updated_at=excluded.updated_at",
                (int(user_id), 1 if receiving else 0, mode),
            )


_core = None
store: Optional[PreferenceStore] = None


def feature_enabled() -> bool:
    return bool(store and store.feature_enabled())


def delivery_policy(user_id: int):
    # Global OFF means the original bot behavior: every authorized email is delivered in full.
    if not store or not store.feature_enabled():
        return True, "all"
    return store.get(user_id)


def _mode_name(mode: str) -> str:
    return {"all": "📩 كل الرسائل", "codes": "🔐 الأكواد فقط", "codes_links": "🔗 الأكواد وروابط التفعيل"}.get(mode, "📩 كل الرسائل")


def _panel_text(user_id: int) -> str:
    receiving, mode = store.get(user_id)
    return (
        "⚙️ إعدادات الإشعارات\n\n"
        "تحكّم بما يصلك من رسائل بريدك المؤقت. الإعداد الافتراضي هو كل الرسائل، "
        "ويمكنك اختيار إرسال رموز التحقق فقط أو رموز التحقق مع روابط التفعيل واستعادة كلمة المرور.\n\n"
        "الإيقاف يوقف وصول رسائل البريد فقط، ولا يحذف بريداتك ولا يوقف اشتراكك. "
        "الرسائل التي تصل أثناء الإيقاف لا تُرسل لاحقًا عند إعادة التشغيل.\n\n"
        f"📡 الاستقبال: {'مفعّل' if receiving else 'متوقف'}\n"
        f"🎯 الوضع الحالي: {_mode_name(mode)}"
    )


def _panel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    receiving, mode = store.get(user_id)
    rows = [[
        _core.make_button("✅ تشغيل الاستقبال" if receiving else "▶️ تشغيل الاستقبال", "mail_pref_receive:1", "success"),
        _core.make_button("⏸ إيقاف الاستقبال" if receiving else "🔴 الاستقبال متوقف", "mail_pref_receive:0", "danger"),
    ]]
    choices = [
        ("all", "📩 كل الرسائل"),
        ("codes", "🔐 الأكواد فقط"),
        ("codes_links", "🔗 الأكواد + الروابط"),
    ]
    for value, label in choices:
        rows.append([_core.make_button(("✅ " if mode == value else "") + label, f"mail_pref_mode:{value}", "success" if mode == value else "primary")])
    rows.append([_core.make_button("🔙 عودة", "back", "primary")])
    return InlineKeyboardMarkup(rows)


def _mode_text(mode: str) -> str:
    details = {
        "all": "يرسل البريد كاملًا كما يصل: المرسل والعنوان والنص، مع زر نسخ رمز التحقق عند اكتشافه.",
        "codes": "يرسل رموز التحقق وتسجيل الدخول فقط بشكل مختصر، ولا يرسل نص الرسالة الكامل أو روابط الإعلانات.",
        "codes_links": "يرسل رموز التحقق، وروابط تفعيل الحساب واستعادة كلمة المرور وتسجيل الدخول فقط، بدون محتوى الرسالة الكامل.",
    }
    return (
        f"{_mode_name(mode)}\n\n{details[mode]}\n\n"
        "يعمل المستكشف محليًا بقواعد ذكية متعددة اللغات، ويتجاهل روابط إلغاء الاشتراك والإعلانات وأكواد الخصم قدر الإمكان. "
        "قد تختلف قوالب البريد بين المواقع، لذلك يمكن العودة إلى «كل الرسائل» في أي وقت لضمان رؤية الرسالة كاملة.\n\n"
        "✅ تم حفظ هذا الاختيار لكل بريداتك."
    )


def _admin_text() -> str:
    enabled = store.feature_enabled()
    return (
        "⚙️ إعدادات إشعارات المستخدمين\n\n"
        "عند التشغيل: يظهر زر الإعدادات للمستخدمين، ويحترم البوت اختيار كل مستخدم بين كل الرسائل أو الأكواد أو الأكواد والروابط.\n\n"
        "عند التعطيل: يختفي زر الإعدادات ويرجع البوت لإرسال كل رسالة كاملة للجميع بالنظام الطبيعي، "
        "حتى لو كان مستخدم قد أوقف الاستقبال. تبقى اختياراتهم محفوظة وتعود عند تشغيل الميزة مجددًا.\n\n"
        f"الحالة الحالية: {'🟢 مفعّلة' if enabled else '🔴 معطّلة — الإرسال العام الطبيعي'}"
    )


def _admin_keyboard() -> InlineKeyboardMarkup:
    enabled = store.feature_enabled()
    return InlineKeyboardMarkup([
        [_core.make_button("🔴 تعطيل الميزة" if enabled else "🟢 تشغيل الميزة", f"mail_pref_admin_set:{0 if enabled else 1}", "danger" if enabled else "success")],
        [_core.make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
    ])


def install(core) -> PreferenceStore:
    global _core, store
    _core = core
    store = PreferenceStore(Path(core.DATA_DIR) / "mail_preferences.sqlite3")
    original_keyboard = core.main_keyboard_for
    original_on_button = core.on_button

    def keyboard(user_id: int):
        markup = original_keyboard(user_id)
        if not store.feature_enabled():
            return markup
        rows = [list(row) for row in markup.inline_keyboard]
        insert_at = len(rows)
        if rows and any(getattr(btn, "callback_data", "") == "admin_menu" for btn in rows[-1]):
            insert_at -= 1
        rows.insert(insert_at, [core.make_button("⚙️ إعدادات الإشعارات", "mail_pref_home", "primary")])
        return InlineKeyboardMarkup(rows)

    async def on_button(update, context):
        query = update.callback_query
        data = (query.data or "") if query else ""
        if not data.startswith("mail_pref_"):
            await original_on_button(update, context)
            return
        await query.answer()
        uid = query.from_user.id

        if data.startswith("mail_pref_admin"):
            if not core.is_admin(uid):
                return
            if data.startswith("mail_pref_admin_set:"):
                store.set_feature_enabled(data.endswith(":1"))
            await query.edit_message_text(_admin_text(), reply_markup=_admin_keyboard())
            return

        if not store.feature_enabled():
            await query.edit_message_text(
                "⚙️ إعدادات الإشعارات غير متاحة حاليًا.\n\nالإرسال يعمل بالنظام الطبيعي: كل الرسائل تصل كاملة.",
                reply_markup=core.back_button("back"),
            )
            return
        if not core.has_active_subscription(uid):
            return
        if data.startswith("mail_pref_receive:"):
            store.set_receiving(uid, data.endswith(":1"))
        elif data.startswith("mail_pref_mode:"):
            mode = data.split(":", 1)[1]
            store.set_mode(uid, mode)
            await query.edit_message_text(_mode_text(mode), reply_markup=_panel_keyboard(uid))
            return
        await query.edit_message_text(_panel_text(uid), reply_markup=_panel_keyboard(uid))

    core.main_keyboard_for = keyboard
    core.on_button = on_button
    return store
