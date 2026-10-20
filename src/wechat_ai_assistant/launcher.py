"""双击启动入口：不依赖 PYTHONPATH、环境变量或开发环境。

``python -m wechat_ai_assistant.merchant_app`` 需要 ``PYTHONPATH=src``，那是**开发**用的。
商户双击时不会设这个变量，所以这里自己把路径准备好。

同时负责本地数据目录：打包便携版使用 EXE 同级的 ``data`` 目录；
源码运行使用 ``%LOCALAPPDATA%\\wechat-ai-assistant``，并兼容旧 ``pyweixin`` 数据。
所有依赖缺失都会给出**中文**说明。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

#: 打包成窗口版（``console=False``）时 ``sys.stdout``/``sys.stderr`` 是 ``None``。
#: 库里有大量 ``print``（监听、导出、发送），没有兜底就会 ``AttributeError``
#: 中断自动化。这里记住「原本有没有控制台」，由 :func:`_fail` 决定打印还是弹窗。
_HAS_CONSOLE = sys.stdout is not None


def _ensure_streams() -> None:
    """窗口版没有控制台，给 ``print`` 一个黑洞，别让它们把程序打断。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def repo_root() -> Path:
    """定位仓库根目录：``<root>/src/wechat_ai_assistant/launcher.py``。"""
    return Path(__file__).resolve().parents[2]


def bootstrap_paths() -> list[str]:
    """把 ``src`` 和 ``packages/*/src`` 加进 ``sys.path``。幂等。"""
    root = repo_root()
    candidates = [root / "src"]
    packages = root / "packages"
    if packages.is_dir():
        candidates.extend(sorted(child / "src" for child in packages.iterdir() if child.is_dir()))
    added: list[str] = []
    for path in candidates:
        text = str(path)
        if path.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
            added.append(text)
    return added


def data_dir() -> Path:
    """商户的本地数据目录。断电或换用户也不会丢。"""
    # This function is also called by users who import launcher directly.
    bootstrap_paths()
    from wechat_ai_assistant.paths import default_data_dir

    return default_data_dir()


def check_dependencies() -> list[tuple[str, str]]:
    """返回缺失依赖的 ``(包名, 中文说明)``。空列表表示齐全。

    **tkinter 不在这张表里**：产品界面已经是 Web（``web_app``），Tk 只剩更新
    闪屏那 3 秒，缺了也只是"换版时少一个提示"，不该拦住启动。
    """
    required = [
        ("sqlite3", "本地数据库"),
        ("webview", "内嵌桌面窗口（WebView2）"),
        ("pywinauto", "微信窗口读取"),
        ("win32gui", "微信窗口操作"),
        ("psutil", "进程检测"),
    ]
    missing: list[tuple[str, str]] = []
    for name, description in required:
        try:
            __import__(name)
        except ImportError:
            missing.append((name, description))
    return missing


def _show_update_splash(version: str) -> None:
    """更新闪屏：告诉商户正在换版、完成后自动打开，不要重复双击。

    打包后没有控制台，print 等于没说。Tk 顶 3 秒，更新脚本接管后
    本进程退出，闪屏跟着消失。任何异常都静默跳过 —— 闪屏不能挡更新。

    闪屏是"窗口还没起来"之前的兜底，所以它永远用 Tk（标准库），
    不依赖界面那套。配色写在这里：主题模块只为 Tk 界面存在，已经删了。
    """
    try:
        import tkinter as tk
    except Exception:
        return
    APP_BG, BRAND, CARD_BG, FONT, INK = (
        "#f2f4f7", "#0e9f6e", "#ffffff", "Microsoft YaHei", "#1a1d21")
    root = None
    try:
        root = tk.Tk()
        try:  # 更新窗口也要是自家脸，不然看起来像个可疑弹窗。
            from wechat_ai_assistant.app_icon import set_window_icon
            set_window_icon(root)
        except Exception:
            pass
        root.title("微信AI智能助理 - 正在更新")
        root.resizable(False, False)
        root.configure(background=CARD_BG)
        root.attributes("-topmost", True)
        bar = tk.Frame(root, background=BRAND, height=6)
        bar.pack(fill="x")
        title = tk.Label(root, text="微信AI智能助理", font=(FONT, 12, "bold"),
                         background=CARD_BG, foreground=BRAND)
        title.pack(pady=(16, 2))
        label = f"正在更新到 v{version or '新版本'}，完成后自动打开，请稍候…\n请不要重复双击。"
        tk.Label(root, text=label, font=(FONT, 10),
                 background=CARD_BG, foreground=INK,
                 padx=28, pady=(4, 20)).pack()
        root.update_idletasks()
        width, height = root.winfo_reqwidth(), root.winfo_reqheight()
        pos_x = (root.winfo_screenwidth() - width) // 2
        pos_y = (root.winfo_screenheight() - height) // 2
        root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        root.update()
        import time as _time
        deadline = _time.time() + 3.0
        while _time.time() < deadline:
            try:
                root.update()
            except Exception:
                break
            _time.sleep(0.05)
    except Exception:
        pass
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


