"""整理明细、日志、重启微信、充值 —— 这几样随 Tk 界面丢过。

## 一条规矩

**明细按需拉，不进轮询**。``/api/state`` 每秒被拉一次，把报告正文塞进去
只会让页面越来越卡；所以它们在单独的接口里，页面点了才取。
（跑完向导不再有"首页"这一页：结果屏看产物，「重新开始」回第一步。）
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant import web_server  # noqa: E402
from wechat_ai_assistant.application import AppEvents  # noqa: E402


class InterfaceTests(unittest.TestCase):
    """``/api/analysis/report``、``/api/logs``、``/api/account/recharge``。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="pyw-home2-")
        self.app = SimpleNamespace(
            data_dir=self.dir.name,
            orders=None,
            tenant="t",
            events=AppEvents(),
            session=None,
            monitor=None,
            refresh_conversations=lambda: [],
            orders_for_display=lambda: [],
            wizard=None,
        )
        self.server = web_server.create_server(self.app, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://%s:%d" % self.server.server_address[:2]
        self.state = self.server.web_state

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post(self, path, body=None):
        data = json.dumps(body or {}).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_the_role_report_is_fetched_on_demand(self):
        status, data = self._get("/api/analysis/report")
        self.assertEqual(status, 200)
        self.assertIsNone(data["report"], "还没跑过就没有")
        self.assertIn("reason", data)

        self.state.set(role_report={
            "role_id": "activity", "role_name": "活动助手", "total": 2,
            "buckets": {"报名接龙": [
                {"sender": "张姐", "content": "报名 2 人", "sent_at": "t"},
            ]},
        })
        _status, data = self._get("/api/analysis/report")
        self.assertEqual(data["report"]["total"], 2)
        self.assertIn("报名接龙", data["report"]["buckets"])

    def test_the_report_is_not_in_the_polled_state(self):
        """几百条明细不能每秒回传一次 —— 只在按需接口里。"""
        _status, data = self._get("/api/state")
        self.assertNotIn("role_report", data)

    def test_logs_give_a_path_and_the_tail(self):
        for i in range(5):
            self.state.log(f"第 {i} 行")
        _status, data = self._get("/api/logs")
        self.assertTrue(data["path"], "要告诉商户日志文件在哪（发给客服）")
        self.assertIn("第 4 行", data["lines"][-1])
        self.assertLessEqual(len(data["lines"]), 120, "只给尾巴，不能整本回传")

    def test_recharge_opens_a_same_site_billing_page(self):
        with mock.patch("webbrowser.open", return_value=True) as opened:
            status, data = self._post("/api/account/recharge")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        url = opened.call_args.args[0]
        self.assertIn("jyisolutions.com", url)
        self.assertIn("billing", url)

    def test_recharge_still_gives_the_url_when_the_browser_fails(self):
        with mock.patch("webbrowser.open", return_value=False):
            _status, data = self._post("/api/account/recharge")
        self.assertFalse(data["ok"])
        self.assertTrue(data["url"], "打不开也要把地址给出来，让人自己开")

    def test_restart_wechat_runs_in_the_background(self):
        """重启要等好几秒，不能占着处理线程（否则整页卡死）。"""
        started = {}

        def fake_restart(_self, what, work):
            started["what"] = what
            return True

        with mock.patch.object(web_server.WebHandler, "_run_blocking", fake_restart):
            status, data = self._post("/api/wechat/restart")
        self.assertEqual(status, 200)
        self.assertTrue(data["started"])
        self.assertIn("微信", started["what"])


class PageEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")

    def test_results_keeps_reference_privacy_and_log_entries(self):
        # 「重新开始」回第一步后没有首页了：名单上传、隐私说明、日志
        # 这些入口跟着结果屏走，不能丢。
        self.assertIn("商品与客户名单", self.page)
        self.assertIn("数据与隐私说明", self.page)
        self.assertIn("运行日志", self.page)

    def test_results_has_a_report_button(self):
        # 入口现在在底栏的动作表里（不是正文写死的 <button>）：底栏不带滚动，
        # 报告再长、正文滚多远都点得到。
        self.assertIn('action:"role-report"', self.page)
        self.assertIn("/api/analysis/report", self.page)

    def test_orders_are_clickable_for_the_full_text(self):
        self.assertIn('data-order="', self.page)
        self.assertIn("orderDetail", self.page)
        self.assertIn("cellText(", self.page)

    def test_logs_and_recharge_have_entries(self):
        # 日志入口是结果屏正文里的小链接按钮（不占商户动作位）。
        self.assertIn('data-action="logs"', self.page)
        self.assertIn("/api/logs", self.page)
        self.assertIn('data-action="recharge"', self.page)

    def test_restart_only_shows_when_wechat_is_really_unreadable(self):
        self.assertIn('action:"restart-wechat"', self.page)
        self.assertIn('w.checked && w.state === "unreadable"', self.page)


if __name__ == "__main__":
    unittest.main()
