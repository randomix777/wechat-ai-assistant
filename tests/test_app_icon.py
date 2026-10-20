"""应用图标：打包链路 + 窗口挂载。

图标是「看起来像正经软件」的一部分，但缺了绝不能拦住商户开店 ——
所以这里既钉住产物存在（打包别退回默认羽毛），也钉住失败是静默的。
"""
from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest import mock

from wechat_ai_assistant import paths
from wechat_ai_assistant.app_icon import set_window_icon

ROOT = Path(__file__).resolve().parents[1]
ICO = ROOT / "deploy" / "assets" / "app.ico"
PNG = ROOT / "deploy" / "assets" / "app.png"
SPEC = ROOT / "deploy" / "merchant.spec"


def ico_sizes(data: bytes) -> list[tuple[int, int]]:
    """手撕 ICO 目录：不为一张图标把 Pillow 变成测试依赖。"""
    count = struct.unpack_from("<H", data, 4)[0]
    sizes: list[tuple[int, int]] = []
    for index in range(count):
        offset = 6 + index * 16
        width, height = data[offset], data[offset + 1]
        sizes.append((width or 256, height or 256))
    return sizes


class IconAssetTests(unittest.TestCase):
    def test_the_icon_exists_as_ico_and_png(self):
        self.assertTrue(ICO.is_file(), "deploy/assets/app.ico 不在，打包会退回默认图标")
        self.assertTrue(PNG.is_file(), "deploy/assets/app.png 不在，源码态窗口没图标")

    def test_the_ico_carries_the_sizes_windows_asks_for(self):
        data = ICO.read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00", "不是 ICO 文件")
        sizes = ico_sizes(data)
        for wanted in ((16, 16), (24, 24), (32, 32), (48, 48), (256, 256)):
            self.assertIn(wanted, sizes, f"缺少 {wanted[0]}px，任务栏缩下去会糊")
        self.assertGreaterEqual(len(sizes), 6)

    def test_the_build_spec_wires_the_icon_in(self):
        spec = SPEC.read_text(encoding="utf-8")
        self.assertIn("app.ico", spec)
        self.assertIn("icon=", spec)
        self.assertIn("datas=DATAS", spec)


class WindowIconTests(unittest.TestCase):
    class _StubbornWindow:
        """有些系统会拒绝 iconbitmap / iconphoto；窗口照开，图标不要了。"""

        def iconbitmap(self, **kwargs):
            raise RuntimeError("no icon for you")

        def iconphoto(self, *args, **kwargs):
            raise RuntimeError("no icon for you")

    def test_the_lookup_finds_the_icon_in_a_source_checkout(self):
        found = paths.asset_path("app.ico")
        self.assertIsNotNone(found)
        assert found is not None  # 给类型检查看
        self.assertTrue(found.is_file())

    def test_a_stubborn_window_does_not_break_startup(self):
        set_window_icon(self._StubbornWindow())

    def test_a_missing_asset_is_not_an_error(self):
        with mock.patch.object(paths, "asset_path", lambda name: None):
            set_window_icon(self._StubbornWindow())


class NativeWindowIconTests(unittest.TestCase):
    """Web 应用窗口是**别人的窗口**（Edge --app / pywebview）。

    所以图标得用 Win32 的 ``WM_SETICON`` + 类图标挂上去 —— 不挂的话任务栏和
    标题栏显示的是 Edge 的图标，看起来不像我们的软件。这里不点真窗口，只验
    调用形状与失败时的静默。
    """

    def _fake_user32(self, calls: dict):
        class _Fake:
            def LoadImageW(self, _inst, path, _type, cx, cy, _flags):
                calls.setdefault("loads", []).append((str(path), cx, cy))
                return 1000 + cx  # 非 0 = 加载成功

            def SendMessageW(self, hwnd, message, wparam, lparam):
                calls.setdefault("sent", []).append((hwnd, message, wparam, lparam))
                return 0

            def SetClassLongPtrW(self, hwnd, index, value):
                calls.setdefault("class", []).append((hwnd, index, value))
                return 0

        return _Fake()

    def test_it_sets_both_icon_sizes_and_the_class_icon(self):
        from wechat_ai_assistant import app_icon

        calls: dict = {}
        with mock.patch.object(app_icon, "_native", lambda: self._fake_user32(calls)):
            self.assertTrue(app_icon.apply_native_icon(4242))

        sizes = {cx for _path, cx, _cy in calls["loads"]}
        self.assertEqual(sizes, {32, 16}, "大图标给标题栏/任务栏，小图标给标题栏左上角")
        self.assertEqual(len(calls["sent"]), 2, "两个尺寸各一次 WM_SETICON")
        self.assertEqual([item[2] for item in calls["sent"]], [1, 0], "先大后小：ICON_BIG=1, ICON_SMALL=0")
        self.assertEqual(len(calls["class"]), 1, "类图标不设，任务栏那格还是 Edge 的")

    def test_a_missing_icon_is_not_an_error(self):
        from wechat_ai_assistant import app_icon

        with mock.patch.object(app_icon, "icon_path", lambda: None):
            self.assertFalse(app_icon.apply_native_icon(4242))

    def test_no_window_means_no_work(self):
        from wechat_ai_assistant import app_icon

        self.assertFalse(app_icon.apply_native_icon(0))
        self.assertEqual(app_icon.find_window_by_pid(0), 0)
        self.assertEqual(app_icon.find_window_by_title(""), 0)

    def test_scheduling_gives_up_quietly(self):
        """窗口一直不出现（Edge 崩了/被关了）也不能一直转。"""
        from wechat_ai_assistant import app_icon

        with mock.patch.object(app_icon, "find_window_by_pid", lambda pid: 0):
            self.assertFalse(app_icon.schedule_icon(pid=123, timeout=0.3))


if __name__ == "__main__":
    unittest.main()
