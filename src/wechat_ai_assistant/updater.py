"""检查更新与静默更新：后台查新版，有新版就悄悄换掉。

## 从哪儿查（顺序固定）

1. 先在 **JYI 工具中心**读这个工具的公开详情（``/api/tools/<slug>``），从里面取
   **签名清单**（发布者用私钥签的那段原始 JSON）与签名 —— 国内拉包快，
   也没有 GitHub 那种匿名限流（60 次/时/IP）；
2. JYI 那边取不到（没配、404、清单还没签、验签不过）就退回
   **GitHub Releases** 的老路。

两条路拿到的东西都必须过同一套签名校验 —— 换个下载入口不等于放宽信任。

## 流程（全自动，不弹窗）

1. 界面起来以后在后台线程查新版，有新版就下载（启动路径上
   不联网 —— 双击就该看见窗口，不能先等一个几十兆的下载）；
2. 先验**签名清单**（公钥钉在客户端里，见
   :mod:`wechat_ai_assistant.update_signature`），再比对包哈希，
   两项都过不了直接扔掉，绝不安装；
3. 解压到版本目录并写 ``pending.json``，界面顶栏留一句「下次打开自动更新」；
4. 下次启动时起一个分离的更新脚本，等本进程退出后换目录、重启，
   旧版留备份，起不来自动回滚。

## 为什么用分离脚本替换

Windows 下运行中的 exe 和 DLL 全被锁死，原地覆盖必失败。
换目录 + 重启是唯一不拼运气的做法。更新在启动阶段完成，不会中断
已经打开并正在使用的客户端。

## 约定（发版的人必须遵守，见 docs/release.md）

* 版本号就是 Git tag 去掉开头的 ``v``，和 :mod:`wechat_ai_assistant.version` 一致；
* 安装包叫 ``wechat-ai-assistant-win64-<版本>.zip``，内容是整个 onedir 目录；
* 安装包上传到 **JYI 工具中心**（``docs/release.md`` 的「上传」一节）；老客户端只认
  GitHub 上同名加 ``.sha256`` 的十六进制哈希，所以过渡期两个都要有；
* 每个版本还要在工具中心填上**签名清单 + 签名**（``deploy/sign_release.py``
  生成的两样东西）—— 客户端就是从那儿读的；缺了就当没有新版。

这里任何一步失败都只记日志、不抛异常 —— 更新永远不能挡住启动。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode, urlsplit

from . import update_signature as signing

LOGGER = logging.getLogger(__name__)

#: 默认查这个仓库的 releases。换地方发版就改这里（或调用时传 repo）。
DEFAULT_REPO = "randomix777/wechat-ai-assistant"
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"

#: JYI 工具中心（发布口）。客户端从这儿读**签名清单**，也从这儿下包 ——
#: 国内拉 GitHub 的 70MB 又慢又被匿名限流。设成空串就只用 GitHub。
JYI_TOOLS_BASE = "https://jyisolutions.com"
#: 工具中心里这个工具的 slug（``<base>/api/tools/<slug>``）。
JYI_TOOL_SLUG = "wechat-ai-assistant"
#: 自建服务器/测试用：覆盖工具中心地址与工具名。空串 = 关掉这条路。
TOOLS_BASE_ENV = "WECHAT_AI_TOOLS_BASE"
TOOL_SLUG_ENV = "WECHAT_AI_TOOL_SLUG"

#: 安装包名前缀。完整文件名形如 ``wechat-ai-assistant-win64-1.9.9.zip``。
ASSET_PREFIX = "wechat-ai-assistant-win64-"
PENDING_NAME = "pending.json"
UPDATES_DIR_NAME = "_updates"
BACKUP_PREFIX = "_backup-"
#: 换版失败记号的文件名前缀（``blocked-<版本>.txt``）。换版脚本按它写，
#: :func:`blocked_versions` 按它读 —— 两处必须同一个。
BLOCKED_PREFIX = "blocked-"

HTTP_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 300.0


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.9.8' / '1.9.8' → (1, 9, 8)。解析不出返回空元组（当没有）。"""
    cleaned = str(text or "").strip().lstrip("vV")
    if not cleaned:
        return ()
    parts: list[int] = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits and piece:
            return ()
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else ()


def is_newer(remote: str, current: str) -> bool:
    """远端版本是否比当前新。任一解析不出都算"没有"。"""
    want, have = parse_version(remote), parse_version(current)
    return bool(want) and bool(have) and want > have


def current_version() -> str:
    """本程序的版本号。拿不到也不崩（更新跳过）。"""
    try:
        from .version import __version__
        return str(__version__ or "")
    except Exception:
        return ""


