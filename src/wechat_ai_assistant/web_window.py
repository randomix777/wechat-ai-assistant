"""给 Web 向导页套一个窗口 —— 和「页面 / 接口」分开的一层。

## 为什么单独一层

页面（``web_wizard_page.py``）和接口（``web_dev.py``）跟"窗口怎么来的"无关。
桌面应用由 Python 本机服务和原生窗口组成；WebView2 的渲染进程由系统单独管理。
页面和业务逻辑不依赖窗口宿主。

## 窗口宿主：一条会自己往下退的链

    内嵌 WebView2 → 系统 Edge ``--app`` → 默认浏览器

产品优先用 ``pywebview``，把 WebView2（Chromium）嵌进应用窗口。但 WebView2
Runtime 是**系统组件**，较旧的 Windows 10 和精简版系统常常没有 —— 那时
pywebview 会静默退回旧版 MSHTML，所以这里宁可报错。

报错不再等于"客户双击没反应"：缺组件时**先用系统 Edge 把界面开出来**
（后端本来就是本机 HTTP，功能一样），同时在后台把便携包里带的安装包静默装上，
下次打开就回到内嵌窗口。装不上、连 Edge 都没有，才退到默认浏览器。

``prefer`` 可以让排障者指定宿主；测试把宿主钉死，不去点真窗口。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: 窗口默认尺寸。1366×768 的老机器上也要放得下。
WINDOW_SIZE = (1120, 780)

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

#: 便携包随包分发的 WebView2 引导安装包（见 ``deploy/fetch_webview2.py``）。
WEBVIEW2_INSTALLER = "MicrosoftEdgeWebview2Setup.exe"

#: 开窗前那次安装最多等多久。等太久（比如商户把 UAC 框晾在那儿）比降级更难受；
#: 超时就用兜底窗口先把界面开出来，后台继续装。
PRE_WINDOW_INSTALL_TIMEOUT = 120.0

#: 静默安装最多等多久：引导包要联网拉 Runtime，慢网络下给足时间。
INSTALL_TIMEOUT_SECONDS = 600

#: 官方 Evergreen 安装说明（装不上时给商户的一句话里有它）。
WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"

#: 缺组件时**现场下载**的那份引导安装包（微软官方跳转，约 1.8 MB）。
#: 便携包里不带它：多一个 1.8 MB 的二进制不如让需要的那台机器自己取。
WEBVIEW2_BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

#: 下载超时。1.8 MB 的东西，120 秒足够，卡住就当这次没装上。
DOWNLOAD_TIMEOUT_SECONDS = 120.0

#: 下载回来的东西至少得有这么大 —— 防止把一张错误页存成 .exe。
MIN_INSTALLER_BYTES = 200_000

#: WebView2 Runtime 在注册表里的产品码（微软文档给的那个）。
_WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def edge_path() -> str:
    """系统 Edge 的可执行文件。找不到就返回空串（不是错误：还有别的宿主）。"""
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("msedge")
    return found or ""


def has_webview() -> bool:
    """pywebview 是否可导入。产品依赖检查和诊断会用到。"""
    if sys.platform != "win32":  # pragma: no cover - 本产品只发 Windows
        return False
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def webview2_runtime_present() -> bool:
    """目标机装没装 WebView2 Runtime。

    **查注册表，不查 pywebview 的 renderer**：renderer 是导入时算一次的，
    本次进程里装完也不会变 —— 用它判"装成功了没有"永远是 False。
    两个位置都要看：按机器安装写 HKLM，按用户安装写 HKCU（不需要管理员权限）。
    """
    if sys.platform != "win32":  # pragma: no cover - 本产品只发 Windows
        return False
    try:
        import winreg

        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
            (winreg.HKEY_CURRENT_USER,
             rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    version = str(winreg.QueryValueEx(key, "pv")[0] or "")
            except OSError:
                continue
            if version:
                return True
    except Exception:
        LOGGER.debug("读取 WebView2 安装状态失败", exc_info=True)
    return False


def download_webview2_installer(dest_dir: Path | None = None) -> Path | None:
    """现场下载微软官方引导安装包。失败返回 None，**从不抛异常**。

    三步把关，缺一不可：HTTPS 取官方跳转地址 → 大小与 PE 头看起来像安装程序
    → Authenticode 签名可信。商户的机器上要**执行**这个文件，不能只看网址
    就当它是微软的。

    不做"发布者是不是 Microsoft"的逐字比对（那要再拉一套证书接口）：签名
    可信 + 微软域名 + HTTPS 已经挡住绝大多数情况，这一点在文档里如实写着。
    """
    import urllib.request

    target_dir = Path(dest_dir) if dest_dir else _scratch("webview2")
    target = target_dir / WEBVIEW2_INSTALLER
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(  # noqa: S310 - 固定的 https 官方地址
                WEBVIEW2_BOOTSTRAPPER_URL, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            data = response.read()
    except Exception:
        LOGGER.exception("下载 WebView2 安装包失败")
        return None
    if len(data) < MIN_INSTALLER_BYTES or not data.startswith(b"MZ"):
        LOGGER.warning("下载回来的不是安装程序：%s 字节", len(data))
        return None
    try:
        target.write_bytes(data)
    except OSError:
        LOGGER.exception("写入安装包失败")
        return None
    if not signature_is_trusted(target):
        LOGGER.warning("下载回来的安装包签名不可信，拒绝执行：%s", target)
        return None
    return target


def signature_is_trusted(path: Path) -> bool:
    """文件的 Authenticode 签名是否有效且可信（Windows 专用，失败即 False）。

    用系统自己的 ``WinVerifyTrust``。``WTD_REVOKE_NONE`` + 只在本地缓存里查
    吊销列表：断网机器上不能因为查不到吊销信息就把安装包判死。
    """
    if sys.platform != "win32":  # pragma: no cover - 本产品只发 Windows
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]

        class FileInfo(ctypes.Structure):
            _fields_ = [("cbStruct", wintypes.DWORD),
                        ("pcwszFilePath", wintypes.LPCWSTR),
                        ("hFile", wintypes.HANDLE),
                        ("pgKnownSubject", ctypes.POINTER(GUID))]

        class TrustData(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("pPolicyCallbackData", ctypes.c_void_p),
                ("pSIPClientData", ctypes.c_void_p),
                ("dwUIChoice", wintypes.DWORD),
                ("fdwRevocationChecks", wintypes.DWORD),
                ("dwUnionChoice", wintypes.DWORD),
                ("pFile", ctypes.POINTER(FileInfo)),
                ("dwStateAction", wintypes.DWORD),
                ("hWVTStateData", wintypes.HANDLE),
                ("pwszURLReference", wintypes.LPCWSTR),
                ("dwProvFlags", wintypes.DWORD),
                ("dwUIContext", wintypes.DWORD),
            ]

        # WINTRUST_ACTION_GENERIC_VERIFY_V2
        action = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                      (ctypes.c_byte * 8)(*[0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE]))
        file_info = FileInfo(cbStruct=ctypes.sizeof(FileInfo),
                             pcwszFilePath=str(path), hFile=None, pgKnownSubject=None)
        data = TrustData(
            cbStruct=ctypes.sizeof(TrustData),
            dwUIChoice=2,               # WTD_UI_NONE：不弹任何窗
            fdwRevocationChecks=0,      # WTD_REVOKE_NONE
            dwUnionChoice=1,            # WTD_CHOICE_FILE
            pFile=ctypes.pointer(file_info),
            dwStateAction=1,            # WTD_STATEACTION_VERIFY
            dwProvFlags=0x1000,         # WTD_CACHE_ONLY_URL_RETRIEVAL：不联网查吊销
        )
        wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
        wintrust.WinVerifyTrust.restype = ctypes.c_long
        wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID),
                                            ctypes.c_void_p]
        result = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
        # 收尾：VERIFY 之后必须 CLOSE，否则状态句柄泄漏。
        data.dwStateAction = 2          # WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
        if result != 0:
            LOGGER.warning("签名校验未通过：0x%08X", result & 0xFFFFFFFF)
            return False
        return True
    except Exception:
        LOGGER.debug("签名校验不可用", exc_info=True)
        return False


def install_webview_runtime(timeout: float = INSTALL_TIMEOUT_SECONDS) -> bool:
    """``ensure_webview_runtime`` 的旧名字。保留是为了不打断既有调用方。

    真正的实现只有一处（含提权重试），别在这里再写一份。
    """
    return ensure_webview_runtime(timeout=timeout)


def fallback_notice(host: str) -> str:
    """兜底宿主下给商户看的一句话。**不承诺装得上**：安装要联网。"""
    where = "系统 Edge" if host == "edge" else "默认浏览器"
    return (f"这台电脑缺少内嵌窗口组件，已用{where}打开界面，功能一样。"
            "软件正在后台自动安装该组件（需要联网），装好后下次打开就恢复内嵌窗口；"
            f"也可以手动安装：{WEBVIEW2_DOWNLOAD}")


def loopback_opener() -> urllib.request.OpenerDirector:
    """一个**不走任何代理**的 opener。

    本机回环永远不该经过代理，但 ``urllib`` 默认会读系统/环境代理，而且
    ``proxy_bypass('127.0.0.1')`` 在没有 ``no_proxy`` 的机器上返回 False ——
    于是装了 Clash/公司代理的电脑上：

    * 我们的就绪探测会被送进代理 → 明明起来了却报"服务没起来"；
    * 浏览器打开 ``127.0.0.1`` 也会被送进代理 → 商户看到"页面不可用"。

    命令行的 curl 通常自己跳过回环，所以这类机器上"用命令行能打开、用软件打不开"
    是典型症状。这里显式关掉代理，和浏览器那边的 ``--no-proxy-server`` 配套。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_until_serving(url: str, *, timeout: float = 15.0) -> bool:
    """等本机服务真的能应答，再开窗口。

    **别把浏览器指到一个还没起来的服务上**：商户看到的是"127.0.0.1 页面不可用"，
    而他没有任何办法判断是软件没起来、还是网络的问题。宁可在这里多等几秒，
    等不到就明确报错（带上日志路径），也不要把死页面摆给人看。
    """
    deadline = time.monotonic() + timeout
    probe = url.split("/wizard")[0].split("/?")[0].rstrip("/") + "/api/state"
    opener = loopback_opener()
    while time.monotonic() < deadline:
        try:
            with opener.open(probe, timeout=2):  # noqa: S310 - 本机回环
                return True
        except Exception:
            time.sleep(0.2)
    LOGGER.error("本机服务在 %.0f 秒内没有应答：%s", timeout, probe)
    return False


