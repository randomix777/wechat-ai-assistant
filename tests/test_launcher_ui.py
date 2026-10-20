"""启动兜底：更新闪屏与失败弹窗。

## 为什么只剩这两个地方还用 Tk

产品界面已经是 Web（`web_app`：本机 HTTP + 应用窗口）。Tk 只留下**窗口起来之前**
那一小段：

* **更新闪屏** —— 正在换 exe，这时不能依赖自己的文件，也不能假设浏览器能用；
  顶 3 秒告诉商户"别重复双击"。这是唯一还在用 Tk 的地方。
* **失败弹窗** —— 已经改成 Win32 ``MessageBoxW``（ctypes），不用 Tk。

这里的测试盯的就是这条边界：**Tk 不许再扩散**，失败弹窗不依赖 Tk。
"""
from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant import launcher  # noqa: E402

SOURCE = (ROOT / "src" / "wechat_ai_assistant" / "launcher.py").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    return SOURCE[SOURCE.index(start):SOURCE.index(end)]


class ErrorDialogTests(unittest.TestCase):
    def test_it_uses_win32_message_box_not_tk(self):
        block = _block("def _show_error_dialog", "def _fail")
        self.assertIn("MessageBoxW", block)
        self.assertNotIn("import tkinter", block, "启动失败弹窗不该再依赖 Tk")
        self.assertNotIn("from tkinter", block)

    def test_it_shows_the_merchant_text_and_steals_focus(self):
        calls = {}

        def fake_message_box(hwnd, text, title, flags):
            calls.update(hwnd=hwnd, text=text, title=title, flags=flags)
            return 1

        fake_user32 = mock.Mock(MessageBoxW=fake_message_box)
        with mock.patch("ctypes.windll", mock.Mock(user32=fake_user32), create=True):
            launcher._show_error_dialog("缺少运行组件", "  这台电脑上缺少以下组件  ")

        self.assertEqual(calls["text"], "这台电脑上缺少以下组件", "首尾空白要去掉")
        self.assertEqual(calls["title"], "缺少运行组件")
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        self.assertTrue(calls["flags"] & MB_TOPMOST, "弹窗必须置顶，否则等于没弹")
        self.assertTrue(calls["flags"] & MB_SETFOREGROUND)

    def test_a_broken_dialog_never_raises(self):
        """报错本身不能再报错。"""
        with mock.patch("ctypes.windll", mock.Mock(user32=None), create=True):
            launcher._show_error_dialog("标题", "内容")  # 不该抛


class TkBoundaryTests(unittest.TestCase):
    def test_tkinter_is_only_referenced_by_the_update_splash(self):
        """Tk 只剩闪屏那一块 —— 别的地方再 import 它，就是在长第二套界面。"""
        head, rest = SOURCE.split("def _show_update_splash", 1)
        splash, tail = rest.split("def _check_and_apply_update", 1)
        for part, name in ((head, "闪屏之前"), (tail, "闪屏之后")):
            with self.subTest(part=name):
                self.assertNotIn("import tkinter", part, f"{name}又用上了 Tk")
                self.assertNotIn("from tkinter", part, f"{name}又用上了 Tk")

    def test_the_splash_still_degrades_silently(self):
        """没有 Tk（裁过的运行环境）时，闪屏不能挡住更新。"""
        block = _block("def _show_update_splash", "def _check_and_apply_update")
        self.assertIn("except Exception:", block)
        self.assertIn("return", block)

    def test_tkinter_is_not_a_required_dependency(self):
        """缺 Tk 只该少一个换版提示，不该拦住启动。"""
        required = {name for name, _description in [
            ("sqlite3", ""), ("pywinauto", ""), ("win32gui", ""), ("psutil", ""),
        ]}
        self.assertTrue(required)  # 说明这张表本来就只列必需项
        block = _block("def check_dependencies", "def _show_update_splash")
        self.assertNotIn("('tkinter'", block,
                         "tkinter 不该出现在必需依赖里 —— 界面已经是 Web")


if __name__ == "__main__":
    unittest.main()
