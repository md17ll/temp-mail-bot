"""Rank current mailbox owners and confirm targeted bulk mailbox deletion."""
import json
import secrets

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest


def install(core):
    original_button = core.on_button
    confirmations = {}
    button = core.make_button

    def back(data="mail_admin_top:0", text="🔙 الأكثر بريدات"):
        return [button(text, data, "primary")]

    def emails_for(uid):
        return tuple(sorted(core.user_emails.get(uid, [])))

    def label(uid):
        return " ".join(core.user_display_name(uid).split())[:55]

    async def edit(query, text, rows):
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def show_top(query, page):
        members = sorted(((uid, len(emails)) for uid, emails in core.user_emails.items() if emails),
                         key=lambda pair: (-pair[1], pair[0]))
        pages = max(1, (len(members)+7)//8)
        page = min(max(page, 0), pages-1)
        rows = [[button(f"{i+1}. {label(uid)[:35]} — {count} بريد", f"mail_admin_user:{uid}:{page}")]
                for i, (uid, count) in enumerate(members[page*8:(page+1)*8], start=page*8)]
        nav = []
        if page:
            nav.append(button("⬅️ السابق", f"mail_admin_top:{page-1}", "primary"))
        if page+1 < pages:
            nav.append(button("التالي ➡️", f"mail_admin_top:{page+1}", "primary"))
        if nav:
            rows.append(nav)
        rows.append([button("🔄 تحديث", f"mail_admin_top:{page}", "primary")])
        rows.append(back("admin_section_monitor", "🔙 الإحصائيات والأعضاء"))
        await edit(query,
            "📧 الأكثر بريدات\n\nترتيب حسب عدد البريدات المحجوزة حاليًا، وليس العدد التاريخي للمحذوفة.\n"
            "اضغط على العضو لعرض معلوماته أو حذف جميع بريداته بعد التأكيد.\n\n"
            f"أصحاب البريدات: {len(members)} — الصفحة {page+1}/{pages}", rows)

    async def show_user(query, target, page):
        rows = [[button("📬 عرض البريدات", f"admin_manage_emails_direct:{target}", "primary")]]
        if emails_for(target):
            rows.append([button("🗑 حذف جميع بريداته", f"mail_admin_delete:{target}:{page}", "danger")])
        rows.append(back(f"mail_admin_top:{page}"))
        await edit(query, f"👤 {label(target)}\nID: {target}\n"
                   f"الحالة: {core.subscription_status_text(target)}\n"
                   f"📧 البريدات الحالية: {len(emails_for(target))}\n\n"
                   "حذف البريدات لا يغيّر الاشتراك أو التجربة أو الحظر.", rows)

    async def confirm(query, target, page, changed=False):
        snapshot = emails_for(target)
        if not snapshot:
            return await show_user(query, target, page)
        nonce = secrets.token_hex(8)
        confirmations[query.from_user.id] = (nonce, target, page, snapshot)
        text = "⚠️ تغيّرت مجموعة البريدات. راجع العدد وأكّد من جديد.\n\n" if changed else ""
        text += (f"🗑 هل تريد حذف جميع بريدات هذا المستخدم؟\n\nالاسم: {label(target)}\n"
                 f"ID: {target}\nعدد البريدات: {len(snapshot)}\n\n"
                 "ستصبح هذه العناوين متاحة للحجز مجددًا، وقد تصل رسائلها المستقبلية لمن يحجزها.\n"
                 "لن يتغير اشتراك المستخدم أو تجربته أو حظره.")
        await edit(query, text,
                   [[button("🔴 نعم، احذف جميع بريداته", f"mail_admin_confirm:{nonce}", "danger")],
                    back(f"mail_admin_user:{target}:{page}", "🔙 إلغاء")])

    def delete_snapshot(target, snapshot):
        # No await between validation, mutation and save; only the confirmed owner is changed.
        old_emails = core.user_emails.pop(target, None)
        had_last = target in core.user_last_email
        old_last = core.user_last_email.pop(target, None)
        removed_owners = {}
        for email in snapshot:
            if core.email_owner.get(email) == target:
                removed_owners[email] = core.email_owner.pop(email)
        try:
            core.save_state()
            # The legacy saver logs failures instead of raising. Do not report false success.
            saved = json.loads(core.STATE_FILE.read_text(encoding="utf-8"))
            if (str(target) in saved.get("user_emails", {})
                    or str(target) in saved.get("user_last_email", {})
                    or any(str(saved.get("email_owner", {}).get(email)) == str(target) for email in removed_owners)):
                raise OSError("Mailbox deletion was not persisted")
        except Exception:
            if old_emails is not None:
                core.user_emails[target] = old_emails
            if had_last:
                core.user_last_email[target] = old_last
            core.email_owner.update(removed_owners)
            return False
        return True

    async def on_button(update, context):
        query = update.callback_query
        if not query:
            return
        uid, data = query.from_user.id, query.data or ""
        if not data.startswith("mail_admin_"):
            confirmations.pop(uid, None)
            return await original_button(update, context)
        await query.answer()
        if not core.is_admin(uid):
            return
        core.admin_pending.pop(uid, None)
        core.admin_pending_target.pop(uid, None)
        action, _, rest = data.partition(":")
        if action == "mail_admin_confirm":
            record = confirmations.get(uid)
            if not record or record[0] != rest:
                await edit(query, "انتهى هذا التأكيد أو استُخدم سابقًا. افتح العضو للتأكيد من جديد.", [back()])
                return
            _, target, page, snapshot = confirmations.pop(uid)
            if emails_for(target) != snapshot:
                return await confirm(query, target, page, changed=True)
            if delete_snapshot(target, snapshot):
                await edit(query, f"✅ تم حذف {len(snapshot)} بريد للمستخدم {target}.\n"
                           "الاشتراك والتجربة والحظر لم تتغير.", [back(f"mail_admin_top:{page}")])
            else:
                await edit(query, "❌ تعذّر تأكيد حفظ الحذف. لم نؤكد نجاح العملية؛ تحقق من التخزين وأعد المحاولة.",
                           [back(f"mail_admin_user:{target}:{page}", "🔙 العضو")])
            return
        confirmations.pop(uid, None)
        try:
            if action == "mail_admin_top":
                await show_top(query, int(rest))
            elif action in {"mail_admin_user", "mail_admin_delete"}:
                target_raw, page_raw = rest.split(":", 1)
                target, page = int(target_raw), max(0, int(page_raw))
                if action == "mail_admin_user":
                    await show_user(query, target, page)
                else:
                    await confirm(query, target, page)
        except ValueError:
            await show_top(query, 0)

    core.on_button = on_button
