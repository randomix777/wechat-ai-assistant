"""Product paths, including first-run compatibility with the old data folder."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

APP_DIRECTORY = "wechat-ai-assistant"
LEGACY_APP_DIRECTORY = "pyweixin"
PORTABLE_MARKER = "portable.flag"
_PORTABLE_MIGRATION_LOCK = threading.Lock()


def _windows_data_paths() -> tuple[Path, Path]:
    base_value = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    base = Path(base_value) if base_value else Path.home()
    if not base_value:
        return base / ".wechat-ai-assistant", base / ".pyweixin"
    return base / APP_DIRECTORY, base / LEGACY_APP_DIRECTORY


def _portable_data_dir() -> Path | None:
    """Return the data folder beside a packaged app marked as portable."""
    if not getattr(sys, "frozen", False):
        return None
    app_dir = Path(sys.executable).resolve().parent
    if not (app_dir / PORTABLE_MARKER).is_file():
        return None
    return app_dir / "data"


def _has_entries(path: Path) -> bool:
    try:
        return path.is_dir() and next(path.iterdir(), None) is not None
    except OSError:
        return False


def _migrate_existing_data(portable_dir: Path, candidates: tuple[Path, Path]) -> None:
    """Copy existing per-user data once; leave the source folder untouched."""
    with _PORTABLE_MIGRATION_LOCK:
        staging: Path | None = None
        source: Path | None = None
        try:
            portable_dir.mkdir(parents=True, exist_ok=True)
            if _has_entries(portable_dir):
                return
            source = next((path for path in candidates if _has_entries(path)), None)
            if source is None or source.resolve() == portable_dir.resolve():
                return

            staging = Path(tempfile.mkdtemp(prefix=".wechat-ai-data-", dir=portable_dir.parent))
            shutil.copytree(source, staging, dirs_exist_ok=True)
            if portable_dir.exists():
                if _has_entries(portable_dir):
                    return
                portable_dir.rmdir()
            staging.replace(portable_dir)
        except OSError as exc:
            raise RuntimeError(
                f"无法准备便携版数据目录：{source or '无旧数据'} → {portable_dir}。"
                "请确认程序文件夹可写后重试。"
            ) from exc
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def default_data_dir() -> Path:
    """Return portable data beside a marked app or the per-user data directory.

    ``WECHAT_AI_CLIENT_DATA_DIR`` takes precedence. The old
    ``PYWEIXIN_CLIENT_DATA_DIR`` remains supported for existing launch scripts.
    A portable build copies existing per-user data once and leaves the source intact.
    """
    override = (os.environ.get("WECHAT_AI_CLIENT_DATA_DIR")
                or os.environ.get("PYWEIXIN_CLIENT_DATA_DIR"))
    if override:
        return Path(override).expanduser()

    portable_dir = _portable_data_dir()
    if portable_dir is not None:
        _migrate_existing_data(portable_dir, _windows_data_paths())
        return portable_dir

    new_path, old_path = _windows_data_paths()
    if new_path.exists() or not old_path.exists():
        return new_path
    return old_path


def scratch_dir(name: str = "") -> Path:
    """临时/中间文件放哪：**跟着数据目录走**。

    免安装版的承诺是"整个软件就在这个文件夹里"：数据、日志、缓存、下载的组件都该
    在它旁边，而不是往 C 盘的 ``%TEMP%`` 撒。以前 Edge 的浏览器配置、WebView2 安装包、
    下载中的更新包、诊断报告全写在 ``%TEMP%`` —— 商户换台机器或清理系统临时目录，
    这些东西就散落在别人看不见的地方。现在统一放到 ``<数据目录>/tmp``：

    * 免安装（有 ``portable.flag``）→ ``<程序目录>/data/tmp``，跟着文件夹走；
    * 普通安装 → ``%LOCALAPPDATA%\\wechat-ai-assistant\\tmp``。

    **例外（必须留在系统临时目录）**：换版脚本 ``pyw-apply-*.bat`` —— 它第一步就是
    把整个程序目录改名，脚本自己绝不能待在那个目录里。见 ``updater.build_updater_script``。

    建不出来（只读介质、权限不足）就退回系统临时目录：中间文件写不进去不能拦住启动。
    """
    try:
        base = default_data_dir() / "tmp"
        target = base / name if name else base
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception:
        fallback = Path(tempfile.gettempdir()) / "pyw" / name if name else Path(tempfile.gettempdir()) / "pyw"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


def asset_path(name: str) -> Path | None:
    """Locate a bundled static asset (currently the window icon).

    Packaged builds put it under ``sys._MEIPASS``/``assets`` (PyInstaller
    extracts ``datas`` there); a source checkout keeps it in ``deploy/assets``.
    Returns ``None`` when neither exists: a missing icon must never stop a
    merchant's app from starting.
    """
    candidates: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "assets" / name)
    candidates.append(Path(__file__).resolve().parents[2] / "deploy" / "assets" / name)
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None