def app_title(suffix: str = "") -> str:
    """窗口标题：**带上版本号**。

    两个版本同时开着的时候，窗口长得一模一样 —— 商户和客服都分不清在用的是哪一版
    （"包里打开显示 1.11.1"这类误会就是这么来的）。标题里写死版本，一眼就能对。
    """
    try:
        from .version import __version__

        base = f"微信AI智能助理 {__version__}"
    except Exception:  # pragma: no cover - 极端裁剪环境
        base = "微信AI智能助理"
    return f"{base}{suffix}"


def notify_and_exit(detail: str) -> None:
    """弹一句中文并结束（窗口版没有控制台，弹窗是唯一能看见的方式）。"""
    try:
        import ctypes

        MB_ICONINFORMATION = 0x00000040
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        ctypes.windll.user32.MessageBoxW(
            None, detail, app_title(), MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)
    except Exception:
        LOGGER.debug("提示框没弹出来", exc_info=True)


def choose_host(prefer: str = "") -> str:
    """默认选择嵌入式 WebView2；Edge/浏览器须由排障代码显式指定。"""
    return prefer or "pywebview"


def open_window(url: str, title: str = "", *, prefer: str = "") -> str:
    """打开窗口并**阻塞到窗口关闭**。返回实际用的宿主。

    返回值不是装饰：调用方（开发服务器）靠它决定"窗口关了要不要跟着退出"，
    也靠它决定要不要告诉商户"这一轮用的是兜底窗口"—— 见 :func:`fallback_notice`。
    """
    title = title or app_title()
    host = choose_host(prefer)
    LOGGER.info("窗口宿主：%s -> %s", host, url)
    if host != "pywebview":
        return _open_explicit(host, url)
    # **先装再开窗**。客户机器缺 WebView2 时，Electron 是自带内核、Tauri 是安装器
    # 先装好 —— 只有我们这里是"起不来再在后台偷偷补"，于是第一轮永远是降级体验，
    # 补装如果因为权限/断网失败，就永远停在兜底上。这里改成开窗之前先装。
    if not webview2_runtime_present():
        # 装的时候屏幕上得有东西：什么都不发生，商户会以为双击没反应，
        # 于是反复双击，安装反而被弄坏。
        from .window_notice import WaitingNotice

        with WaitingNotice("正在安装界面组件，请稍候…\n首次启动需要联网，约 1–2 分钟。"
                           "\n装好后会自动打开窗口。"):
            ensure_webview_runtime(timeout=PRE_WINDOW_INSTALL_TIMEOUT)
    try:
        return _open_webview(url, title)
    except Exception as exc:
        # 仍然起不来：先把界面用系统 Edge 开出来，再在后台补一次（有些机器上
        # 需要联网重试），绝不能让客户"双击没反应"。
        LOGGER.exception("嵌入式 WebView2 窗口启动失败，改用兜底宿主")
        threading.Thread(target=ensure_webview_runtime, name="webview2-retry",
                         daemon=True).start()
        return _open_fallback(url)


