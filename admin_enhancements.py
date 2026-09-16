import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardMarkup


_core = None
_original_has_active_subscription = None
_original_on_button = None
_original_process_admin_pending = None

PUBLIC_ACCESS_FILE: Optional[Path] = None
public_access_until: Optional[str] = None
MEMBERS_PER_PAGE = 8


def _load_public_access() -> None:
    global public_access_until
    if not PUBLIC_ACCESS_FILE:
        return
    try:
        if not PUBLIC_ACCESS_FILE.exists():
            public_access_until = None
            return
        data = json.loads(PUBLIC_ACCESS_FILE.read_text(encoding="utf-8"))
        value = data.get("public_access_until")
        public_access_until = str(value) if value else None
    except Exception as exc:
        print("public access load error:", repr(exc))
        public_access_until = None


def _save_public_access() -> None:
    if not PUBLIC_ACCESS_FILE:
        return
    try:
        PUBLIC_ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = PUBLIC_ACCESS_FILE.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps({"public_access_until": public_access_until}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(PUBLIC_ACCESS_FILE)
    except Exception as exc:
        print("public access save error:", repr(exc))


def _public_access_expiry():
    if not public_access_until:
        return None
    return _core.str_to_dt(public_access_until)


def public_access_active() -> bool:
    expiry = _public_access_expiry()
    return bool(expiry and expiry > _core.now_utc())


def enhanced_has_active_subscription(user_id: int) -> bool:
    # Keep the original admin bypass and block behavior intact.
    if _core.is_admin(user_id):
        return True
    if _core.is_blocked(user_id):
        return False

    # Public access is an extra permission layer only. It never changes,
    # pauses, extends, or deletes any existing subscription.
    if public_access_active():
        return True

    return _original_has_active_subscription(user_id)


def _public_access_status_text() -> str:
    expiry = _public_access_expiry()
    if not expiry or expiry <= _core.now_utc():
        return "🔒 مغلق للعامة — الاستخدام لأصحاب الاشتراكات أو التجارب الفعالة"

    remaining = expiry - _core.now_utc()
    seconds = max(0, int(remaining.total_seconds()))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    if days:
        remaining_text = f"{days} يوم و {hours} ساعة"
    else:
        minutes = max(1, (seconds % 3600) // 60)
        remaining_text = f"{hours} ساعة و {minutes} دقيقة"

    return (
        "🔓 مفتوح للجميع\n"
        f"⏳ المتبقي تقريباً: {remaining_text}\n"
        f"📅 ينتهي: {expiry.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_core.make_button("👑 إدارة الاشتراكات", "admin_section_subscriptions", "success")],
            [_core.make_button("📊 الإحصائيات والأعضاء", "admin_section_monitor", "primary")],
            [
                _core.make_button("🔎 البحث والبريد", "admin_section_search", "primary"),
                _core.make_button("📣 التواصل", "admin_section_communication", "success"),
            ],
            [_core.make_button("🛡️ الحماية", "admin_section_security", "danger")],
            [_core.make_button("🎁 إدارة التجارب", "trial_admin_home", "primary")],
            [_core.make_button("🔙 عودة", "back", "primary")],
        ]
    )


def admin_subscriptions_keyboard() -> InlineKeyboardMarkup:
    public_label = "🌍 فتح البوت للجميع"
    if public_access_active():
        public_label = "🌍 الوصول العام — مفتوح"

    return InlineKeyboardMarkup(
        [
            [_core.make_button("➕ إضافة مستخدم", "admin_add_user", "success")],
            [
                _core.make_button("⏳ تمديد اشتراك", "admin_extend", "primary"),
                _core.make_button("👥 المشتركون", "admin_users", "primary"),
            ],
            [
                _core.make_button("⌛ قرب الانتهاء", "admin_expiring", "primary"),
                _core.make_button("❌ إلغاء اشتراك", "admin_cancel_sub", "danger"),
            ],
            [_core.make_button(public_label, "admin_public_access", "success" if public_access_active() else "primary")],
            [_core.make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def admin_search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_core.make_button("🔍 بحث عن مستخدم", "admin_search", "primary")],
            [
                _core.make_button("📧 بحث عن بريد", "admin_search_email", "success"),
                _core.make_button("📬 إدارة البريدات", "admin_manage_emails", "primary"),
            ],
            [_core.make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def admin_monitor_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_core.make_button("📧 الأكثر بريدات", "mail_admin_top:0", "primary")],
            [
                _core.make_button("👥 الأعضاء", "admin_members_page:0", "success"),
                _core.make_button("🔎 بحث عن عضو", "admin_member_search", "primary"),
            ],
            [
                _core.make_button("🔄 تحديث الإحصائيات", "admin_stats", "primary"),
                _core.make_button("⌛ قرب الانتهاء", "admin_expiring", "primary"),
            ],
            [_core.make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def admin_communication_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_core.make_button("📢 رسالة جماعية", "admin_broadcast", "success")],
            [_core.make_button("🔔 إعدادات التنبيهات", "admin_notifications", "primary")],
            [_core.make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def admin_security_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _core.make_button("🚫 حظر شخص", "admin_block", "danger"),
                _core.make_button("✅ فك الحظر", "admin_unblock", "success"),
            ],
            [_core.make_button("🔙 لوحة الأدمن", "admin_menu", "primary")],
        ]
    )


def _statistics_text() -> str:
    active = sum(1 for user_id in _core.subscriptions if _original_has_active_subscription(user_id))
    permanent = sum(
        1
        for user_id, record in _core.subscriptions.items()
        if record.get("expires_at") is None and not _core.is_blocked(user_id)
    )
    expired = sum(
        1
        for user_id, record in _core.subscriptions.items()
        if record.get("expires_at") is not None
        and not _original_has_active_subscription(user_id)
        and not _core.is_blocked(user_id)
    )

    return (
        "📊 الإحصائيات والأعضاء\n\n"
        "من هنا يمكنك متابعة أرقام البوت، عرض جميع الأعضاء واحداً واحداً، "
        "أو البحث عن عضو بالـ ID أو اسم المستخدم.\n\n"
        f"👥 إجمالي الأعضاء: {len(_core.known_users)}\n"
        f"✅ المشتركون الفعالون: {active}\n"
        f"♾ اشتراك غير محدد: {permanent}\n"
        f"⌛ الاشتراكات المنتهية: {expired}\n"
        f"🚫 المحظورون: {len(_core.blocked_users)}\n"
        f"📧 البريدات المحجوزة: {len(_core.email_owner)}\n\n"
        f"🌍 الوصول العام:\n{_public_access_status_text()}"
    )


def _member_label(user_id: int) -> str:
    data = _core.known_users.get(user_id, {})
    name = str(data.get("name") or "بدون اسم").strip()
    username = str(data.get("username") or "").strip()
    suffix = f" | @{username}" if username else ""
    label = f"👤 {name}{suffix}"
    return label[:60]


def _sorted_member_ids() -> List[int]:
    def last_seen_value(user_id: int) -> str:
        return str(_core.known_users.get(user_id, {}).get("last_seen") or "")

    return sorted(_core.known_users.keys(), key=last_seen_value, reverse=True)


def members_keyboard(page: int) -> InlineKeyboardMarkup:
    member_ids = _sorted_member_ids()
    total_pages = max(1, math.ceil(len(member_ids) / MEMBERS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * MEMBERS_PER_PAGE
    current = member_ids[start : start + MEMBERS_PER_PAGE]

    rows = [
        [_core.make_button(_member_label(user_id), f"admin_member:{user_id}")]
        for user_id in current
    ]

    nav = []
    if page > 0:
        nav.append(_core.make_button("⬅️ السابق", f"admin_members_page:{page - 1}", "primary"))
    if page < total_pages - 1:
        nav.append(_core.make_button("التالي ➡️", f"admin_members_page:{page + 1}", "primary"))
    if nav:
        rows.append(nav)

    rows.append([_core.make_button("🔙 الإحصائيات والأعضاء", "admin_section_monitor", "primary")])
    return InlineKeyboardMarkup(rows)


def member_profile_text(target_id: int) -> str:
    profile = _core.known_users.get(target_id, {})
    name = profile.get("name") or "غير معروف"
    username = profile.get("username") or ""
    first_seen = _core.str_to_dt(profile.get("first_seen"))
    last_seen = _core.str_to_dt(profile.get("last_seen"))

    return (
        "👤 معلومات العضو\n\n"
        f"الاسم: {name}\n"
        f"اليوزر: @{username if username else 'بدون يوزر'}\n"
        f"ID: {target_id}\n"
        f"الحالة: {_core.subscription_status_text(target_id)}\n"
        f"عدد البريدات: {len(_core.user_emails.get(target_id, []))}\n"
        f"أول دخول: {first_seen.strftime('%Y-%m-%d %H:%M') if first_seen else 'غير معروف'}\n"
        f"آخر ظهور: {last_seen.strftime('%Y-%m-%d %H:%M') if last_seen else 'غير معروف'}"
    )


def member_profile_keyboard(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _core.make_button("⏳ تمديد", f"admin_extend_direct:{target_id}", "primary"),
                _core.make_button("❌ إلغاء الاشتراك", f"admin_cancel_direct:{target_id}", "danger"),
            ],
            [_core.make_button("📧 إدارة البريدات", f"admin_manage_emails_direct:{target_id}", "success")],
            [_core.make_button("🔙 الأعضاء", "admin_members_page:0", "primary")],
        ]
    )


def public_access_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            _core.make_button("🟢 يوم", "admin_public_set:1", "success"),
            _core.make_button("🟢 7 أيام", "admin_public_set:7", "success"),
        ],
        [
            _core.make_button("🟢 شهر", "admin_public_set:30", "success"),
            _core.make_button("🟢 سنة", "admin_public_set:365", "success"),
        ],
    ]
    if public_access_active():
        rows.append([_core.make_button("🔴 إغلاق الوصول العام الآن", "admin_public_close", "danger")])
    rows.append([_core.make_button("🔙 إدارة الاشتراكات", "admin_section_subscriptions", "primary")])
    return InlineKeyboardMarkup(rows)


