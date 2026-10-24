"""把"客户机器上到底发生了什么"写成一份能发回来的文本。

## 为什么需要它

界面打不开时，商户**看不到任何可点的东西** —— 日志入口在首页上，而首页正是打不开的
那一页。于是客服只能问"你装的哪一版""是不是公司网络"，全靠猜。

这个模块给程序加一个 `--diagnose`：不打开界面，只把判定这件事需要的**事实**
（版本、装没装 WebView2、代理怎么配的、本机回环通不通、最近日志）写成一个 txt
并用记事本打开，商户直接把这个文件发回来。

## 只写事实，不写猜测

每一条都是当场测出来的（能测就测、测不了写"测不了"），不做推断 —— 推断留给看报告的人。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


def _section(title: str) -> str:
    return f"\n===== {title} =====\n"


def _safe(label: str, func: Any) -> str:
    """跑一条检查；抛异常也照实写下来。"""
    try:
        return f"{label}: {func()}"
    except Exception as exc:  # noqa: BLE001 - 诊断就是要看见异常
        return f"{label}: <出错> {type(exc).__name__}: {exc}"


def _proxy_lines() -> list[str]:
    lines: list[str] = []
    lines.append(_safe("urllib.getproxies()", urllib.request.getproxies))
    lines.append(_safe("proxy_bypass('127.0.0.1')",
                       lambda: urllib.request.proxy_bypass("127.0.0.1")))
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
            try:
                lines.append(f"注册表 {name}: {winreg.QueryValueEx(key, name)[0]}")
            except OSError:
                lines.append(f"注册表 {name}: <未设置>")
        key.Close()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"注册表代理: <读不到> {exc}")
    return lines


def _loopback_lines() -> list[str]:
    """本机回环到底通不通：先绑一个临时端口，再分别用两种方式连它。"""
    lines: list[str] = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        lines.append(f"绑定 127.0.0.1:{port}: 成功")
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=3)
            client.close()
            lines.append("直连（不走代理）: 成功")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"直连（不走代理）: 失败 {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"绑定 127.0.0.1: 失败 {type(exc).__name__}: {exc}"
                     "（本机安全软件可能拦住了程序监听端口）")
    finally:
        server.close()
    return lines


def _port_lines() -> list[str]:
    """有没有哪个地址在监听（顺便看是不是被别的东西占了）。"""
    lines: list[str] = []
    try:
        import psutil

        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and str(conn.laddr.ip) in (
                    "127.0.0.1", "::1", "0.0.0.0", "::"):
                name = ""
                if conn.pid:
                    try:
                        name = psutil.Process(conn.pid).name()
                    except Exception:
                        name = "?"
                lines.append(f"监听 {conn.laddr.ip}:{conn.laddr.port} <- {name} (pid {conn.pid})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"<读不到监听列表> {exc}")
    return lines


def _window_lines() -> list[str]:
    from . import web_window

    lines = [
        _safe("WebView2 Runtime 已安装", web_window.webview2_runtime_present),
        _safe("Edge 可执行文件", web_window.edge_path),
        _safe("pywebview 可导入", web_window.has_webview),
        _safe("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
              lambda: os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "<未设置>")),
    ]
    try:
        from webview.platforms import winforms

        lines.append(f"pywebview 渲染器: {winforms.renderer}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"pywebview 渲染器: <读不到> {type(exc).__name__}: {exc}")
    return lines


def _update_lines(data_dir: Path) -> list[str]:
    from . import updater

    lines = [f"安装目录: {updater.app_dir()}"]
    lines.append(f"pending.json: {(Path(updater.app_dir()) / updater.PENDING_NAME).is_file()}")
    marker = Path(updater.app_dir()) / f"{updater.BLOCKED_PREFIX}*.txt"
    blocked = sorted(str(p.name) for p in Path(updater.app_dir()).glob(marker.name))
    lines.append(f"换版失败记号: {', '.join(blocked) or '无'}")
    lines.append(f"数据目录: {data_dir}")
    try:
        from .paths import scratch_dir

        lines.append(f"中间文件目录: {scratch_dir()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"中间文件目录: <读不到> {exc}")
    return lines


def _log_lines(data_dir: Path, limit: int = 120) -> list[str]:
    from . import diagnostics

    path = Path(diagnostics.path_of(data_dir))
    if not path.is_file():
        return [f"日志文件: {path} <不存在>"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"日志文件: {path} <读不到> {exc}"]
    return [f"日志文件: {path}（最后 {limit} 行）", *text[-limit:]]


def collect(url: str = "", data_dir: Path | None = None) -> str:
    """收集诊断文本。``url`` 给了就顺手探一下这个地址。"""
    from .version import __version__

    data_dir = Path(data_dir) if data_dir else Path(tempfile.gettempdir())
    parts: list[str] = [
        "威信AI智能助理 诊断报告",
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        _section("程序"),
        f"版本: {__version__}",
        f"打包运行: {bool(getattr(sys, 'frozen', False))}",
        f"可执行文件: {sys.executable}",
        f"程序目录: {Path(sys.executable).resolve().parent}",
        f"当前目录: {os.getcwd()}",
        f"Python: {sys.version.split()[0]}",
        f"参数: {' '.join(sys.argv[1:]) or '<无>'}",
        f"系统临时目录: {tempfile.gettempdir()}",
        _section("代理"),
        *_proxy_lines(),
        _section("本机回环"),
        *_loopback_lines(),
        _section("窗口宿主"),
        *_window_lines(),
        _section("更新与数据"),
        *_update_lines(data_dir),
        _section("监听端口"),
        *_port_lines(),
    ]
    if url:
        from .web_window import loopback_opener

        parts += [_section("服务应答"), f"地址: {url}"]
        try:
            with loopback_opener().open(url, timeout=5) as response:  # noqa: S310
                parts.append(f"不走代理访问: HTTP {getattr(response, 'status', '?')}")
        except Exception as exc:  # noqa: BLE001
            parts.append(f"不走代理访问: 失败 {type(exc).__name__}: {exc}")
    try:
        parts += [_section("最近日志"), *_log_lines(data_dir)]
    except Exception as exc:  # noqa: BLE001
        parts.append(f"日志: <读不到> {exc}")
    return "\n".join(str(line) for line in parts) + "\n"


def write_report(text: str | None = None, url: str = "", data_dir: Path | None = None) -> Path:
    """写到 ``<数据目录>/tmp/pyw-diagnose-<时间>.txt`` 并返回路径。

    放数据目录（免安装版就是 ``data\\tmp``）而不是系统临时目录：商户要把它发出去，
    而且换台机器/清理临时目录后还能找到。
    """
    text = text if text is not None else collect(url=url, data_dir=data_dir)
    try:
        from .paths import scratch_dir

        folder = scratch_dir()
    except Exception:
        folder = Path(tempfile.gettempdir())
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        folder = Path(tempfile.gettempdir())
    path = folder / time.strftime("pyw-diagnose-%Y%m%d-%H%M%S.txt")
    path.write_text(text, encoding="utf-8-sig")
    return path


def write_and_open(url: str = "", data_dir: Path | None = None) -> Path:
    """写报告并用记事本打开 —— 窗口版没有控制台，这是唯一能让商户看见的办法。"""
    path = write_report(url=url, data_dir=data_dir)
    try:
        subprocess.Popen(["notepad.exe", str(path)], close_fds=True)
    except Exception:
        pass
    return path


__all__ = ["collect", "write_and_open", "write_report"]
