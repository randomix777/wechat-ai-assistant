"""登录与退出。

两件事以前都不成立：

* **登不出去。** ``client_login.logout()`` 一直存在，却没有任何地方调用它。
  商户登录一次之后就再也换不了账号 —— 收银电脑上换个人就登不进去。
* **登录后看不到自己登的是谁。** ``MerchantWindow.session`` 是类属性
  ``None``，**从来没有被赋值过**，所以 ``if self.session is not None`` 永远为假，
  顶栏登录后依然显示「账号：未登录」。

后一条是被"退出"这条测试撞出来的：断言登录后账号条会写名字，结果它一直说未登录。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "order_agent" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "business_agent" / "src"))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from wechat_ai_assistant.history_read import SimulatedHistory  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402


class Session:
    merchant_name = "一号店"


class FakeClient:
    def __init__(self, fail_on_logout: bool = False):
        self.logged_out = False
        self.fail_on_logout = fail_on_logout

    def logout(self):
        if self.fail_on_logout:
            raise RuntimeError("network down")
        self.logged_out = True


class DetachTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pyw-detach-")
        data_dir = Path(self._tmp.name)
        self.store = OrderStore(str(data_dir / "orders.db"))
        self.tenant = self.store.create_merchant("登出测试店")["merchant_id"]
        self.app = MerchantApplication(
            data_dir=data_dir, orders=self.store, tenant=self.tenant,
            probe=None, launcher=None,
            conversation_source=SimulatedConversations(
                [("鲜果批发群", ConversationKind.GROUP, 8)]
            ),
            history_reader_fn=SimulatedHistory({}, delay=0.0), simulated=True,
        )
        self.client = FakeClient()
        self.app.attach(session=Session(), client=self.client, parse_service=object())

    def tearDown(self):
        for closer in (self.app.merger, self.app.cursor):
            try:
                closer.close()
            except Exception:
                pass
        self.app.close()
        self.store.close()
        self._tmp.cleanup()

    def test_attach_stores_the_session(self):
        self.assertIsNotNone(self.app.session)

    def test_detach_clears_the_session(self):
        self.app.detach()
        self.assertIsNone(self.app.session)

    def test_detach_notifies_the_server(self):
        self.app.detach()
        self.assertTrue(self.client.logged_out, "没有通知服务端作废")

    def test_a_failed_server_logout_still_signs_out_locally(self):
        """断网时也要能退出来 —— 否则商户被锁在自己登录不上的状态里。"""
        self.app.client = FakeClient(fail_on_logout=True)
        self.app.detach()
        self.assertIsNone(self.app.session)

    def test_detach_drops_the_cloud_pipeline(self):
        self.app.detach()
        self.assertIsNone(self.app.parse_service)
        self.assertIsNone(self.app.wizard.pipeline)
        self.assertFalse(self.app.backend_is_model,
                         "退出后不得继续把结果标成真实模型")

    def test_detach_returns_to_the_login_page(self):
        """退出登录必须回到登录前置页（真实报障）。

        门禁判据是登录态，而退出是**唯一一次**让它由真变假的操作。以前只清
        session/client/pipeline、不动 ``wizard.step``：停在首页或核对页点退出，
        页面纹丝不动 —— 顶栏写着「未登录」，正文还是商户的经营数据。
        """
        from wechat_ai_assistant.wizard import WizardStep

        self.app.wizard.step = WizardStep.DONE
        self.app.detach()
        self.assertEqual(self.app.wizard.step, WizardStep.LOGIN,
                         "退出登录要回登录页，不能留在原来的流程里")

    def test_detach_deletes_the_local_credentials(self):
        """退出必须删本机凭证：不清文件，下次启动 restore 又登回去 ——
        商户明明点了退出，重开发现还登着（真实报障）。"""
        from wechat_ai_assistant.client_login import CredentialStore, restore_session

        credentials = CredentialStore(str(Path(self._tmp.name) / "cred.bin"))
        credentials.save({"session": {"access_token": "a", "refresh_token": "r"}})
        self.app.credentials = credentials
        self.app.detach()
        self.assertIsNone(restore_session(credentials), "凭证文件没删干净")

    def test_detach_deletes_credentials_even_when_offline(self):
        """断网退出也要彻底：服务端没通知到，本地必须照删。"""
        from wechat_ai_assistant.client_login import CredentialStore, restore_session

        credentials = CredentialStore(str(Path(self._tmp.name) / "cred2.bin"))
        credentials.save({"session": {"access_token": "a", "refresh_token": "r"}})
        self.app.credentials = credentials
        self.app.client = FakeClient(fail_on_logout=True)
        self.app.detach()
        self.assertIsNone(restore_session(credentials))

    def test_detach_keeps_local_messages(self):
        """已读到的消息是商户自己经营出来的，退出登录不该删。"""
        # 走真实的入库路径，不要手写 INSERT —— messages 有一串非空外键列，
        # 手写的那份数据和真实写入的不是一回事。
        self.store.add_import(
            self.tenant, "c1",
            [{"sender": "张姐", "content": "苹果两箱",
              "sent_at": "2026-10-02 09:01", "source_id": "s1", "stable_source_id": True}],
            source="import-s1", source_type="test",
        )
        self.app.detach()
        count = self.store.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE merchant_id=?", (self.tenant,)
        ).fetchone()["c"]
        self.assertEqual(count, 1, "退出登录把本地消息删了")

    def test_local_reading_still_works_after_signing_out(self):
        """退出云端登录不影响本地读取 —— 本地资料本来就不该要账号。"""
        self.app.detach()
        self.app.wizard.step = "history"
        self.app.wizard.choose_history_conversations(
            list(self.app.wizard.refresh_conversations())[:1]
        )
        self.assertIs(self.app.wizard.orders(), self.store)


class LogoutWordingTests(unittest.TestCase):
    """商户最误会的一件事：退出登录 = 数据没了。

    退出只断开云端能力。已读到的消息和订单一个字节都没少 —— 界面必须主动
    说出来，否则商户会去重读一遍真实数据，白费工夫还可能重复下单。
    """

    @classmethod
    def setUpClass(cls):
        cls.page = (Path(__file__).resolve().parent.parent
                    / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
                        encoding="utf-8")

    def test_logout_says_the_local_data_is_kept(self):
        body = self.page[self.page.index('if (action === "logout")'):]
        body = body[:body.index("return;")]
        self.assertIn("还在这台电脑上", body, "退出后要主动说清数据还在")

    def test_logout_says_he_does_not_need_to_read_again(self):
        """重读一遍会重复占用额度，也可能重复下单。"""
        body = self.page[self.page.index('if (action === "logout")'):]
        body = body[:body.index("return;")]
        self.assertIn("不需要重新读取", body)


if __name__ == "__main__":
    unittest.main()
