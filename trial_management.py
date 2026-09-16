"""Admin-only trial link creation, revocation and beneficiary views."""
import secrets

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest


class TrialManagement:
    def __init__(self, core, store, timestamp):
        self.core, self.store, self.timestamp = core, store, timestamp
        self.drafts = {}
        self.original_pending = core.process_admin_pending
        core.process_admin_pending = self.process_pending

    def button(self, text, data, style=None):
        return self.core.make_button(text, data, style)

    def back(self, data="trial_admin_list:0", text="🔙 روابط التجربة"):
        return [self.button(text, data, "primary")]

    def name(self, link):
        return link["name"] or f"رابط {link['token'][:6]}"

    def clear(self, uid):
        self.drafts.pop(uid, None)
        if (str(self.core.admin_pending.get(uid, "")).startswith("trial_wizard_")
                or self.core.admin_pending.get(uid) == "trial_support_username"):
            self.core.admin_pending.pop(uid, None)

    async def edit(self, query, text, rows, **kwargs):
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), **kwargs)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    def live_link(self, token):
        link, total, active = self.store.stats(token, self.core.now_utc().timestamp())
        return (None if link and link["deleted"] else link), total, active

    async def show_list(self, query, page):
        links, page, count = self.store.links(page)
        rows = [[self.button("➕ إنشاء رابط تجربة", "trial_admin_create", "success")]]
        rows.append([self.button("✏️ تعديل يوزر الدعم", "trial_admin_support")])
        for link in links:
            status = "🟢" if link["enabled"] else "🔴"
            label = f"{status} {self.name(link)[:35]} · {link['duration_days']} يوم"
            rows.append([self.button(label, f"trial_admin_view:{link['token']}", "primary")])
        nav = []
        if page:
            nav.append(self.button("⬅️ السابق", f"trial_admin_list:{page-1}", "primary"))
        if (page+1)*8 < count:
            nav.append(self.button("التالي ➡️", f"trial_admin_list:{page+1}", "primary"))
        if nav:
            rows.append(nav)
        rows.append(self.back("admin_menu", "🔙 لوحة الأدمن"))
        await self.edit(query,
            "🎁 روابط التجربة\n\nاختر مدة واسم كل رابط عند إنشائه. تبدأ المدة من تفعيل المستخدم، "
            "مرة واحدة لكل حساب عبر جميع الروابط.\n"
            "تعطيل الرابط أو حذفه يمنع تفعيلات جديدة فقط؛ التجارب المفعّلة تكمل مدتها.\n\n"
            f"يوزر الدعم: {'@' + self.store.support_username() if self.store.support_username() else 'غير محدد'}\n"
            f"عدد الروابط: {count} — الصفحة {page+1}", rows)

    async def show_link(self, query, context, token):
        link, total, active = self.live_link(token)
        if not link:
            return await self.show_list(query, 0)
        bot_user = await context.bot.get_me()
        rows = [[self.button("🔄 تحديث الإحصائيات", f"trial_admin_view:{token}", "primary")],
                [self.button("👥 المستفيدون", f"trial_admin_users:{token}:0", "primary")]]
        if link["enabled"]:
            rows.append([self.button("🔴 تعطيل الرابط", f"trial_admin_disable:{token}", "danger")])
        else:
            rows.append([self.button("🟢 إعادة تفعيل الرابط", f"trial_admin_enable:{token}", "success")])
        rows.append([self.button("🗑 حذف الرابط", f"trial_admin_delete:{token}", "danger")])
        rows.append(self.back())
        await self.edit(query,
            f"🎁 {self.name(link)}\n\nhttps://t.me/{bot_user.username}?start=trial_{token}\n\n"
            f"الحالة: {'فعال' if link['enabled'] else 'معطل'}\n"
            f"مدة التجربة لكل شخص: {link['duration_days']} يوم من تفعيله\n"
            f"تاريخ الإنشاء: {self.timestamp(link['created'])}\n"
            f"مرات وصول /start عبر الرابط: {link['opens']}\n"
            f"الأشخاص الذين فعّلوا التجربة: {total}\n"
            f"التجارب النشطة زمنيًا: {active}\nالتجارب المنتهية: {total-active}\n\n"
            "الحظر والاشتراكات مستقلان عن مدة التجربة. تعطيل الرابط أو حذفه لا يغيّر التجارب السابقة.",
            rows, disable_web_page_preview=True)

    async def show_users(self, query, token, page):
        link, _, _ = self.live_link(token)
        if not link:
            return await self.show_list(query, 0)
        users, page, count = self.store.beneficiaries(token, page)
        lines = [f"👥 المستفيدون — {self.name(link)}", f"العدد: {count} — الصفحة {page+1}"]
        now = self.core.now_utc().timestamp()
        rows = []
        for record in users:
            uid = record["user_id"]
            name = self.core.user_display_name(uid)[:60]
            status = "نشطة" if record["expires"] > now else "منتهية"
            if self.core.is_blocked(uid):
                status += " — المستخدم محظور"
            lines.append(f"\n👤 {name}\nID: {uid} — {status}\n"
                         f"بدأت: {self.timestamp(record['started'])}\nتنتهي: {self.timestamp(record['expires'])}")
            rows.append([self.button(f"👤 {name[:40]} · {uid}", f"admin_member:{uid}")])
        if not users:
            lines.append("\nلم يفعّل أحد التجربة عبر هذا الرابط بعد.")
        nav = []
        if page:
            nav.append(self.button("⬅️ السابق", f"trial_admin_users:{token}:{page-1}", "primary"))
        if (page+1)*8 < count:
            nav.append(self.button("التالي ➡️", f"trial_admin_users:{token}:{page+1}", "primary"))
        if nav:
            rows.append(nav)
        rows.append(self.back(f"trial_admin_view:{token}", "🔙 تفاصيل الرابط"))
        await self.edit(query, "\n".join(lines), rows)

    async def choose_duration(self, query, uid):
        nonce = secrets.token_hex(6)
        self.drafts[uid] = {"nonce": nonce, "stage": "duration"}
        rows = [[self.button(f"{days} يوم", f"trial_admin_days:{nonce}:{days}", "success")
                 for days in pair] for pair in ((1,3),(7,30))]
        rows.append([self.button("✏️ مدة مخصصة", f"trial_admin_custom:{nonce}", "primary")])
        rows.append(self.back())
        await self.edit(query, "➕ إنشاء رابط تجربة\n\nاختر مدة تجربة كل مستخدم. اليوم = 24 ساعة من لحظة تفعيله للرابط.", rows)

    def name_prompt(self, draft):
        return (f"✏️ اسم الرابط\n\nالمدة: {draft['days']} يوم.\n"
                "أرسل اسمًا حتى 60 حرفًا، مثل: تجربة قناة تلجرام، أو اختر بدون اسم.")

    def name_rows(self, draft):
        return [[self.button("بدون اسم", f"trial_admin_skip:{draft['nonce']}", "primary")], self.back()]

    def confirm_text(self, draft):
        return ("✅ تأكيد إنشاء رابط تجربة\n\n"
                f"الاسم: {draft.get('name') or 'تلقائي'}\nالمدة: {draft['days']} يوم لكل مستخدم من لحظة تفعيله.\n"
                "مرة واحدة لكل حساب عبر جميع الروابط، دون تغيير الاشتراكات الحالية.")

    def confirm_rows(self, draft):
        return [[self.button("✅ إنشاء الرابط", f"trial_admin_confirm:{draft['nonce']}", "success")],
                self.back("trial_admin_create", "🔙 تغيير المدة"), self.back()]

    async def process_pending(self, update, context):
        user, message = update.effective_user, update.effective_message
        if not user or not message:
            return False
        uid = user.id
        action = self.core.admin_pending.get(uid, "")
        if action == "trial_support_username":
            if not self.core.is_admin(uid):
                self.clear(uid)
                return True
            try:
                username = self.store.set_support_username(message.text or "")
            except ValueError:
                await message.reply_text("❌ أرسل يوزرًا صحيحًا مثل @username، بدون رابط أو مسافات.",
                                         reply_markup=InlineKeyboardMarkup([self.back()]))
                return True
            self.clear(uid)
            await message.reply_text(f"✅ تم حفظ يوزر الدعم: @{username}\n"
                                     "يظهر زر التواصل لأصحاب التجربة عند فتح الشاشة من جديد. الأزرار المرسلة سابقًا لا تتغير تلقائيًا.",
                                     reply_markup=InlineKeyboardMarkup([self.back()]))
            return True
        if not str(action).startswith("trial_wizard_"):
            return await self.original_pending(update, context)
        if not self.core.is_admin(uid):
            self.clear(uid)
            return True
        draft = self.drafts.get(uid)
        if not draft:
            self.clear(uid)
            await message.reply_text("انتهت جلسة الإنشاء. افتح روابط التجربة وابدأ من جديد.",
                                     reply_markup=InlineKeyboardMarkup([self.back()]))
            return True
        text = (message.text or "").strip()
        if action == "trial_wizard_days":
            try:
                days = int(text)
            except ValueError:
                days = 0
            if not 1 <= days <= 3650:
                await message.reply_text("❌ أرسل عدد أيام صحيحًا بين 1 و3650.", reply_markup=InlineKeyboardMarkup([self.back()]))
                return True
            draft.update(days=days, stage="name")
            self.core.admin_pending[uid] = "trial_wizard_name"
            await message.reply_text(self.name_prompt(draft), reply_markup=InlineKeyboardMarkup(self.name_rows(draft)))
        elif action == "trial_wizard_name":
            if not text or len(text) > 60 or not message.text:
                await message.reply_text("❌ أرسل اسمًا من 1 إلى 60 حرفًا، أو اختر بدون اسم.", reply_markup=InlineKeyboardMarkup(self.name_rows(draft)))
                return True
            draft.update(name=" ".join(text.split()), stage="confirm")
            self.core.admin_pending.pop(uid, None)
            await message.reply_text(self.confirm_text(draft), reply_markup=InlineKeyboardMarkup(self.confirm_rows(draft)))
        return True

    async def on_button(self, update, context):
        query = update.callback_query
        data, uid = query.data or "", query.from_user.id
        if not data.startswith("trial_admin_"):
            self.clear(uid)
            return False
        await query.answer()
        if not self.core.is_admin(uid):
            return True
        action, _, rest = data.partition(":")
        if action in {"trial_admin_days", "trial_admin_custom", "trial_admin_skip", "trial_admin_confirm"}:
            nonce, _, value = rest.partition(":")
            draft = self.drafts.get(uid)
            stages = {"trial_admin_days":"duration", "trial_admin_custom":"duration",
                      "trial_admin_skip":"name", "trial_admin_confirm":"confirm"}
            if not draft or draft["nonce"] != nonce or draft["stage"] != stages[action]:
                await self.edit(query, "هذا الزر قديم أو انتهت جلسة الإنشاء. افتح القائمة للبدء مجددًا.", [self.back()])
                return True
            if action == "trial_admin_custom":
                draft["stage"] = "custom"
                self.core.admin_pending[uid] = "trial_wizard_days"
                await self.edit(query, "✏️ مدة مخصصة\n\nأرسل عدد الأيام من 1 إلى 3650. تبدأ المدة من تفعيل كل شخص.", [self.back()])
            elif action == "trial_admin_days":
                if value not in {"1","3","7","30"}:
                    return True
                draft.update(days=int(value), stage="name")
                self.core.admin_pending[uid] = "trial_wizard_name"
                await self.edit(query, self.name_prompt(draft), self.name_rows(draft))
            elif action == "trial_admin_skip":
                draft.update(name="", stage="confirm")
                self.core.admin_pending.pop(uid, None)
                await self.edit(query, self.confirm_text(draft), self.confirm_rows(draft))
            else:
                await context.bot.get_me()
                # Re-check after network I/O; a cancelled or consumed draft cannot create a link.
                if self.drafts.get(uid) is not draft or draft["stage"] != "confirm":
                    return True
                token = self.store.create(self.core.now_utc().timestamp(), draft["days"], draft["name"])
                self.clear(uid)
                await self.show_link(query, context, token)
            return True
        self.clear(uid)
        self.core.admin_pending.pop(uid, None)
        self.core.admin_pending_target.pop(uid, None)
        if action == "trial_admin_create":
            await self.choose_duration(query, uid)
        elif action == "trial_admin_support":
            self.core.admin_pending[uid] = "trial_support_username"
            username = self.store.support_username()
            await self.edit(query,
                "✏️ يوزر الدعم\n\n"
                f"الحالي: {'@' + username if username else 'غير محدد'}\n"
                "أرسل يوزر حساب الدعم مثل @username. سيظهر بزر شفاف لطلب الاشتراك داخل شاشة التجربة.\n"
                "إذا لم تحدد يوزرًا، يبقى زر التواصل مخفيًا.", [self.back()])
        elif action == "trial_admin_list":
            try:
                page = int(rest)
            except ValueError:
                page = 0
            await self.show_list(query, page)
        elif action == "trial_admin_users":
            token, _, page = rest.partition(":")
            try:
                page = int(page)
            except ValueError:
                page = 0
            await self.show_users(query, token, page)
        elif action in {"trial_admin_view", "trial_admin_disable", "trial_admin_enable",
                        "trial_admin_delete", "trial_admin_delete_confirm"}:
            token = rest
            link, _, _ = self.live_link(token)
            if not link:
                await self.show_list(query, 0)
                return True
            if action == "trial_admin_delete":
                await self.edit(query,
                    f"🗑 حذف الرابط: {self.name(link)}\n\nهل تريد حذف الرابط من القائمة وإيقاف استقبال مستخدمين جدد؟\n"
                    "التجارب المفعّلة تكمل مدتها، ويبقى سجل الاستفادة لمنع تكرار التجربة.",
                    [[self.button("🔴 نعم، احذف الرابط", f"trial_admin_delete_confirm:{token}", "danger")],
                     self.back(f"trial_admin_view:{token}", "🔙 إلغاء")])
            elif action == "trial_admin_delete_confirm":
                self.store.delete(token)
                await self.show_list(query, 0)
            else:
                if action == "trial_admin_disable":
                    self.store.disable(token)
                elif action == "trial_admin_enable":
                    self.store.enable(token)
                await self.show_link(query, context, token)
        return True
