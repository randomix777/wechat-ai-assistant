"""导出、参考信息、账号概况 —— 这三样随 Tk 界面一起丢了入口，现在接回来。

## 判据在哪一层

* **能不能导**：``export_service.exportable``（只导跑过 AI 的，跳过几条要说得出来）；
* **打包**：``export_bundle.build_export_bundle``（纯逻辑，早就有）；
* **接口**：``/api/export/check`` 先说清能不能导，``/api/export/bundle`` 才给字节流；
* **页面**：只负责显示 ``reason`` / 触发下载，不自己判一遍。

导出是最容易"默默少导"的地方，所以这里盯的是**条数与跳过数必须一致**。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


from wechat_ai_assistant import export_service  # noqa: E402
from wechat_ai_assistant.merchant_app import ensure_local_store  # noqa: E402
from wechat_ai_assistant.conversations import Conversation, ConversationRegistry  # noqa: E402


class _App:
    """够 ``export_service`` 用的最小应用：订单库 + 已选会话 + 水位线。"""

    def __init__(self, data_dir: Path):
        from wechat_ai_assistant.application import AppEvents

        self.orders, self.tenant = ensure_local_store(data_dir / "orders.db")
        self.events = AppEvents()
        self.session = None
        self.monitor = None
        self.conversations = ConversationRegistry()
        self.conversations.refresh([Conversation(name="鲜果批发群", kind="group",
                                                 member_count=35)])
        self.conversations.set_history(self.conversations.available)

    def refresh_conversations(self):
        return list(self.conversations.available)

    @property
    def wizard(self):
        return type("W", (), {"conversations": self.conversations})()


class ExportRowsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="pyw-export-")
        self.app = _App(Path(self.dir.name))
        self.app.orders.add_import(
            self.app.tenant, "鲜果批发群",
            [{"sender": "张姐", "content": "苹果两箱", "kind": "text",
              "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "s1"},
             {"sender": "李姐", "content": "", "kind": "image",
              "sent_at": "2026-10-14T09:01:00-04:00", "source_id": "s2"}],
            "import-1", "wechat_history")

    def tearDown(self):
        self.app.orders.close()
        self.dir.cleanup()

    def test_review_rows_come_from_the_selected_conversations(self):
        rows = export_service.review_rows(self.app)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["group"] == "鲜果批发群" for row in rows))

    def test_nothing_is_exportable_before_analysis(self):
        rows = export_service.review_rows(self.app)
        kept, skipped = export_service.exportable(self.app, rows)
        self.assertEqual(kept, [])
        self.assertEqual(skipped, 2, "没整理过的要被数出来，不能悄悄导")

    def test_analyzed_messages_are_exported_with_placeholders(self):
        rows = export_service.review_rows(self.app)
        self.app.orders.mark_role_organized(
            self.app.tenant, [row["id"] for row in rows], "order")
        kept, skipped = export_service.exportable(self.app, rows)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(kept), 2)
        image_rows = [row for row in kept if row.get("kind") == "image"]
        self.assertEqual(image_rows[0]["content"], "［图片］",
                         "图片行导出时要有占位，不能是空格子")

    def test_the_bundle_contains_a_csv_and_a_readme(self):
        rows = export_service.review_rows(self.app)
        self.app.orders.mark_role_organized(
            self.app.tenant, [row["id"] for row in rows], "order")
        bundle = export_service.build(self.app, analysis_report="这次一共两单。",
                                      role_name="订单助手")
        self.assertTrue(bundle["data"], bundle["reason"])
        archive = zipfile.ZipFile(io.BytesIO(bundle["data"]))
        names = archive.namelist()
        self.assertTrue(any(name.endswith(".csv") for name in names), names)
        self.assertTrue(any("说明" in name for name in names), names)
        self.assertIn("两单", "".join(
            archive.read(name).decode("utf-8", "replace")
            for name in names if name.endswith(".md") or "说明" in name))

    def test_the_filename_says_which_conversation_and_how_many(self):
        rows = [{"group": "鲜果批发群"} for _ in range(3)]
        name = export_service.filename(rows)
        self.assertIn("鲜果批发群", name)
        self.assertIn("3条", name)
        self.assertTrue(name.endswith(".zip"))

    def test_without_analysis_it_says_why_instead_of_exporting_nothing(self):
        bundle = export_service.build(self.app)
        self.assertEqual(bundle["data"], b"")
        self.assertIn("整理", bundle["reason"])


class ExportEndpointTests(unittest.TestCase):
    """接口层：先说能不能导，再给字节流。"""

    def setUp(self):
        from wechat_ai_assistant import web_server

        self.dir = tempfile.TemporaryDirectory(prefix="pyw-export-http-")
        self.app = _App(Path(self.dir.name))
        self.app.orders.add_import(
            self.app.tenant, "鲜果批发群",
            [{"sender": "张姐", "content": "苹果两箱", "kind": "text",
              "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "s1"}],
            "import-1", "wechat_history")
        self.server = web_server.create_server(self.app, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://%s:%d" % self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.orders.close()
        self.dir.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=15) as response:
            return response.status, response.headers, response.read()

    def test_check_refuses_before_anything_is_analyzed(self):
        status, _headers, body = self._get("/api/export/check")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("整理", data["reason"])

    def test_bundle_is_a_zip_with_a_chinese_filename(self):
        rows = export_service.review_rows(self.app)
        self.app.orders.mark_role_organized(
            self.app.tenant, [row["id"] for row in rows], "order")
        status, headers, body = self._get("/api/export/bundle")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        disposition = headers["Content-Disposition"]
        self.assertIn("attachment", disposition)
        self.assertIn("filename*=UTF-8''", disposition, "中文文件名要靠 filename*")
        self.assertTrue(zipfile.ZipFile(io.BytesIO(body)).namelist())

    def test_bundle_refuses_politely_when_there_is_nothing_to_export(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/api/export/bundle")
        self.assertEqual(caught.exception.code, 409)
        data = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("整理", data["error"])


class PageEntryTests(unittest.TestCase):
    """页面上真的摆着这些入口 —— 否则接口做得再好，商户也点不到。"""

    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")

    def test_export_button_exists_and_checks_first(self):
        # 导出在底栏动作表里（结果页和首页共用同一套位置）。
        self.assertIn('action:"export"', self.page)
        self.assertIn("/api/export/check", self.page)
        self.assertIn("/api/export/bundle", self.page)

    def test_reference_upload_exists(self):
        self.assertIn('data-kind="products"', self.page)
        self.assertIn('data-kind="customers"', self.page)
        self.assertIn("/api/reference", self.page)

    def test_account_summary_shows_plan_and_expiry(self):
        self.assertIn("套餐：", self.page)
        self.assertIn("有效期至", self.page)
        self.assertIn("已用 ", self.page)


if __name__ == "__main__":
    unittest.main()