def app_dir() -> Path:
    """程序所在目录：exe 和 _internal 的上一级。

    打包后是 exe 所在目录；源码跑是仓库根（更新只在打包形态下生效，
    源码跑直接 git pull，不走这套）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def is_frozen_app() -> bool:
    """是不是打好包的 exe。源码开发模式不做自我更新。"""
    return bool(getattr(sys, "frozen", False))


def _http_get_json(url: str, headers: Mapping[str, str] | None = None) -> Any:
    request_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "wechat-ai-assistant-updater/1",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))


def _http_get_bytes(url: str, timeout: float = HTTP_TIMEOUT,
                    headers: Mapping[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": "wechat-ai-assistant-updater/1"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(1024 * 1024 * 1024 + 1)


def _find_asset(assets: Any, name: str) -> str:
    """按文件名在 release 资源里找下载地址。找不到返回空字符串。"""
    for asset in assets or []:
        if not isinstance(asset, Mapping):
            continue
        if str(asset.get("name") or "") == name:
            return str(asset.get("browser_download_url") or "")
    return ""


def _env_or(env_name: str, default: str) -> str:
    """环境变量认"显式设置"：设成空串是**关掉**这条路，不是"没设过"。"""
    raw = os.environ.get(env_name)
    if raw is None:
        raw = default
    return str(raw).strip()


def tools_base() -> str:
    """JYI 工具中心的地址（不含结尾斜杠）。空串 = 这条路关着。"""
    return _env_or(TOOLS_BASE_ENV, JYI_TOOLS_BASE).rstrip("/")


def tool_slug() -> str:
    """工具中心里这个工具的 slug。空串 = 这条路关着。"""
    return _env_or(TOOL_SLUG_ENV, JYI_TOOL_SLUG)


class _DownloadPaused(Exception):
    """工具中心明确说这一版暂停下载了。

    这不是"没查到"，所以不能转 GitHub 找同一个包 —— 暂停多半就是因为这版有问题。
    """


def _windows_asset(assets: Any) -> Mapping[str, Any] | None:
    """工具中心的资源列表里挑 Windows 的那条。挑不到返回 None。

    优先 x64，其次任何 Windows 架构 —— 挑不到宁可当没有新版，也不猜一个别的平台。
    """
    windows: list[Mapping[str, Any]] = []
    for asset in assets or []:
        if not isinstance(asset, Mapping):
            continue
        if str(asset.get("platform") or "").strip().lower() != "windows":
            continue
        windows.append(asset)
        if str(asset.get("arch") or "").strip().lower() in {"x64", "x86_64", "amd64"}:
            return asset
    return windows[0] if windows else None


def _check_jyi(current: str) -> dict[str, Any] | None:
    """读工具中心的**公开详情**，从签名清单里拿这一版该装什么。

    公开详情不带凭据（``GET /api/tools/<slug>?lang=zh``）：它给的是"这一版有什么"，
    而**判据始终是清单的签名** —— 工具中心报的版本号、哈希只当线索，清单被改过
    一律当没有新版。下载才需要授权，那枚短效 Bearer 由
    :func:`download_and_stage` 挂在工具中心的下载地址上。
    """
    base, slug = tools_base(), tool_slug()
    if not base or not slug:
        return None
    url = f"{base}/api/tools/{quote(slug, safe='')}?{urlencode({'lang': 'zh'})}"
    try:
        payload = _http_get_json(url)
    except Exception as exc:
        LOGGER.debug("JYI 工具中心没查到（%s），改问 GitHub", exc)
        return None
    tool = payload.get("tool") if isinstance(payload, Mapping) else None
    if not isinstance(tool, Mapping):
        LOGGER.debug("工具中心没给工具详情，改问 GitHub")
        return None
    # 后台把这一版的下载暂停了：**连 GitHub 都不问** —— 同一版换个地方下回来，
    # 下架就白下了（发布的人正是要它别再出去）。
    if tool.get("downloadsPaused"):
        raise _DownloadPaused(current)
    status = str(tool.get("status") or "").strip().lower()
    if status and status != "published":
        LOGGER.info("工具中心这一版状态是 %s，这次不更新", status)
        return None
    asset = _windows_asset(tool.get("assets"))
    if not isinstance(asset, Mapping):
        LOGGER.debug("工具中心没有 Windows 的包，改问 GitHub")
        return None
    manifest = asset.get("signedManifest")
    signature = asset.get("signature")
    if not manifest or not signature:
        LOGGER.debug("工具中心里这版还没带签名清单，改问 GitHub")
        return None
    if not signing.verify_manifest(str(manifest), str(signature)):
        LOGGER.warning("工具中心的签名清单没验过，改问 GitHub 要真清单")
        return None
    parsed = signing.parse_manifest(str(manifest).encode("utf-8"))
    if not parsed:
        LOGGER.warning("工具中心的签名清单读不懂，当没有新版")
        return None
    version = str(parsed.get("version") or "").lstrip("vV")
    if not is_newer(version, current):
        return None
    download_url = str(parsed.get("zip_url") or "").strip()
    if not _is_tools_center_url(download_url, base):
        LOGGER.warning("JYI 签名清单的下载地址不属于工具中心，拒绝使用授权凭据")
        return None
    return {
        "version": version,
        # 清单里没写更新说明就用工具中心页面上那句 —— 两边都空就没有说明。
        "notes": str(parsed.get("notes") or "") or str(tool.get("releaseNotes") or ""),
        "zip_url": download_url,
        "sha256": str(parsed.get("sha256") or ""),
        # 清单里说的地址万一挂了，GitHub 上同一版还能兜一下。
        "alt_urls": [_github_asset_url(version)],
    }


def _is_tools_center_url(url: str, base: str | None = None) -> bool:
    """Only attach the short-lived Tool Center bearer to the expected HTTPS origin."""
    try:
        candidate = urlsplit(url)
        expected = urlsplit(base or tools_base())
        return (candidate.scheme == "https" and not candidate.username and not candidate.password
                and expected.scheme == "https" and candidate.hostname == expected.hostname
                and candidate.port == expected.port and candidate.path.startswith("/api/tools/"))
    except ValueError:
        return False


def _github_asset_url(version: str) -> str:
    """按版本号拼出 GitHub 上的包地址（不用问 API，规则是固定的）。"""
    from urllib.parse import quote
    name = f"{ASSET_PREFIX}{str(version).lstrip('vV')}.zip"
    return f"https://github.com/{DEFAULT_REPO}/releases/download/v{str(version).lstrip('vV')}/{quote(name, safe='')}"


def _check_github(repo: str, current: str) -> dict[str, Any] | None:
    """老路：GitHub 的 ``releases/latest``。"""
    try:
        release = _http_get_json(API_LATEST.format(repo=repo))
    except Exception as exc:
        LOGGER.debug("检查更新失败：%s", exc)
        return None
    if not isinstance(release, dict):
        return None
    version = str(release.get("tag_name") or release.get("name") or "")
    if not is_newer(version, current):
        return None
    return _release_info(release, version, current)


def check_for_update(repo: str = DEFAULT_REPO, current: str = "") -> dict[str, Any] | None:
    """查最新版。有新版返回 ``{version, notes, zip_url, sha256, alt_urls}``，
    没有/查不到/任何异常都返回 None（调用方直接跳过）。

    先问 JYI 工具中心，再问 GitHub。**检查不需要凭据**（读的是公开详情），
    只有**下载**那一枚短效 Bearer 要用在工具中心的下载地址上 ——
    见 :func:`download_and_stage`。两条路的清单都**只认签名**：清单里写着这一版
    该装哪个包、哈希是多少，包地址只是个下载入口。清单缺失或验不过，钉了公钥
    的客户端一律当作"没有新版" —— 宁可停在旧版，也不装一个来路不明的包。
    """
    current = current or current_version()
    if not parse_version(current):
        return None
    try:
        info = _check_jyi(current)
    except _DownloadPaused as paused:
        LOGGER.info("工具中心把 %s 的下载暂停了，这次不更新", paused)
        return None
    if info is None:
        info = _check_github(repo, current)
    if info is None:
        return None
    version = str(info.get("version") or "").lstrip("vV")
    if version_is_blocked(version):
        LOGGER.info("版本 %s 之前换上去没起来，这次跳过（不重下）", version)
        return None
    return info


def blocked_marker(version: str, base: Path | None = None) -> Path:
    """换版失败留下的记号文件路径（`blocked-<版本>.txt`）。"""
    return (base or app_dir()) / f"blocked-{str(version).lstrip('vV')}.txt"


def blocked_versions(base: Path | None = None) -> list[str]:
    """被跳过的版本号（安装目录里的 ``blocked-<版本>.txt``），按版本从小到大。"""
    directory = base or app_dir()
    found: list[str] = []
    try:
        for path in directory.glob(f"{BLOCKED_PREFIX}*.txt"):
            version = path.stem[len(BLOCKED_PREFIX):].strip()
            if version:
                found.append(version)
    except OSError:
        return []
    return sorted(found, key=parse_version)


def failed_update_note(base: Path | None = None) -> str:
    """上次换版没成时给商户看的一句话；没这回事就返回空串。

    换版失败以前只写 ``%TEMP%`` 的运行日志和安装目录里的记号，界面上一个字都没有
    —— 商户看到的是"怎么一直没更新"，客服也问不出所以然。
    """
    versions = blocked_versions(base)
    if not versions:
        return ""
    return (f"上次更新到 v{versions[-1]} 没装成，已跳过这一版；"
            "下一版出来会自动再试")


def version_is_blocked(version: str, base: Path | None = None) -> bool:
    """这一版换上去过、但没起来：别再让商户一遍遍下它。

    记号由换版脚本在回滚时留下（见 ``build_updater_script``），文件名跟这里要一致。
    """
    try:
        return blocked_marker(version, base).is_file()
    except Exception:
        return False


def _release_info(release: Mapping[str, Any], version: str, current: str) -> dict[str, Any] | None:
    """从 release 里取这次更新该装什么：优先签名清单，退回老路。"""
    assets = release.get("assets") or []
    manifest_url = _find_asset(assets, signing.MANIFEST_NAME)
    if manifest_url:
        info, rejected = _signed_release_info(release, assets, manifest_url, version, current)
        if info is not None:
            return info
        if rejected or signing.is_pinned():
            return None
        LOGGER.warning("更新清单没验过，退回按哈希校验（客户端还没钉公钥）")
    elif signing.is_pinned():
        LOGGER.warning("release %s 里没有签名清单（%s），拒绝更新",
                       version, signing.MANIFEST_NAME)
        return None
    return _legacy_release_info(assets, version)


def _signed_release_info(release: Mapping[str, Any], assets: Any, manifest_url: str,
                         version: str, current: str) -> tuple[dict[str, Any] | None, bool]:
    """下载清单与签名、验签、核对内容。返回 ``(信息, 是否已否决)``。"""
    signature_url = _find_asset(assets, signing.SIGNATURE_NAME) or manifest_url + ".sig"
    try:
        manifest = _http_get_bytes(manifest_url)
        signature = _http_get_bytes(signature_url)
    except Exception as exc:
        LOGGER.warning("下载更新清单或签名失败：%s", exc)
        return None, True
    if not signing.verify_manifest(manifest, signature):
        LOGGER.warning("更新清单签名校验失败，拒绝更新（可能是有人在改包）")
        return None, True
    parsed = signing.parse_manifest(manifest)
    if parsed is None:
        LOGGER.warning("更新清单内容不合法，拒绝更新")
        return None, True
    want = version.lstrip("vV")
    if parsed["version"] != want:
        LOGGER.warning("清单版本（%s）和 release 标签（%s）对不上，拒绝更新",
                       parsed["version"], want)
        return None, True
    if not is_newer(parsed["version"], current):
        return None, False
    alt_urls = [url for url in (_find_asset(assets, f"{ASSET_PREFIX}{parsed['version']}.zip"),) if url]
    return {
        "version": parsed["version"],
        "notes": parsed["notes"] or str(release.get("body") or ""),
        "zip_url": parsed["zip_url"],
        "sha256": parsed["sha256"],
        "alt_urls": alt_urls,
    }, False


def _legacy_release_info(assets: Any, version: str) -> dict[str, Any] | None:
    """老路：release 里的 zip + 同名 .sha256。只该给还没钉公钥的部署用。"""
    zip_url, sha_url = "", ""
    want_zip = f"{ASSET_PREFIX}{version.lstrip('vV')}.zip"
    for asset in assets or []:
        if not isinstance(asset, Mapping):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name == want_zip:
            zip_url = url
        elif name == want_zip + ".sha256":
            sha_url = url
    # 文件名对不上就退而求其次：第一个 win64 zip + 同名 sha256。
    if not zip_url:
        for asset in assets or []:
            if not isinstance(asset, Mapping):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if name.endswith(".zip") and "win64" in name.lower():
                zip_url = url
                sha_url = url + ".sha256"
                break
    if not zip_url:
        LOGGER.debug("release %s 里没有安装包", version)
        return None
    return {"version": version.lstrip("vV"), "notes": "",
            "zip_url": zip_url, "sha256": "", "alt_urls": [], "sha_url": sha_url}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scratch(name: str) -> Path:
    """中间文件目录：跟着数据目录走（见 ``paths.scratch_dir``）。"""
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


def download_and_stage(info: Mapping[str, Any], base: Path | None = None,
                       on_progress: Callable[[int, int], None] | None = None,
                       tool_access_token: str = "") -> Path | None:
    """下载 → 校验 → 解压到版本目录。成功返回版本目录，失败返回 None。

    校验不过/解压失败一律删干净，不留半截文件让下次启动误用。
    """
    base = base or app_dir()
    version = str(info.get("version") or "")
    urls = [str(info.get("zip_url") or "")]
    urls.extend(str(url) for url in (info.get("alt_urls") or []))
    urls = [url for url in urls if url]
    if not version or not urls or not parse_version(version):
        return None
    staged = base / UPDATES_DIR_NAME / version
    # 中间文件放在**数据目录**下的 tmp（免安装版就在 data\tmp，不往 C 盘撒）。
    # 用 mkdtemp 在它下面再开一个唯一目录：两次更新同时跑时不打架。
    tmp_dir = Path(tempfile.mkdtemp(prefix="dl-", dir=str(_scratch("updater"))))
    try:
        zip_path = tmp_dir / "app.zip"
        data = b""
        for url in urls:
            try:
                # 只有**工具中心的下载地址**才挂那枚短效 Bearer（授权审批）；别的
                # 地址（GitHub 兜底）不带任何凭据。没有凭据时连参数都不传。
                if tool_access_token and _is_tools_center_url(url):
                    data = _http_get_bytes(
                        url, DOWNLOAD_TIMEOUT,
                        headers={"Authorization": f"Bearer {tool_access_token}"})
                else:
                    data = _http_get_bytes(url, DOWNLOAD_TIMEOUT)
                break
            except Exception as exc:
                LOGGER.debug("下载更新失败（%s）：%s", url, exc)
        if not data:
            LOGGER.warning("更新包哪个地址都没下下来，跳过这次更新")
            return None
        zip_path.write_bytes(data)
        if on_progress is not None:
            try:
                on_progress(len(data), len(data))
            except Exception:
                pass
        # 校验：优先用签名清单里写的哈希（清单本身已经验过签名），其次才去
        # 下载 .sha256。两个都没有就拒绝安装 —— 没有哈希的包不装，省这一步
        # 等于把商户电脑交给任意下载。
        expected = str(info.get("sha256") or "").strip().lower()
        if not expected:
            sha_url = str(info.get("sha_url") or "")
            if sha_url:
                try:
                    expected = _http_get_bytes(sha_url).decode("utf-8", errors="replace").split()[0].strip().lower()
                except Exception as exc:
                    LOGGER.debug("下载校验文件失败：%s", exc)
        if not expected or _sha256_file(zip_path) != expected:
            LOGGER.warning("更新包校验失败，已丢弃")
            return None
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(staged)
        except Exception as exc:
            LOGGER.debug("更新包解压失败：%s", exc)
            shutil.rmtree(staged, ignore_errors=True)
            return None
        _flatten_single_top_dir(staged)
        if not _looks_like_app(staged):
            LOGGER.warning("更新包内容不对（找不到主程序），已丢弃")
            shutil.rmtree(staged, ignore_errors=True)
            return None
        pending = {"version": version, "dir": staged.name,
                   "notes": str(info.get("notes") or "")}
        (base / PENDING_NAME).write_text(
            json.dumps(pending, ensure_ascii=False), encoding="utf-8")
        return staged
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _flatten_single_top_dir(staged: Path) -> None:
    """压缩包里多套了一层目录就扒掉（只扒一层，里面还套就不管了）。"""
    try:
        children = [p for p in staged.iterdir()]
    except OSError:
        return
    if len(children) == 1 and children[0].is_dir():
        inner = children[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(staged / item.name))
        try:
            inner.rmdir()
        except OSError:
            pass


def _looks_like_app(directory: Path) -> bool:
    """解压出来像不像我们的程序：有 exe + _internal。"""
    try:
        names = {p.name for p in directory.iterdir()}
    except OSError:
        return False
    has_exe = any(name.lower().endswith(".exe") for name in names)
    return has_exe and "_internal" in names


def pending_update(base: Path | None = None) -> dict[str, Any] | None:
    """读 pending.json。没有/坏了/目录不在了都返回 None。"""
    base = base or app_dir()
    path = base / PENDING_NAME
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(info, dict) or not info.get("version"):
        return None
    staged = base / UPDATES_DIR_NAME / str(info.get("dir") or "")
    if not staged.is_dir() or not _looks_like_app(staged):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    info["staged_dir"] = str(staged)
    return info


def build_updater_script(base: Path, info: Mapping[str, Any], exe_name: str,
                         self_pid: int, settle_seconds: int = 15) -> Path:
    """生成更新脚本并返回路径。脚本自己住在系统临时目录（搬目录时不受影响）。

    流程：等本进程退出 → 把新版目录挪到外面 → 旧目录整个改名备份 →
    新版目录搬进来 → 起新版（等 `settle_seconds` 秒还活着就算成，删备份）→
    起不来就删了换回旧版再起。最后自删。

    用 .bat 而不用再打一个 exe：少一个构建产物，逻辑全摆在明面上。
    `settle_seconds` 只给测试调小用（真机上 15 秒是给慢机器留的余量）。
    """
    staged = base / UPDATES_DIR_NAME / str(info.get("dir"))
    backup = base.parent / (base.name + BACKUP_PREFIX + str(info.get("version")))
    sibling = base.parent / (base.name + f".staged-{info.get('version')}")
    portable_data = base / "data"
    script = Path(tempfile.gettempdir()) / f"pyw-apply-{info.get('version')}.bat"
    version = str(info.get("version"))
    # 换版一旦落定（不管成没成），都必须把 pending.json 收掉：脚本在收尾时会
    # **把软件重新启动一次**，那一次启动又会读到 pending 再生成一个脚本 —— 演练里
    # 实测会变成"启动→换版失败→再启动"的死循环（日志里连着刷了二十次）。
    # 备份目录里那份也要删：换到一半时旧目录已经被改名成备份，pending 跟着它走。
    forget_pending = [
        f'del "{base}\\{PENDING_NAME}" 2>nul',
        f'del "{backup}\\{PENDING_NAME}" 2>nul',
    ]
    # 新版换上去又没起来的证据：在**两个**目录里都留一份记号（回滚时活下来的是备份
    # 那个目录，它随后会被改名回 base），后台查更新看到记号就不再下这一版 —— 否则
    # 每启动一次都要重下 70MB、再失败一次。
    mark_blocked = [
        f'>>"{blocked_marker(version, base)}" echo 这一版换上去没起来，先跳过',
        f'>>"{blocked_marker(version, backup)}" echo 这一版换上去没起来，先跳过',
    ]

    def sleep(seconds: int) -> str:
        """等一会儿。用 ping 不用 timeout.exe：timeout 在标准输入被重定向时直接退出
        （"Input redirection is not supported, exiting the process immediately."），
        而脚本是 GUI 进程用 CREATE_NO_WINDOW 拉起来的，根本没有控制台给它 —— 真机上
        那两处等待会一起消失。回环的第一个回包立刻就到，所以 -n N 大约等 N-1 秒。"""
        return f'"%SYS%\\ping.exe" -n {seconds + 1} 127.0.0.1 >nul'

    log = Path(tempfile.gettempdir()) / f"pyw-apply-{info.get('version')}.log"

    def note(text: str) -> str:
        """往日志里记一行（`%TEMP%\\pyw-apply-<版本>.log`），商户机器上出问题能查。

        只插在"上一条 errorlevel 已经被 if 消化掉"的位置；失败分支的日志放分支第一行
        （那里后面只剩 `if exist` 判断，不会再读 errorlevel）。文案里别用括号、管道、
        重定向符号 —— 它们会改掉命令行的意思。
        """
        return f'>>"{log}" echo [%date% %time%] {text}'

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        # 外部命令一律走绝对路径：开发机上 PATH 里的 find/timeout 可能是 MSYS 的
        # 同名工具（find 会拿着参数跑去全盘搜文件名，timeout 的语法也不一样）。
        'set "SYS=%SystemRoot%\\System32"',
        "rem 换版过程写进日志：商户把 %TEMP% 下的 pyw-apply-*.log 发回来就能查",
        note(f"开始换版：{info.get('version')}，等旧进程 {self_pid} 退出"),
        f"rem 等待旧进程 {self_pid} 退出",
        ":wait",
        f'"%SYS%\\tasklist.exe" /FI "PID eq {self_pid}" /NH 2>nul'
        f' | "%SYS%\\find.exe" "{self_pid}" >nul',
        f'if %errorlevel%==0 ({sleep(1)} & goto wait)',
        note("旧进程已退出，开始搬目录"),
        f'if exist "{sibling}" rmdir /s /q "{sibling}"',
        f'move "{staged}" "{sibling}"',
        "if errorlevel 1 goto stage_failed",
        # Never discard an existing backup: an interrupted prior update may
        # still have the only preserved copy of a portable user's data.
        f'if exist "{backup}" goto restore_stage',
        # 旧目录偶尔还被人占着（杀软刚扫过、上一个进程的句柄还没彻底放）。试三次再
        # 认输：一次偶发占用不该让商户这次更新白等一个版本周期。
        "set /a swap_try=0",
        ":try_swap",
        f'move "{base}" "{backup}" 2>nul',
        "if not errorlevel 1 goto swapped",
        "set /a swap_try+=1",
        "if %swap_try% GEQ 3 goto restore_stage",
        note("旧目录还被占着，等两秒再试一次"),
        sleep(2),
        "goto try_swap",
        ":swapped",
        f'move "{sibling}" "{base}"',
        "if errorlevel 1 goto rollback",
        # Portable data from the old version joins the new app. 新版包里自带一个
        # 空的 data 目录，所以这里不能用 move：move 到已存在的目录会在它里面再嵌一层
        # （data\data\…），用户就会以为自己的数据没了。xcopy 倒的是源目录的内容；
        # 它的 errorlevel 1 只是"没东西可拷"，2 及以上才算真失败。
        f'if exist "{backup}\\data" xcopy /e /i /q /y /h "{backup}\\data" "{portable_data}" >nul',
        "if errorlevel 2 goto rollback",
        f'if exist "{backup}\\data" rmdir /s /q "{backup}\\data"',
        note("新版已就位，便携数据已并过去"),
        f'if not exist "{base}\\{exe_name}" goto rollback',
        f'start "" "{base}\\{exe_name}"',
        note(f"起了新版，等 {settle_seconds} 秒后看它活没活"),
        sleep(settle_seconds),
        # "新版起来了没有"：拿它自己的映像名问 tasklist —— 老版进程这时候已经退了（脚本
        # 就是它起的），所以同名进程还在，就是新版起来了。
        # 不用「能不能往主程序里写一个字节」判断：实测那份映像被搬过目录之后追加写会
        # 成功（它不再算"运行中的映像"），于是新版明明活着却被判成没起来，脚本会走进
        # 回滚分支、还想 rmdir 正在运行的新目录，最后把备份嵌进新版里面。
        # tasklist 的 IMAGENAME 过滤在 chcp 65001 下对中文名是对得上的；find 走绝对
        # 路径，避开开发机 PATH 里那个 MSYS 的同名工具。
        f'"%SYS%\\tasklist.exe" /FI "IMAGENAME eq {exe_name}" /NH 2>nul'
        f' | "%SYS%\\find.exe" "{exe_name}" >nul',
        "if not errorlevel 1 goto success",
        note("新版没起来：回滚旧版"),
        ":: 新版没起来，回滚旧版",
        *forget_pending,
        *mark_blocked,
        f'if exist "{portable_data}" if not exist "{backup}\\data" move "{portable_data}" "{backup}\\data"',
        f'rmdir /s /q "{base}"',
        f'move "{backup}" "{base}"',
        f'start "" "{base}\\{exe_name}"',
        "goto finish",
        ":rollback",
        note("换版中途失败：回滚旧版"),
        *forget_pending,
        f'if exist "{portable_data}" if not exist "{backup}\\data" move "{portable_data}" "{backup}\\data"',
        f'if exist "{base}" rmdir /s /q "{base}"',
        # 新版目录删不掉（还有进程占着、杀软锁着）时绝不能往下搬：move 到已存在的目录
        # 会把整个备份塞进新版里面，用户就同时丢了"能跑的旧版"和"能回滚的备份"。
        # 这时候退而求其次：直接起备份里的旧版，剩下的留给下次启动处理。
        f'if exist "{base}" goto start_backup',
        f'if exist "{backup}" move "{backup}" "{base}"',
        f'if exist "{base}\\{exe_name}" start "" "{base}\\{exe_name}"',
        "goto finish",
        ":start_backup",
        note("新版目录删不掉：先把备份里的旧版起起来"),
        *forget_pending,
        f'if exist "{backup}\\{exe_name}" start "" "{backup}\\{exe_name}"',
        "goto finish",
        ":stage_failed",
        note("换版失败：暂存目录搬不动，可能被杀软占着"),
        *forget_pending,
        "goto start_old",
        ":restore_stage",
        note("换版失败：旧目录还被占着，这次不换了，下次启动再说"),
        *forget_pending,
        f'if exist "{sibling}" move "{sibling}" "{staged}"',
        ":start_old",
        f'if exist "{base}\\{exe_name}" start "" "{base}\\{exe_name}"',
        "goto finish",
        ":success",
        note("新版活了：删掉旧版备份，换版完成"),
        *forget_pending,
        f'if exist "{backup}" rmdir /s /q "{backup}"',
        ":finish",
        note("脚本收尾"),
        'del "%~f0"',
    ]
    # newline="" 别让 Python 再做一次换行翻译：否则显式的 "\r\n" 会变成 "\r\r\n"，
    # cmd 会把每两行之间那条空的回车当成一个空命令行（能跑，但日志和排错都难看）。
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    return script


def apply_pending_update(base: Path | None = None) -> bool:
    """启动时调用：有 pending 就起更新脚本并返回 True（调用方随后退出）。

    只在打包形态下干活；返回 False 表示没什么可做的，正常启动。
    """
    if not is_frozen_app():
        return False
    base = base or app_dir()
    info = pending_update(base)
    if info is None:
        return False
    try:
        exe = next(p for p in base.iterdir()
                   if p.suffix.lower() == ".exe" and p.is_file())
    except (OSError, StopIteration):
        return False
    try:
        script = build_updater_script(base, info, exe.name, os.getpid())
        # cwd 千万别设成 base：脚本第一步就是把 base 整个改名成"备份"，而 Windows
        # 不允许重命名"某个进程的当前目录"—— 真机上就表现成"旧目录还被占着"，
        # 于是每次启动都换不成（演练里实测：日志里连着二十次 swap 失败）。
        # 脚本里全是绝对路径，从 %TEMP% 跑完全没问题。
        subprocess.Popen(
            ["cmd", "/c", str(script)], cwd=str(script.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True)
    except Exception as exc:
        LOGGER.debug("启动更新脚本失败：%s", exc)
        return False
    return True


def check_in_background(on_staged: Callable[[Mapping[str, Any]], None] | None = None,
                        repo: str = DEFAULT_REPO, jyi_client: Any = None,
                        on_failed: Callable[[Mapping[str, Any], str], None] | None = None,
                        ) -> threading.Thread:
    """起后台线程查更新、下包、暂存。任何异常都吞掉，只记日志。

    只在打包形态下跑；开发模式调了也是空转（apply 那边同样只认打包形态）。

    ``on_failed(info, hint)``：**查到新版但没下下来**时回调。以前这种情况只有
    一行日志，界面一声不吭 —— 商户看到的是"怎么一直不更新"，客服也问不出所以然。
    ``hint`` 是一句能采取行动的话（最常见的是"还没登录，登录后才能从工具中心下载"）。
    """

    def work() -> None:
        try:
            if not is_frozen_app():
                return
            # 必须带上本机版本：不传就是拿空串比，任何 release 都"更新"，界面一启动
            # 就去下自己已经在跑的这版，白耗流量还多一次换版。
            # 凭据只留给**下载**：查新版读的是公开详情，不带任何凭据。
            info = check_for_update(repo, current=current_version())
            if info is None:
                return
            tool_access_token = ""
            token_error = ""
            get_tool_access = getattr(jyi_client, "tool_access_token", None)
            if callable(get_tool_access):
                try:
                    tool_access_token = str(get_tool_access() or "")
                except Exception as exc:
                    token_error = str(exc)
                    LOGGER.debug("获取 JYI 工具中心下载凭据失败：%s", exc)
            staged = download_and_stage(info, tool_access_token=tool_access_token)
            if staged is None:
                # 查到了新版却没下成：说清楚"卡在哪一步"，别让商户对着旧界面猜。
                hint = ("还没登录产品账号，登录后才能从工具中心下载"
                        if not tool_access_token
                        else "下载或校验没通过，稍后会自动再试")
                LOGGER.warning("新版本 %s 没下下来：%s（凭据错误：%s）",
                               info.get("version"), hint, token_error or "无")
                if on_failed is not None:
                    try:
                        on_failed(info, hint)
                    except Exception:
                        pass
                return
            LOGGER.info("新版本 %s 已暂存，下次启动生效", info.get("version"))
            if on_staged is not None:
                try:
                    on_staged(info)
                except Exception:
                    pass
        except Exception as exc:
            LOGGER.debug("后台更新失败：%s", exc)

    thread = threading.Thread(target=work, name="app-update", daemon=True)
    thread.start()
    return thread


__all__ = [
    "API_LATEST",
    "ASSET_PREFIX",
    "BLOCKED_PREFIX",
    "DEFAULT_REPO",
    "UPDATES_DIR_NAME",
    "apply_pending_update",
    "app_dir",
    "blocked_versions",
    "build_updater_script",
    "check_for_update",
    "check_in_background",
    "current_version",
    "download_and_stage",
    "failed_update_note",
    "is_frozen_app",
    "is_newer",
    "parse_version",
    "pending_update",
]