async def _show_admin_menu(query) -> None:
    await query.edit_message_text(
        "👋 مرحباً بك يا أدمن\n\n"
        "🛠️ هذه لوحة التحكم الخاصة بك.\n"
        "اختر القسم المطلوب من الأزرار التالية. كل قسم يحتوي على وصف واضح وأزرار رجوع للتنقل بسهولة.",
        reply_markup=admin_keyboard(),
    )


async def enhanced_on_button(update, context) -> None:
    query = update.callback_query
    if not query:
        return

    uid = query.from_user.id
    data = query.data or ""

    custom_callbacks = (
        data == "admin_menu"
        or data.startswith("admin_section_")
        or data == "admin_stats"
        or data.startswith("admin_members_page:")
        or data.startswith("admin_member:")
        or data == "admin_member_search"
        or data == "admin_public_access"
        or data.startswith("admin_public_set:")
        or data == "admin_public_close"
        or data == "admin_notifications"
    )

    if not custom_callbacks:
        await _original_on_button(update, context)
        return

    await query.answer()
    if not _core.is_admin(uid):
        return

    if data == "admin_menu":
        _core.admin_pending.pop(uid, None)
        _core.admin_pending_target.pop(uid, None)
        await _show_admin_menu(query)
        return

    if data == "admin_section_subscriptions":
        _core.admin_pending.pop(uid, None)
        await query.edit_message_text(
            "👑 إدارة الاشتراكات\n\n"
            "من هنا يمكنك إضافة المشتركين، تمديد أو إلغاء الاشتراكات، متابعة قرب الانتهاء، "
            "وفتح البوت للجميع لمدة محددة بدون إيقاف أو تعديل اشتراك أي شخص.",
            reply_markup=admin_subscriptions_keyboard(),
        )
        return

    if data == "admin_section_search":
        _core.admin_pending.pop(uid, None)
        await query.edit_message_text(
            "🔎 البحث والبريد\n\n"
            "استخدم هذا القسم للبحث عن مستخدم، معرفة صاحب بريد معيّن، أو إدارة البريدات المحجوزة.",
            reply_markup=admin_search_keyboard(),
        )
        return

    if data == "admin_section_monitor" or data == "admin_stats":
        _core.admin_pending.pop(uid, None)
        await query.edit_message_text(_statistics_text(), reply_markup=admin_monitor_keyboard())
        return

    if data == "admin_section_communication":
        _core.admin_pending.pop(uid, None)
        await query.edit_message_text(
            "📣 التواصل والتنبيهات\n\n"
            "من هنا يمكنك إرسال رسالة جماعية للمشتركين والتحكم بتنبيهات دخول المستخدمين وتحويل رسائلهم للأدمن وتنبيه انتهاء الاشتراك.",
            reply_markup=admin_communication_keyboard(),
        )
        return

    if data == "admin_section_security":
        _core.admin_pending.pop(uid, None)
        await query.edit_message_text(
            "🛡️ الحماية\n\n"
            "استخدم أزرار هذا القسم لحظر مستخدم أو فك الحظر عنه. المستخدم المحظور يبقى محظوراً حتى عند فتح البوت للجميع.",
            reply_markup=admin_security_keyboard(),
        )
        return

    if data == "admin_notifications":
        await query.edit_message_text(
            "🔔 إعدادات التنبيهات\n\n"
            "فعّل أو عطّل إشعارات دخول المستخدمين، تحويل رسائلهم للأدمن، وتنبيه المستخدم قبل انتهاء اشتراكه.",
            reply_markup=_core.notifications_keyboard(),
        )
        return

    if data.startswith("admin_members_page:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except ValueError:
            page = 0
        member_ids = _sorted_member_ids()
        total_pages = max(1, math.ceil(len(member_ids) / MEMBERS_PER_PAGE))
        page = max(0, min(page, total_pages - 1))
        await query.edit_message_text(
            "👥 الأعضاء\n\n"
            "كل عضو يظهر بزر مستقل. اضغط على أي عضو لعرض معلوماته وإدارة اشتراكه أو بريداته.\n\n"
            f"العدد: {len(member_ids)} — الصفحة {page + 1}/{total_pages}",
            reply_markup=members_keyboard(page),
        )
        return

    if data.startswith("admin_member:"):
        try:
            target_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            return
        await query.edit_message_text(
            member_profile_text(target_id),
            reply_markup=member_profile_keyboard(target_id),
        )
        return

    if data == "admin_member_search":
        _core.admin_pending[uid] = "member_search"
        await query.edit_message_text(
            "🔎 بحث عن عضو\n\n"
            "أرسل الآن ID العضو أو اسم المستخدم بالشكل @username، وسأعرض لك بياناته.",
            reply_markup=_core.back_button("admin_section_monitor"),
        )
        return

    if data == "admin_public_access":
        await query.edit_message_text(
            "🌍 فتح البوت للجميع\n\n"
            "هذا الخيار يضيف وصولاً مؤقتاً لغير المشتركين فقط.\n"
            "اشتراكات الأشخاص تستمر بوقتها الطبيعي ولا تتوقف ولا تُمدد ولا تُحذف.\n"
            "وعند انتهاء المدة، يستمر وصول أصحاب الاشتراكات أو التجارب الفعالة.\n\n"
            f"الحالة الحالية:\n{_public_access_status_text()}\n\n"
            "اختر مدة الفتح:",
            reply_markup=public_access_keyboard(),
        )
        return

    if data.startswith("admin_public_set:"):
        try:
            days = int(data.rsplit(":", 1)[1])
        except ValueError:
            return
        if days not in {1, 7, 30, 365}:
            return
        global public_access_until
        public_access_until = _core.dt_to_str(_core.now_utc() + timedelta(days=days))
        _save_public_access()
        await query.edit_message_text(
            "✅ تم فتح البوت للجميع\n\n"
            f"المدة: {days} يوم\n"
            "الاشتراكات الحالية مستمرة بشكل طبيعي ولن تتأثر.\n\n"
            f"{_public_access_status_text()}",
            reply_markup=public_access_keyboard(),
        )
        return

    if data == "admin_public_close":
        public_access_until = None
        _save_public_access()
        await query.edit_message_text(
            "🔒 تم إغلاق الوصول العام.\n\n"
            "أصحاب الاشتراكات الفعالة يستمرون باستخدام البوت بشكل طبيعي، "
            "وأصحاب التجارب الفعالة يكملون مدتهم؛ غير ذلك يعود البوت صامتاً.",
            reply_markup=public_access_keyboard(),
        )
        return


async def enhanced_process_admin_pending(update, context) -> bool:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return False

    uid = user.id
    if _core.admin_pending.get(uid) != "member_search":
        return await _original_process_admin_pending(update, context)

    if not _core.is_admin(uid):
        _core.admin_pending.pop(uid, None)
        return True

    if not message.text:
        await message.reply_text("❌ أرسل ID أو @username للعضو.")
        return True

    query_text = message.text.strip()
    _core.admin_pending.pop(uid, None)
    target_id = _core.find_user(query_text)
    if target_id is None:
        await message.reply_text(
            "❌ لم أجد العضو المطلوب.",
            reply_markup=_core.back_button("admin_section_monitor"),
        )
        return True

    await message.reply_text(
        member_profile_text(target_id),
        reply_markup=member_profile_keyboard(target_id),
    )
    return True


def install(core_module) -> None:
    global _core, _original_has_active_subscription, _original_on_button
    global _original_process_admin_pending, PUBLIC_ACCESS_FILE

    _core = core_module
    PUBLIC_ACCESS_FILE = Path(_core.DATA_DIR) / "public_access.json"
    _load_public_access()

    _original_has_active_subscription = _core.has_active_subscription
    _original_on_button = _core.on_button
    _original_process_admin_pending = _core.process_admin_pending

    _core.has_active_subscription = enhanced_has_active_subscription
    _core.on_button = enhanced_on_button
    _core.process_admin_pending = enhanced_process_admin_pending

    # Replace only admin presentation helpers. Core business logic remains untouched.
    _core.admin_keyboard = admin_keyboard
    _core.admin_subscriptions_keyboard = admin_subscriptions_keyboard
    _core.admin_search_keyboard = admin_search_keyboard
    _core.admin_monitor_keyboard = admin_monitor_keyboard
    _core.admin_communication_keyboard = admin_communication_keyboard
    _core.admin_security_keyboard = admin_security_keyboard

    # Additional admin tools preserve the core mailbox/subscription handlers.
    from member_mail_admin import install as install_mail_admin
    install_mail_admin(_core)

    # Add isolated trial storage and access/UI wrappers after the existing layer.
    from trial_access import install as install_trials
    install_trials(_core, _original_has_active_subscription,
                   public_access_active, _public_access_status_text)

    print("Admin enhancements installed")
