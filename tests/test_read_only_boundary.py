"""产品不许往微信发消息 —— 这条边界要能从**制品**里验证。

## 为什么这是最该守住的一条边界

产品对微信只做四件事：读消息、整理、记单、导出。**不发消息。**
`runmode.py` 写着这件事，`application.py` 也把自动发送拆了（回执只整理、不发送）。

但注释不是证据。真收到律师函的时候，"这个工具不会替我给别人发微信"必须是一条
**从二进制里能验证的事实**，而不是文档里的一句承诺 —— 所以这条边界要变成三道
自动检查：

1. **源码层**：产品代码不许 import 那套点击自动化（`WeChatAuto`：点"发送"、
   自动加好友、自动回复）。`src/wechat_ai_assistant/__init__.py` 也不再导出它。
2. **spec 层**：`deploy/merchant.spec` 显式排除它 —— 打包时再拦一道。
3. **制品层**：打出来的 exe 里不许有它（`PYZ` 目录逐个模块名查）。

外加一条更细的：**进了包的模块里，不许出现"往聊天输入框里写东西"**。
唯一用到那个控件（`CurrentChatEdit`）的功能是"在群里发接龙"，它已经删掉了
（`utils.message_chain`）。剩下的按键操作都在**读**（点开消息、翻页、选成员、
截屏比对）和**在搜索框里打字**（公众号/视频号/小程序搜索）—— 那不是发消息。

## 为什么以前没出事

因为 `WeChatAuto` 只被 `__init__.py` 的 `__getattr__` 懒加载指着，而 PyInstaller
只收**静态可见**的 import，所以它一直没进包。这是"恰好"，不是"保证"：
哪天有人为了加个功能 import 一下，发送能力就静悄悄进包了。
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PACKAGE = ROOT / "src" / "wechat_ai_assistant"
SPEC = ROOT / "deploy" / "merchant.spec"
DIST = ROOT / "dist"

#: 带点击发送的那套自动化模块。它**不许**进制品。
CLICK_AUTOMATION = "wechat_ai_assistant.WeChatAuto"

#: 聊天输入框的控件描述符。只有"往聊天里写东西"的功能才用得上它 ——
#: 而那种功能产品一个都没有。
CHAT_INPUT = "CurrentChatEdit"


def _excluded_modules() -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("EXCLUDES = [")
    block = text[start:text.index("]", start)]
    return set(re.findall(r'"([^"]+)"', block))


def _shipped_modules() -> list[Path]:
    """进包的模块 = 包里的模块 - spec 显式排除的那些。

    排除按**顶层名**算（``wechat_ai_assistant.WeChatAuto`` 排掉的是那个文件，
    ``pywechat`` 排掉的是另一整个包）。
    """
    excluded = _excluded_modules()
    files = sorted(PACKAGE.glob("*.py"))
    out = []
    for path in files:
        dotted = f"wechat_ai_assistant.{path.stem}"
        if dotted in excluded or path.stem in excluded:
            continue
        out.append(path)
    return out


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            base = node.module or ""
            for alias in node.names:
                names.add(f"{prefix}{base}.{alias.name}" if base else f"{prefix}{alias.name}")
                names.add(f"{prefix}{base}" if base else prefix)
    return names


class ClickAutomationBoundaryTests(unittest.TestCase):
    def test_no_shipped_module_imports_the_click_automation(self):
        """产品代码一旦 import 它，PyInstaller 就会把它收进制品。"""
        offenders = []
        for path in _shipped_modules():
            for name in _imported_modules(path):
                if "WeChatAuto" in name or name.startswith("pywechat"):
                    offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [],
                         "这些模块带着点击发送的能力，不许进制品：" + "；".join(offenders))

    def test_the_package_does_not_export_it_any_more(self):
        """顶层懒加载是最大的坑：它看不见静态 import，却能触发一次运行时 import。

        查的是 ``_EXPORTS`` 的**值**，不是文件里出现过这个词 —— 注释里
        解释"为什么删掉它"是应该的，删掉才是。
        """
        tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        exports: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_EXPORTS" not in targets or not isinstance(node.value, ast.Dict):
                continue
            for key, value in zip(node.value.keys, node.value.values):
                if isinstance(key, ast.Constant) and isinstance(value, ast.Constant):
                    exports[str(key.value)] = str(value.value)
        self.assertTrue(exports, "没读到 _EXPORTS —— 顶层导出表改名了？")
        offenders = {name: module for name, module in exports.items()
                     if "WeChatAuto" in module}
        self.assertEqual(offenders, {},
                         "顶层不该再导出那套点击自动化 —— 导出即等于把它请进制品")

    def test_the_spec_excludes_it(self):
        """打包时再拦一道。"""
        excluded = _excluded_modules()
        self.assertIn(CLICK_AUTOMATION, excluded,
                      "spec 必须显式排除点击自动化模块")
        self.assertIn("pywechat", excluded,
                      "spec 必须显式排除另一整套上游自动化包")


class ChatWriteBoundaryTests(unittest.TestCase):
    def test_no_shipped_module_writes_into_the_chat_box(self):
        """往聊天输入框里写东西 = 能替用户发消息。进包的模块里不许有。"""
        offenders = []
        for path in _shipped_modules():
            if path.name == "Uielements.py":
                continue  # 控件描述符本身，只是名字，不是行为
            text = path.read_text(encoding="utf-8")
            if CHAT_INPUT in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [],
                         "这些进包的模块碰了聊天输入框：" + "；".join(offenders))

    def test_the_product_still_declares_itself_read_only(self):
        """这句话是给商户看的，也是给律师看的 —— 别让它悄悄变成假话。"""
        runmode = (PACKAGE / "runmode.py").read_text(encoding="utf-8")
        self.assertIn("不会", runmode)
        self.assertIn("发送任何消息", runmode)


class BuiltPackageTests(unittest.TestCase):
    """制品层：打出来的东西里没有那套能力。

    没构建过就跳过 —— 源码层的两道检查不依赖构建。
    """

    def _built_executables(self) -> list[Path]:
        return sorted(DIST.glob("*/*.exe")) if DIST.is_dir() else []

    def test_the_built_package_has_no_click_automation(self):
        executables = self._built_executables()
        if not executables:
            self.skipTest("还没有构建产物（dist/ 是空的）")
        try:
            from PyInstaller.archive.readers import CArchiveReader
        except ImportError:  # pragma: no cover - 开发机没装 PyInstaller
            self.skipTest("这个环境没有 PyInstaller，看不了制品")
        for executable in executables:
            with self.subTest(executable=executable.name):
                archive = CArchiveReader(str(executable))
                modules = list(archive.open_embedded_archive("PYZ.pyz").toc)
                offenders = [m for m in modules if "WeChatAuto" in m or m.startswith("pywechat")]
                self.assertEqual(offenders, [],
                                 "制品里带着点击自动化：" + "；".join(offenders))


if __name__ == "__main__":
    unittest.main()
