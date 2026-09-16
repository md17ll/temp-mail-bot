"""Subscription expiry notifications; independent of subscription records and durations."""
import asyncio
import json

from telegram import InlineKeyboardMarkup
from telegram.error import RetryAfter, BadRequest, Forbidden


class SubscriptionNotices:
    def __init__(self, core, store):
        self.core, self.store = core, store
        self.task = None

    def period(self, record):
        # Renewals get a distinct key without changing existing subscription data.
        return json.dumps([record.get('activated_at'), record.get('expires_at')], separators=(',', ':'))

    async def check(self):
        from trial_access import support_button
        if not self.core.tg_app:
            return
        for uid, record in list(self.core.subscriptions.items()):
            if self.core.subscriptions.get(uid) is not record:
                continue  # A renewal may replace the record while another send is awaited.
            expiry = self.core.str_to_dt(record.get('expires_at'))
            if not expiry or expiry > self.core.now_utc() or self.core.is_blocked(uid) or self.core.is_admin(uid):
                continue
            period = self.period(record)
            if not self.store.claim_subscription_notice(uid, period):
                continue
            username = self.store.support_username()
            markup = InlineKeyboardMarkup([[support_button(username, '💬 التواصل مع الأدمن لتجديد الاشتراك')]]) if username else None
            try:
                await self.core.tg_app.bot.send_message(
                    chat_id=uid, text='⌛ لقد انتهى اشتراكك\n\nيرجى التواصل مع الأدمن لتجديد اشتراكك ومتابعة استخدام البوت 👇',
                    reply_markup=markup)
                self.store.subscription_notice_status(uid, period, 'sent')
            except RetryAfter:
                # Telegram explicitly rejected this request; retry on a later check.
                with self.store.connect() as db, db:
                    db.execute('DELETE FROM subscription_notices WHERE user_id=? AND period=?', (uid, period))
                break
            except (BadRequest, Forbidden) as exc:
                self.store.subscription_notice_status(uid, period, 'failed')
                print('Subscription expiry notice rejected:', uid, type(exc).__name__)
            except Exception as exc:
                # A timeout may mean delivered. Retain the durable claim to avoid duplicates.
                print('Subscription expiry notice uncertain:', uid, type(exc).__name__)

    async def run(self):
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print('Subscription expiry notice check error:', type(exc).__name__)
            await asyncio.sleep(60)

    async def start(self):
        # On first installation, do not broadcast retrospectively to old expired accounts.
        # Later restarts still notify periods that ended while the bot was offline.
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            first = db.execute("INSERT OR IGNORE INTO trial_settings VALUES ('subscription_notices_initialized', '1')").rowcount
            if first:
                for uid, record in self.core.subscriptions.items():
                    expiry = self.core.str_to_dt(record.get('expires_at'))
                    if expiry and expiry <= self.core.now_utc():
                        db.execute('INSERT OR IGNORE INTO subscription_notices VALUES (?, ?, ?)',
                                   (uid, self.period(record), 'historical'))
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None


def install(core, store):
    notices = SubscriptionNotices(core, store)
    core.app.on_event('startup')(notices.start)
    core.app.on_event('shutdown')(notices.stop)
    return notices