def ensure_webview_runtime(timeout: float = INSTALL_TIMEOUT_SECONDS) -> bool:
    """缺 WebView2 时**当场装上**，返回装完了没有。从不抛异常。

    这是"启动时把运行时装好"的那一步（Electron/Tauri 的等价物）。安装器需要
    管理员权限才写机器级目录，所以先按当前权限试一次，不行再走 UAC 提权。
    装不上不是灾难：调用方还有系统 Edge 兜底，只是体验降级。
    """
    if webview2_runtime_present():
        return True
    installer = download_webview2_installer()
    if installer is None:
        LOGGER.warning("拿不到 WebView2 安装包，这次用兜底窗口")
        return False
    if _run_installer(installer, timeout=timeout):
        LOGGER.info("WebView2 已装好，使用内嵌窗口")
        return True
    LOGGER.warning("WebView2 没装上，这次用兜底窗口（下次启动会再试）")
    return False


def _run_installer(installer: Path, *, timeout: float = INSTALL_TIMEOUT_SECONDS) -> bool:
    """跑安装器：先按当前权限，失败再提权。以注册表为准，不看退出码。"""
    try:
        subprocess.run(  # noqa: S603 - 路径是我们刚下载并验签的临时文件
            [str(installer), "/silent", "/install"],
            check=False, timeout=timeout, close_fds=True,
        )
    except Exception:
        LOGGER.exception("WebView2 安装器执行失败（当前权限）")
    if webview2_runtime_present():
        return True
    return _run_installer_elevated(installer, timeout=timeout)


