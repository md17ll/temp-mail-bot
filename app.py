from typing import Dict, List

from fastapi import HTTPException, Request

import main as core
from smart_mail import (
    clean_text,
    extract_otp,
    html_to_readable_text,
    otp_copy_keyboard,
    split_message,
)


# Reuse the existing FastAPI app and all Telegram/admin/subscription logic.
app = core.app

# Replace only the old Mailgun inbound endpoint. Everything else in main.py stays unchanged.
app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == "/mailgun"
        and "POST" in (getattr(route, "methods", set()) or set())
    )
]


def build_email_text(to_email: str, sender: str, subject: str, body: str) -> str:
    return (
        "📩 وصلت رسالة جديدة\n\n"
        f"إلى: {to_email}\n"
        f"من: {sender or '(غير معروف)'}\n"
        f"العنوان: {subject or '(بدون عنوان)'}\n\n"
        f"{body or '(بدون نص)'}"
    )


@app.post("/mailgun")
async def smart_mailgun_inbound(request: Request) -> Dict[str, bool]:
    if not core.tg_app:
        return {"ok": True}

    if core.MAILGUN_WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != core.MAILGUN_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Bad mailgun secret")

    form = await request.form()

    recipient_raw = str(form.get("recipient", "") or "")
    to_raw = str(form.get("To", "") or form.get("to", "") or "")
    envelope_to_raw = str(form.get("envelope", "") or "")
    recipients = core.extract_emails(
        " , ".join([recipient_raw, to_raw, envelope_to_raw]).strip()
    )

    sender = clean_text(str(form.get("sender", "") or ""))
    subject = clean_text(str(form.get("subject", "") or ""))

    # Prefer HTML because many providers put OTP/PIN values only in the visual HTML
    # version. Convert it to readable text and remove CSS/JS/schema/tracking noise.
    body_html = str(
        form.get("body-html")
        or form.get("stripped-html")
        or ""
    )
    body_plain = str(
        form.get("stripped-text")
        or form.get("body-plain")
        or ""
    )

    if body_html.strip():
        body = html_to_readable_text(body_html)
        # Some malformed templates can yield almost no visible text after parsing.
        # In that case retain the plain-text part instead of losing content.
        if len(body.strip()) < 10 and body_plain.strip():
            body = clean_text(body_plain)
    else:
        body = clean_text(body_plain)

    print(
        "SMART MAILGUN INBOUND recipients:",
        recipients,
        "sender:",
        sender,
        "subject:",
        subject,
    )

    if not recipients:
        return {"ok": True, "delivered": False}

    # Detect OTP/PIN once from the actual email content. A button is added only
    # when contextual evidence strongly indicates a real verification code.
    otp = extract_otp(subject, body)
    copy_keyboard = otp_copy_keyboard(otp)

    sent_any = False
    for to_email in recipients:
        owner_id = core.email_owner.get(to_email)
        if not owner_id:
            print("No owner for:", to_email)
            continue

        if not core.has_active_subscription(owner_id):
            print("Inactive owner, skip deliver to:", owner_id, "email:", to_email)
            continue

        full_text = build_email_text(to_email, sender, subject, body)
        chunks: List[str] = split_message(full_text)

        try:
            for index, chunk in enumerate(chunks):
                is_last = index == len(chunks) - 1
                await core.tg_app.bot.send_message(
                    chat_id=owner_id,
                    text=chunk,
                    reply_markup=copy_keyboard if is_last else None,
                    disable_web_page_preview=True,
                )
            sent_any = True
        except Exception as exc:
            print("Telegram smart mail send error:", repr(exc))

    return {"ok": True, "delivered": sent_any}
