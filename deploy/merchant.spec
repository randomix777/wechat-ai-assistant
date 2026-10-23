# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格：把商户软件打成一个可双击的 exe。

    python deploy/build_windows.py

产物：dist/微信AI智能助理/（onedir）或 dist/微信AI智能助理.exe（onefile）。

**onedir 是默认选择**：微信自动化依赖 pywinauto/pywin32 的原生 DLL 和
隐藏导入，onefile 每次启动都要把它们解包到临时目录，启动慢且更容易被杀软
误报。onedir 启动快，行为可预期。
"""
import sys
from pathlib import Path

# A PyInstaller spec file is exec'd, not imported, so ``__file__`` does not
# exist. PyInstaller injects ``SPECPATH`` (the directory holding this file).
ROOT = Path(SPECPATH).resolve().parents[0]
sys.path.insert(0, str(ROOT / "src"))

# 微信自动化与界面依赖的原生/隐藏导入。漏掉任何一个都会在商户机器上
# 表现为「双击没反应」或「一开就崩」，而不是在打包机上暴露。
HIDDEN = [
    "pywinauto",
    "pywinauto.controls",
    "pywinauto.controls.uiawrapper",
    "pywinauto.timings",
    "pywinauto.application",
    # pywebview selects its WinForms backend dynamically at runtime. Include
    # it explicitly so the frozen app can host WebView2 instead of Edge --app.
    "webview",
    "webview.platforms.winforms",
    "win32com.propsys.propsys",
    "win32com.propsys.pscon",
    # pywinauto's UIA wrapper imports numpy and comtypes at module load time.
    # Excluding them makes the built exe fail on first import, which the
    # merchant sees as "双击没反应" -- not a build-machine-only problem.
    "numpy",
    "numpy.core",
    "comtypes",
    "comtypes.client",
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "win32clipboard",
    "win32crypt",
    "win32service",
    "pywintypes",
    "pythoncom",
    "psutil",
    "pyautogui",
    "pydoc_data",
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "sqlite3",
    "business_agent",
    "business_agent.backends",
    "business_agent.contracts",
    "business_agent.core",
    "order_agent",
    # 页面模块是**按名字动态加载**的（web_server.PAGE_SOURCES → import_module），
    # PyInstaller 静态分析看不见它们。漏了这两个，打包版取 /wizard 会抛
    # ModuleNotFoundError，浏览器那头就是 ERR_EMPTY_RESPONSE（客户看到的
    # "empty response"）—— 而源码运行完全正常，所以这个问题躲了很久。
    "wechat_ai_assistant.web_wizard_page",
    "wechat_ai_assistant.web_page",
]

EXCLUDES = [
    # Test and developer tooling stays out of the shipped product. numpy is
    # deliberately NOT excluded -- pywinauto needs it.
    "pytest", "pydoc", "doctest", "pip", "IPython", "notebook",
    "pandas", "matplotlib", "setuptools",
    # The click-automation library: clicking the "send" button, auto-reply,
    # auto-add-friend. The product does not use it -- reading goes through the
    # local decrypted database (wcdb) and nothing in the product
    # writes to WeChat -- and it must not reach the merchant's machine.
    #
    # "This tool does not send messages on my behalf" has to be a fact about
    # the binary, not a promise in a docstring. Excluding it here is the last
    # line of defence; `tests/test_read_only_boundary.py` is the first, and
    # `src/wechat_ai_assistant/__init__.py` no longer exports it at all.
    "wechat_ai_assistant.WeChatAuto",
    "pywechat",
]

#: 图标干两件事：写进 exe 资源（任务栏 / Alt-Tab / 文件图标），并作为 datas
#: 随包分发，运行时的窗口图标由 ``app_icon.set_window_icon`` 从这里读。
#: 由 ``python deploy/build_icon.py --final b`` 生成，换方向改这一个文件即可。
ICON = ROOT / "deploy" / "assets" / "app.ico"
DATAS = [(str(ICON), "assets")] if ICON.is_file() else []

# 把版本号写进 exe 的文件属性（右键 → 属性 → 详细信息）。
# 界面打不开时，客服就靠这一行确认客户装的是哪一版 —— 比"你双击诊断.bat"更省事，
# 而且老版本没有诊断脚本也能查。版本号**只从 version.py 取**，不在这里写第二份。
from wechat_ai_assistant.version import __version__  # noqa: E402

_parts = [int(p) for p in __version__.split(".") if p.isdigit()]
while len(_parts) < 4:
    _parts.append(0)
VERSION_TUPLE = tuple(_parts[:4])
VERSION_FILE = ROOT / "build" / "pyw_version_info.txt"
VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
VERSION_FILE.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={VERSION_TUPLE}, prodvers={VERSION_TUPLE},
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('CompanyName', 'JYI'),
      StringStruct('FileDescription', '微信AI智能助理'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'wechat-ai-assistant'),
      StringStruct('OriginalFilename', '微信AI智能助理.exe'),
      StringStruct('ProductName', '微信AI智能助理'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""",
    encoding="utf-8",
)

# WebView2 Runtime 安装包**不进包**（客户机器缺组件时由
# ``web_window.install_webview_runtime`` 现场下载并静默安装，见该模块）。

a = Analysis(
    [str(ROOT / "src" / "wechat_ai_assistant" / "launcher.py")],
    pathex=[str(ROOT / "src")]
    + [str(p) for p in sorted((ROOT / "packages").glob("*/src"))],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="微信AI智能助理",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 任务栏、Alt-Tab、资源管理器里显示的那张脸。
    icon=str(ICON) if ICON.is_file() else None,
    # 右键属性里能看到版本号：界面打不开时靠它确认客户装的是哪一版。
    version=str(VERSION_FILE),
    # 窗口版：不分配控制台，商户双击后只看到软件窗口，没有黑框。
    # 代价是没有控制台输出，错误改由 launcher 弹 Tk 对话框（launcher._fail）。
    console=False,
    # 关窗时不要留下「已停止工作」的弹窗。
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="微信AI智能助理",
)