def _run_installer_elevated(installer: Path, *, timeout: float = INSTALL_TIMEOUT_SECONDS) -> bool:
    """提权再试一次（UAC）。安装器要写机器级目录时必须走这条。

    ``ShellExecuteW`` 不带进程句柄，所以只能**轮询注册表**等它落地；
    商户拒绝 UAC 或等超时都算没装上，交给兜底窗口。
    """
    if sys.platform != "win32":  # pragma: no cover - 产品只发 Windows
        return False
    try:
        import ctypes

        # SW_SHOWNORMAL=1；runas 会弹标准的 UAC 确认框，这是唯一能写机器级目录的办法。
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", str(installer), "/silent /install", None, 1)
        if int(result) <= 32:  # ShellExecute 的返回值 <=32 表示失败
            LOGGER.warning("提权安装没有启动：ShellExecuteW=%s", result)
            return False
    except Exception:
        LOGGER.exception("提权安装失败")
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if webview2_runtime_present():
            return True
        time.sleep(3)
    return False


def _open_explicit(host: str, url: str) -> str:
    if host == "edge":
        _open_edge(url)
        return "edge"
    if host == "browser":
        _open_browser(url)
        return "browser"
    raise ValueError(f"未知窗口宿主：{host}")


def _open_fallback(url: str) -> str:
    """兜底：系统 Edge ``--app``（还是独立窗口），再退到默认浏览器。"""
    try:
        _open_edge(url)
        return "edge"
    except Exception:
        LOGGER.exception("系统 Edge 也起不来，改用默认浏览器")
        # 这条路上要**一直挂着**：浏览器只拿到一个地址，我们没有任何句柄知道
        # 商户什么时候关掉它。直接返回的话调用方会以为"窗口关了"，把服务一起
        # 停掉 —— 页面在商户眼前白掉。宁可留一个没有窗口的进程（日志里写明）。
        _open_browser(url, block=True)
        return "browser"