def _check_and_apply_update() -> bool:
    """启动前只应用**已经下好**的暂存更新；启动路径上不再联网。

    返回 True 表示更新脚本已接管当前进程，调用方应结束旧进程；
    False 表示没有暂存更新（或不可用），继续启动当前客户端。

    「有没有新版」早就挪到界面起来之后，由
    :func:`wechat_ai_assistant.updater.check_in_background` 在后台线程里查、下、
    暂存了 —— 商户双击图标就该看见窗口，不该先等一个几十兆的下载。新版下好
    会在顶栏留一句「下次打开自动更新」，下次启动由这里接手换版。
    """
    try:
        from wechat_ai_assistant.updater import (
            apply_pending_update,
            is_frozen_app,
            pending_update,
        )
        if not is_frozen_app():
            return False
        # 之前下载完但尚未来得及替换：先应用它，避免重复下载。
        pending = pending_update()
        if pending is None:
            return False
        _show_update_splash(str(pending.get("version") or ""))
        return apply_pending_update()
    except Exception:
        return False


def _show_error_dialog(title: str, detail: str) -> None:
    """窗口版没有控制台，报错只能弹窗 —— 否则商户看到的是「双击没反应」。

    用 Win32 的 ``MessageBoxW``，**不用 tkinter**：产品界面已经是 Web，启动兜底
    也只需要一句中文。``MB_TOPMOST | MB_SETFOREGROUND`` 是必须的 —— 弹窗藏在
    别的窗口后面，等于没弹。任何异常都咽掉：报错本身不能再报错。
    """
    try:
        import ctypes

        MB_ICONERROR = 0x00000010
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        ctypes.windll.user32.MessageBoxW(
            None, str(detail or "").strip(), str(title or "微信AI智能助理"),
            MB_ICONERROR | MB_TOPMOST | MB_SETFOREGROUND,
        )
    except Exception:
        pass


def _fail(title: str, detail: str) -> int:
    detail = detail.strip()
    if _HAS_CONSOLE:
        try:
            print("=" * 56)
            print(f"  {title}")
            print()
            for line in detail.splitlines():
                print(f"  {line}")
            print()
            print("=" * 56)
            input("按回车键关闭…")
            return 1
        except Exception:
            # 控制台其实不可用（流被丢弃/重定向）。退回弹窗，别把报错吞掉。
            pass
    _show_error_dialog(title, detail)
    return 1


def main(argv: list[str] | None = None) -> int:
    """启动商户软件。``--check`` 只做自检。"""
    _ensure_streams()
    if sys.version_info < (3, 10):
        return _fail(
            "需要更新 Python",
            "本软件需要 Python 3.10 或更高版本。\n请到 python.org 下载安装后重试。",
        )

    bootstrap_paths()
    # 启动路径上只做本地的事：有下好的新版就先换掉再打开。查新版、下包挪到
    # 界面起来之后的后台线程（见 web_server.start_background_update_check），
    # 所以这里的失败或"没有新版"都不影响打开软件。
    try:
        args = list(argv if argv is not None else sys.argv[1:])
        # 诊断模式：界面打不开时，商户**没有任何可点的东西**（日志入口在首页上，
        # 而首页正是打不开的那一页）。这里不开界面，只把事实写成一份 txt 并打开，
        # 商户直接发回来 —— 否则客服只能隔着电话猜。
        if "--diagnose" in args:
            from wechat_ai_assistant import window_diagnosis

            path = window_diagnosis.write_and_open(data_dir=data_dir())
            print(f"诊断报告：{path}")
            return 0
        if "--check" not in args and _check_and_apply_update():
            return 0
    except Exception:
        pass
    try:
        import wechat_ai_assistant  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        return _fail(
            "软件文件不完整",
            f"没有找到本软件的核心文件：\n{exc}\n\n请重新解压便携版文件夹后重试。",
        )

    missing = check_dependencies()
    if missing:
        lines = ["这台电脑上缺少以下组件，无法运行：", ""]
        for name, description in missing:
            lines.append(f"· {description}（{name}）")
        lines += [
            "",
            "请重新下载并解压完整的便携版软件包。",
        ]
        return _fail("缺少运行组件", "\n".join(lines))

    try:
        # 按绝对模块名导入：本文件既可能被 ``python -m wechat_ai_assistant.launcher``
        # 启动，也可能被 ``python path\to\launcher.py`` 直接执行。后者没有
        # 包上下文，相对导入会失败。
        #
        # 界面是 Web 那套（``web_app``：本机 HTTP + 应用窗口）。业务逻辑在
        # ``MerchantApplication`` 里，和界面无关 —— 换界面不动它。
        from wechat_ai_assistant.web_app import main as app_main
    except Exception as exc:  # pragma: no cover - defensive
        return _fail("软件启动失败", f"{exc}\n\n请把这段信息发给客服。")

    if "--data-dir" not in args:
        try:
            args = ["--data-dir", str(data_dir())] + args
        except RuntimeError as exc:
            return _fail("便携数据迁移失败", str(exc))
    try:
        return app_main(args)
    except Exception as exc:
        # 走到这里说明**连兜底窗口都没起来**：内嵌 WebView2 失败会自动改用系统
        # Edge，Edge 也没有才退到默认浏览器 —— 三条路全断才弹这句话。
        from wechat_ai_assistant.web_window import WEBVIEW2_DOWNLOAD

        return _fail(
            "客户端窗口启动失败",
            f"{exc}\n\n"
            "这台电脑没能打开界面窗口。请先安装 WebView2 运行时（免费，微软官方）：\n"
            f"{WEBVIEW2_DOWNLOAD}\n\n"
            "本软件包里已带安装包，也可以直接双击安装后重试：\n"
            "MicrosoftEdgeWebview2Setup.exe（在解压出来的文件夹里）。",
        )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
