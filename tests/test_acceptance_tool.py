"""验收工具本身可不可信。

`examples/acceptance.py` 会打出一张全绿的表并说"验收通过"。这种工具最需要
怀疑 —— 一个只会说"都很好"的脚本，和没有脚本一样没用（同 `test_guard_map.py`
的立场）。

这里证明三件事：

1. 它的**界面契约冒烟**是真的能跑通（起服务、读页面、看状态），不是空转；
2. 任何一项失败都会让退出码变成 1，并且点名是哪项 —— 不会"绿着绿着就过了"；
3. `--skip-tests` 真的不跑全套回归（否则快速验收会悄悄跑五分钟）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

import acceptance  # noqa: E402


class UiContractSmokeTests(unittest.TestCase):
    def test_the_ui_smoke_actually_checks_something(self):
        """它必须真的起一个服务、读到页面和状态，而不是直接返回 True。"""
        result = acceptance.check_ui_contract()
        self.assertTrue(result.ok, result.detail)
        # 细节里要有一眼能核对的事实（步数/角色数/当前步），不是一句"通过"。
        self.assertIn("步", result.detail)
        self.assertIn("角色", result.detail)
        self.assertGreater(result.seconds, 0, "瞬间返回说明没真起服务")


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self._originals = {name: getattr(acceptance, name)
                           for name in ("check_hygiene", "check_guard_map",
                                        "check_client", "check_ui_contract", "check_tests")}

    def tearDown(self):
        for name, function in self._originals.items():
            setattr(acceptance, name, function)

    def _pass_all(self):
        for name in ("check_hygiene", "check_guard_map", "check_client", "check_ui_contract"):
            setattr(acceptance, name, lambda n=name: acceptance.Result(n, True, "ok"))

    def test_one_failure_turns_the_verdict_red(self):
        """一项失败 → 退出码 1，并且在最后一段点名是哪一项。"""
        self._pass_all()
        acceptance.check_client = lambda: acceptance.Result("客户端自检", False, "起不来")

        import io
        buffer = io.StringIO()
        stdout, sys.stdout = sys.stdout, buffer
        try:
            code = acceptance.main(["--skip-tests"])
        finally:
            sys.stdout = stdout
        output = buffer.getvalue()
        self.assertEqual(code, 1, "有失败却报通过，这个工具就白做了")
        self.assertIn("验收不通过", output)
        self.assertIn("客户端自检", output, "要说清是哪一项")

    def test_all_green_returns_zero(self):
        self._pass_all()
        import io
        buffer = io.StringIO()
        stdout, sys.stdout = sys.stdout, buffer
        try:
            code = acceptance.main(["--skip-tests"])
        finally:
            sys.stdout = stdout
        self.assertEqual(code, 0)
        self.assertIn("验收通过", buffer.getvalue())

    def test_skip_tests_really_skips_the_suite(self):
        """``--skip-tests`` 不许偷偷去跑 pytest（那是五分钟）。"""
        self._pass_all()

        def explode():
            raise AssertionError("--skip-tests 还是跑了全套回归")

        acceptance.check_tests = explode
        import io
        buffer = io.StringIO()
        stdout, sys.stdout = sys.stdout, buffer
        try:
            code = acceptance.main(["--skip-tests"])
        finally:
            sys.stdout = stdout
        self.assertEqual(code, 0, "没跑测试也应当能给出结论")


if __name__ == "__main__":
    unittest.main()