def _open_webview(url: str, title: str) -> str:
    if sys.platform != "win32":  # pragma: no cover - 产品只发 Windows
        raise RuntimeError("嵌入式 WebView2 窗口仅支持 Windows 客户端")

    # 内嵌窗口同样只连本机：装了 Clash / 公司代理的电脑上，WebView2 默认会读系统
    # 代理，把 127.0.0.1 也送进代理，页面直接打不开。这个环境变量是 WebView2 官方
    # 支持的附加启动参数，必须在创建窗口**之前**设好。
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--no-proxy-server")

    import webview

    # pywebview can silently select the legacy MSHTML engine when WebView2 is
    # missing. Check its resolved renderer before showing a window so the app
    # never degrades to IE compatibility mode.
    from webview.platforms import winforms

    if winforms.renderer != "edgechromium":
        raise RuntimeError("未检测到 WebView2 Runtime，当前 pywebview 会回退到旧版 MSHTML")

    # Pythonw's executable identity makes Windows group this window with Python/IDLE
    # and reuse its taskbar icon. Set the product identity before creating any HWND.
    from .app_icon import (
        apply_native_icon,
        icon_path,
        set_app_user_model_id,
        set_window_user_model_id,
    )

    set_app_user_model_id()
    window = webview.create_window(title, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1])

    def set_taskbar_identity(window=window) -> None:
        native = getattr(window, "native", None)
        if native is not None:
            try:
                hwnd = int(native.Handle.ToInt32())
            except Exception:
                LOGGER.debug("读取 pywebview HWND 失败", exc_info=True)
                return
            if not set_window_user_model_id(hwnd):
                LOGGER.warning("未能为窗口设置产品任务栏身份：hwnd=%s", hwnd)
            if not apply_native_icon(hwnd):
                LOGGER.warning("未能在窗口显示前挂载产品图标：hwnd=%s", hwnd)

    # before_show is synchronous in pywebview's WinForms backend and runs after
    # the HWND exists but before it becomes visible to the taskbar. Set both the
    # shell identity and native icons before Explorer reads the initial button.
    window.events.before_show += set_taskbar_identity
    # WinForms 后端会把 start(icon=...) 写入原生 Form.Icon；这比窗口显示后再用
    # WM_SETICON 补挂更可靠，尤其能保证任务栏第一次取图标时就拿到产品图标。
    ico = icon_path()
    webview.start(gui="edgechromium", icon=str(ico) if ico is not None else None)
    if webview.renderer != "edgechromium":
        raise RuntimeError(f"pywebview 使用了非预期的渲染器：{webview.renderer}")
    return "pywebview"


def _scratch(name: str) -> Path:
    """中间文件目录（跟着数据目录走，见 ``paths.scratch_dir``）。

    拿不到就退回系统临时目录 —— 中间文件写不进去不能拦住启动。
    """
    try:
        from .paths import scratch_dir

        return scratch_dir(name)
    except Exception:
        fallback = Path(tempfile.gettempdir()) / "pyw" / name
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


def _edge_profile() -> Path:
    """这次启动专用的 Edge 配置目录（每次不同，见 ``_open_edge`` 的说明）。"""
    profiles = _scratch("edge-profile")
    _drop_stale_profiles(profiles)
    profile = profiles / f"p{os.getpid()}"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _drop_stale_profiles(profiles: Path, *, keep_days: float = 2.0) -> None:
    """清掉两天前留下的浏览器配置目录。

    每次启动一个独立 profile 是必要的（否则 Edge 会把窗口交给已在跑的进程），
    代价是它不会自己消失 —— 不清就一天一个几百 MB 的目录堆在数据目录里。
    """
    import time as _time

    deadline = _time.time() - keep_days * 86400
    try:
        for child in profiles.iterdir():
            if child.is_dir() and child.stat().st_mtime < deadline:
                shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass


