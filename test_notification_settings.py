import os
import tempfile
import unittest
from pathlib import Path


TEST_DATA = tempfile.TemporaryDirectory()
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")
os.environ.setdefault("OWNER_ID", "999")
os.environ["DATA_DIR"] = TEST_DATA.name

import app as bot_app
import mail_preferences
from verification_extract import extract_smart_findings


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class FakeRequest:
    headers = {}

    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


class NotificationSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = mail_preferences.PreferenceStore(
            Path(TEST_DATA.name) / f"{self._testMethodName}.sqlite3"
        )
        mail_preferences.store = self.store
        self.fake_bot = FakeBot()
        bot_app.core.tg_app = type("FakeApp", (), {"bot": self.fake_bot})()
        bot_app.core.email_owner = {"box@example.test": 77}
        self.old_access = bot_app.core.has_active_subscription
        bot_app.core.has_active_subscription = lambda _uid: True

    def tearDown(self):
        bot_app.core.has_active_subscription = self.old_access

    async def _deliver(self, subject="رمز التحقق", body="رمز التحقق هو ١٢٣ ٤٥٦", html=""):
        request = FakeRequest({
            "recipient": "box@example.test",
            "sender": "security@example.test",
            "subject": subject,
            "stripped-text": body,
            "body-html": html,
        })
        return await bot_app.smart_mailgun_inbound(request)

    async def test_default_is_original_full_message(self):
        await self._deliver(body="رمز التحقق هو 123456\nمحتوى كامل مهم")
        self.assertEqual(len(self.fake_bot.sent), 1)
        self.assertIn("محتوى كامل مهم", self.fake_bot.sent[0]["text"])

    async def test_codes_mode_sends_summary_and_copy_button(self):
        self.store.set_mode(77, "codes")
        await self._deliver(body="رمز التحقق هو ١٢٣ ٤٥٦\nنص خاص لا يجب إرساله")
        self.assertEqual(len(self.fake_bot.sent), 1)
        sent = self.fake_bot.sent[0]
        self.assertIn("123456", sent["text"])
        self.assertNotIn("نص خاص لا يجب إرساله", sent["text"])
        self.assertIsNotNone(sent["reply_markup"])

    async def test_paused_suppresses_but_global_disable_restores_full_delivery(self):
        self.store.set_receiving(77, False)
        await self._deliver(body="رسالة كاملة أولى")
        self.assertEqual(self.fake_bot.sent, [])
        self.store.set_feature_enabled(False)
        await self._deliver(body="رسالة كاملة ثانية")
        self.assertEqual(len(self.fake_bot.sent), 1)
        self.assertIn("رسالة كاملة ثانية", self.fake_bot.sent[0]["text"])

    async def test_codes_links_mode_keeps_activation_not_unsubscribe(self):
        self.store.set_mode(77, "codes_links")
        html = (
            '<p>أكّد حسابك</p><a href="https://example.test/activate?t=abc">تفعيل الحساب</a>'
            '<a href="https://example.test/unsubscribe?id=7">إلغاء الاشتراك</a>'
        )
        await self._deliver(subject="تفعيل الحساب", body="أكّد حسابك", html=html)
        text = self.fake_bot.sent[0]["text"]
        self.assertIn("/activate?t=abc", text)
        self.assertNotIn("unsubscribe", text)

    def test_store_is_persistent_and_defaults_are_all_on(self):
        self.assertEqual(self.store.get(501), (True, "all"))
        self.store.set_mode(501, "codes_links")
        self.store.set_receiving(501, False)
        reopened = mail_preferences.PreferenceStore(self.store.path)
        self.assertEqual(reopened.get(501), (False, "codes_links"))

    def test_global_toggle_hides_and_restores_user_settings_button(self):
        def callbacks():
            markup = bot_app.core.main_keyboard_for(77)
            return [button.callback_data for row in markup.inline_keyboard for button in row]

        self.assertIn("mail_pref_home", callbacks())
        self.store.set_feature_enabled(False)
        self.assertNotIn("mail_pref_home", callbacks())
        self.store.set_feature_enabled(True)
        self.assertIn("mail_pref_home", callbacks())

    def test_subscription_status_is_above_welcome(self):
        bot_app.core.subscriptions[77] = {"expires_at": None}
        text = bot_app.core.start_text("box@example.test", 77)
        self.assertLess(text.index("اشتراكك"), text.index("مرحباً بك"))
        bot_app.core.subscriptions.pop(77, None)

    def test_extractor_avoids_promos_orders_and_unsubscribe(self):
        findings = extract_smart_findings(
            "عرض اليوم",
            "استخدم كود الخصم 2026. رقم الطلب 778899.",
            '<a href="https://shop.test/unsubscribe">Unsubscribe</a>',
        )
        self.assertEqual(findings.codes, [])
        self.assertEqual(findings.links, [])


if __name__ == "__main__":
    unittest.main()