def _open_edge(url: str) -> None:
    """用 Edge 的 ``--app`` 起一个无地址栏的窗口，并等它真正关掉。

    ## 两个坑（都踩过一次，换来线上"127.0.0.1 页面不可用"）

    1. **``--user-data-dir`` 必须每次启动都不同。** 用固定目录时，如果上一次的
       Edge 还占着它，新启动的 ``msedge.exe`` 只会把窗口交给**已在跑的那个浏览器
       进程**然后自己立刻退出（实测 0.3 秒）。我们等的是这个启动器，于是误判
       "窗口已关"，调用方把本机服务一关，商户眼前就变成"页面不可用"。
    2. **启动器退出 ≠ 窗口关了。** 所以启动器退出后还要看"我们的 profile 还有没有
       浏览器在用"：有就继续等，没有才收工；连一个都找不到时**宁可继续挂着**，
       也不能把服务关掉 —— 死页面比多留一个进程严重得多。
    """
    exe = edge_path()
    if not exe:
        raise FileNotFoundError("没有找到 Edge")
    profile = _edge_profile()
    process = subprocess.Popen([  # noqa: S603 - 路径来自本机固定候选，不是输入
        exe,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # 商户机器上装了 Clash / 公司代理时，浏览器会把 127.0.0.1 也送进代理，
        # 于是页面变成"不可用"。我们的界面只连本机，关掉代理没有任何副作用。
        "--no-proxy-server",
        f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
    ], close_fds=True)
    # 窗口是 Edge 的进程，图标得我们挂上去（否则任务栏显示的是 Edge）。
    from .app_icon import set_window_icon_async

    set_window_icon_async(pid=process.pid)
    process.wait()
    LOGGER.info("Edge 启动器已退出（pid=%s），开始盯着窗口本身", process.pid)
    _wait_for_browser(profile)


def _wait_for_browser(profile: Path, *, grace: int = 3) -> None:
    """等到**用这个 profile 的浏览器**彻底退出。

    ``grace`` 是连续几次看不到浏览器才认账（每 2 秒看一次）：刚关窗那一刻进程
    可能还在收尾，看一次就下结论会提前把服务关掉。
    连一次都没看到过就**一直等**：宁可留一个没有窗口的进程（日志里写明），
    也不能把商户眼前的页面弄死。
    """
    seen = False
    missing = 0
    while True:
        if browser_uses_profile(profile):
            seen = True
            missing = 0
        elif seen:
            missing += 1
            if missing >= grace:
                LOGGER.info("窗口已关闭，退出")
                return
        else:
            missing += 1
            if missing == 15:  # 30 秒都没见到浏览器：大概率已被接管，别关服务
                LOGGER.warning("没找到使用 %s 的浏览器进程；为免页面失效，本进程继续运行", profile)
        time.sleep(2)


def browser_uses_profile(profile: Path) -> bool:
    """有没有浏览器进程正在用这个 profile（Edge/Chrome 都认）。

    用 ``psutil`` 按命令行匹配：``--user-data-dir=<profile>`` 是每次启动唯一的，
    所以"还剩谁在用"能准确问到。
    """
    try:
        import psutil
    except Exception:  # pragma: no cover - 依赖缺失时按"没有"处理
        return False
    needle = str(profile).lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = str((proc.info or {}).get("name") or "").lower()
            if name not in ("msedge.exe", "msedge", "chrome.exe", "chrome"):
                continue
            command = " ".join((proc.info or {}).get("cmdline") or []).lower()
            if needle in command:
                return True
        except Exception:  # 进程刚好退出（NoSuchProcess 等）
            continue
    return False


def _open_browser(url: str, *, block: bool = False) -> None:
    """默认浏览器打开。

    ``block=False``（排障用的显式宿主）返回即走，调用方自己决定要不要留着进程；
    兜底路径要 ``block=True``，否则服务会跟着"窗口已关闭"一起停掉。
    """
    import webbrowser

    webbrowser.open(url)
    if block:
        LOGGER.info("已用默认浏览器打开 %s；关闭标签页后本进程仍在，可直接结束它", url)
        wait_forever()


def wait_forever() -> None:
    """一直等到进程被杀掉。

    单独一个函数是为了能测：兜底浏览器那条路必须"不返回"，而测试里不能真等。
    """
    threading.Event().wait()


__all__ = ["WINDOW_SIZE", "app_title", "browser_uses_profile", "choose_host",
           "download_webview2_installer", "edge_path", "ensure_webview_runtime",
           "fallback_notice", "has_webview", "install_webview_runtime", "loopback_opener",
           "notify_and_exit", "open_window", "signature_is_trusted",
           "wait_until_serving", "webview2_runtime_present"]
