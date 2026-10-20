"""自动更新的回归测试：不联网（HTTP 全 mock），不碰真程序目录。

签名的真签真验在 ``tests/test_update_signature.py``；这里只管"签名清单怎么被
用在更新判断上"，以及换版脚本本身。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import zipfile
from pathlib import Path
from urllib.parse import unquote

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from wechat_ai_assistant import update_signature as sig  # noqa: E402
from wechat_ai_assistant import updater  # noqa: E402

needs_cng = unittest.skipUnless(sig.available(), "需要 Windows 自带的加密组件（bcrypt.dll）")


def _release(version="1.9.9", assets=True):
    body = {"tag_name": "v" + version, "body": "修复若干问题",
            "assets": []}
    if assets:
        name = f"{updater.ASSET_PREFIX}{version}.zip"
        body["assets"] = [
            {"name": name,
             "browser_download_url": "https://example.com/" + name},
            {"name": name + ".sha256",
             "browser_download_url": "https://example.com/" + name + ".sha256"},
        ]
    return body


def _with_manifest(body, *, signature=True):
    """给 release 加上清单（和签名）资源，返回清单的下载地址。"""
    body["assets"].append({"name": sig.MANIFEST_NAME,
                           "browser_download_url": "https://example.com/" + sig.MANIFEST_NAME})
    if signature:
        body["assets"].append({"name": sig.SIGNATURE_NAME,
                               "browser_download_url": "https://example.com/" + sig.SIGNATURE_NAME})
    return "https://example.com/" + sig.MANIFEST_NAME


def _fake_app_zip(top_dir=True):
    """造一个像 onedir 解压物的 zip。"""
    buf = io.BytesIO()
    prefix = "appdir/" if top_dir else ""
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(prefix + "微信AI智能助理.exe", b"EXE")
        archive.writestr(prefix + "_internal/a.pyd", b"BIN")
    return buf.getvalue()


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v1.9.8"), (1, 9, 8))
        self.assertEqual(updater.parse_version("1.10.0"), (1, 10, 0))
        self.assertEqual(updater.parse_version("xxx"), ())

    def test_is_newer(self):
        self.assertTrue(updater.is_newer("1.9.9", "1.9.8"))
        self.assertTrue(updater.is_newer("v1.10.0", "1.9.8"))
        self.assertFalse(updater.is_newer("1.9.8", "1.9.8"))
        self.assertFalse(updater.is_newer("1.9.7", "1.9.8"))
        self.assertFalse(updater.is_newer("xxx", "1.9.8"))
        self.assertFalse(updater.is_newer("1.9.9", ""))


class CheckTests(unittest.TestCase):
    """还没签名发版的部署（公钥是空的）：退回"同名 zip + 同名 .sha256"老路。

    真程序里公钥已经钉上了，所以这里必须把钉子拔掉才测得到老路；钉上以后的行
    为在 :class:`SignedReleaseTests` 里测。
    """

    def setUp(self):
        pin = unittest.mock.patch.object(sig, "PUBLISHER_PUBKEY", "")
        pin.start()
        self.addCleanup(pin.stop)
        self._github_only()

    def _github_only(self):
        """关掉 JYI 那条路，这一组只测 GitHub 老路。"""
        off = unittest.mock.patch.dict(os.environ, {updater.TOOLS_BASE_ENV: ""})
        off.start()
        self.addCleanup(off.stop)

    def _check(self, release, current="1.9.8"):
        with unittest.mock.patch.object(
                updater, "_http_get_json", return_value=release):
            return updater.check_for_update("some/repo", current)

    def test_newer_with_assets(self):
        info = self._check(_release("1.9.9"))
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["version"], "1.9.9")
        self.assertTrue(info["zip_url"].endswith(".zip"))

    def test_same_version_is_none(self):
        self.assertIsNone(self._check(_release("1.9.8"), "1.9.8"))

    def test_network_error_is_none(self):
        with unittest.mock.patch.object(
                updater, "_http_get_json", side_effect=OSError("down")):
            self.assertIsNone(updater.check_for_update("some/repo", "1.9.8"))

    def test_no_zip_asset_is_none(self):
        self.assertIsNone(self._check(_release("1.9.9", assets=False)))

    def test_a_version_that_already_failed_is_skipped(self):
        """换上去没起来的版本留了记号：别再让商户重下 70MB、再失败一次。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            updater.blocked_marker("1.9.9", base).write_text("x", encoding="utf-8")
            with unittest.mock.patch.object(updater, "app_dir", return_value=base):
                self.assertIsNone(self._check(_release("1.9.9")))
                self.assertTrue(updater.version_is_blocked("v1.9.9", base))


@needs_cng
class SignedReleaseTests(unittest.TestCase):
    """钉了公钥以后：只认签过的清单。清单缺失、验不过、版本对不上都不装。"""

    def setUp(self):
        self.public, self.private = sig.generate_keypair()
        pin = unittest.mock.patch.object(sig, "PUBLISHER_PUBKEY", self.public)
        pin.start()
        self.addCleanup(pin.stop)
        # 这一组测的是"从 GitHub 拿到清单"这条路，JYI 那条在 JyiReleaseTests 里。
        off = unittest.mock.patch.dict(os.environ, {updater.TOOLS_BASE_ENV: ""})
        off.start()
        self.addCleanup(off.stop)

    def _manifest(self, version="1.10.0", zip_url="https://cdn.example.com/app.zip",
                  sha256="ab" * 32, notes="签名版说明"):
        raw = sig.canonical_manifest(version=version, zip_url=zip_url,
                                     sha256=sha256, notes=notes)
        return raw, sig.signature_text(sig.sign_bytes(raw, self.private)).encode() + b"\n"

    def _check(self, release, blobs, current="1.9.9"):
        with unittest.mock.patch.object(updater, "_http_get_json", return_value=release), \
            unittest.mock.patch.object(updater, "_http_get_bytes",
                                       side_effect=lambda url, *a: blobs[url]):
            return updater.check_for_update("some/repo", current)

    def _signed_release(self, **over):
        raw, signature = self._manifest(**over)
        release = _release("1.10.0")
        manifest_url = _with_manifest(release)
        blobs = {manifest_url: raw,
                 "https://example.com/" + sig.SIGNATURE_NAME: signature}
        return release, blobs

    def test_the_manifest_decides_what_gets_installed(self):
        release, blobs = self._signed_release()
        info = self._check(release, blobs)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["version"], "1.10.0")
        # 包地址和哈希来自清单，不是来自 release —— 这才叫"签名说了算"
        self.assertEqual(info["zip_url"], "https://cdn.example.com/app.zip")
        self.assertEqual(info["sha256"], "ab" * 32)
        self.assertEqual(info["notes"], "签名版说明")
        # release 里那个官方附件只是备用下载入口
        self.assertEqual(len(info["alt_urls"]), 1)
        self.assertTrue(info["alt_urls"][0].endswith("微信AI智能助理-win64-1.10.0.zip"))

    def test_an_empty_manifest_notes_falls_back_to_the_release_body(self):
        release, blobs = self._signed_release(notes="")
        info = self._check(release, blobs)
        assert info is not None
        self.assertEqual(info["notes"], "修复若干问题")

    def test_a_release_without_a_manifest_is_refused(self):
        """钉了公钥之后，光有 zip + sha256 的 release 一律不装。"""
        self.assertIsNone(self._check(_release("1.10.0"), {}))

    def test_a_forged_manifest_is_refused(self):
        raw, _ = self._manifest()
        _, other = sig.generate_keypair()
        forged = sig.signature_text(sig.sign_bytes(raw, other)).encode()
        release = _release("1.10.0")
        manifest_url = _with_manifest(release)
        blobs = {manifest_url: raw,
                 "https://example.com/" + sig.SIGNATURE_NAME: forged}
        self.assertIsNone(self._check(release, blobs))

    def test_a_tampered_manifest_is_refused(self):
        release, blobs = self._signed_release()
        manifest_url = "https://example.com/" + sig.MANIFEST_NAME
        blobs[manifest_url] = blobs[manifest_url].replace(
            "https://cdn.example.com/app.zip".encode(),
            "https://evil.example.com/app.zip".encode())
        self.assertIsNone(self._check(release, blobs))

    def test_a_manifest_for_another_version_is_refused(self):
        release, blobs = self._signed_release(version="1.9.8")
        self.assertIsNone(self._check(release, blobs))

    def test_a_missing_signature_asset_still_tries_the_sidecar_name(self):
        """签名可以只发成 ``latest.json.sig`` 附件名之外的 .sig 旁路。"""
        release, blobs = self._signed_release()
        release["assets"] = [a for a in release["assets"]
                             if a["name"] != sig.SIGNATURE_NAME]
        blobs["https://example.com/latest.json.sig"] = blobs.pop(
            "https://example.com/" + sig.SIGNATURE_NAME)
        self.assertIsNotNone(self._check(release, blobs))


@needs_cng
class JyiReleaseTests(unittest.TestCase):
    """JYI 工具中心那条路：先问工具中心，取不到/没签/验不过/不是新版，才退回 GitHub。

    换下载入口不等于放宽信任：工具中心里的清单一样要过钉死的公钥，验不过就当
    没有新版，不会因为"是本家域名"就装。
    """

    JYI = "https://jyi.test"
    SLUG = "wechat-ai-assistant"

    def setUp(self):
        self.public, self.private = sig.generate_keypair()
        pin = unittest.mock.patch.object(sig, "PUBLISHER_PUBKEY", self.public)
        pin.start()
        self.addCleanup(pin.stop)
        env = unittest.mock.patch.dict(os.environ, {
            updater.TOOLS_BASE_ENV: self.JYI, updater.TOOL_SLUG_ENV: self.SLUG})
        env.start()
        self.addCleanup(env.stop)

    def _download_url(self, platform="windows", arch="x64"):
        return f"{self.JYI}/api/tools/{self.SLUG}/download?platform={platform}&arch={arch}"

    def _manifest(self, version="1.10.0", zip_url=None, notes="工具中心上的说明",
                  sha256="cd" * 32):
        raw = sig.canonical_manifest(version=version, notes=notes,
                                     zip_url=zip_url or self._download_url(),
                                     sha256=sha256)
        return raw, sig.signature_text(sig.sign_bytes(raw, self.private))

    def _tool(self, version="1.10.0", notes="工具中心上的说明", asset_over=None,
              manifest=True, signature=None, **tool_over):
        """工具中心公开详情的返回体（``GET /api/tools/<slug>``）。"""
        raw, good = self._manifest(version=version, notes=notes)
        asset = {
            "platform": "windows", "arch": "x64",
            "filename": f"{updater.ASSET_PREFIX}{version}.zip", "sizeBytes": 4096,
            "sha256": "cd" * 32, "signatureStatus": "signed", "signatureDetail": "",
            "signedManifest": raw.decode("utf-8") if manifest else "",
            "signature": signature if signature is not None else good,
        }
        asset.update(asset_over or {})
        tool = {"slug": self.SLUG, "status": "published", "access": "free",
                "downloadsPaused": False, "version": version, "releaseNotes": notes,
                "assets": [asset]}
        tool.update(tool_over)
        return {"ok": True, "lang": "zh", "tool": tool}

    def _check(self, payload=None, release=None, blobs=None, current="1.9.9"):
        """payload=工具中心的详情（None=404）；release=GitHub 的 JSON（None=404）；
        blobs 按 URL 给字节（GitHub 上的清单/签名文件）。"""
        blobs = blobs or {}
        calls: list[str] = []

        def fetch_bytes(url, *a):
            if url in blobs:
                return blobs[url]
            raise OSError("404 " + url)

        def fetch_json(url, *a):
            calls.append(url)
            if "/api/tools/" in url:
                if payload is None:
                    raise OSError("404 " + url)
                return payload
            if release is None:
                raise OSError("404 " + url)
            return release

        with unittest.mock.patch.object(updater, "_http_get_bytes", side_effect=fetch_bytes), \
            unittest.mock.patch.object(updater, "_http_get_json", side_effect=fetch_json):
            return updater.check_for_update("some/repo", current), calls

    def _github_blobs(self, notes="GitHub 上的说明", version="1.10.0"):
        release = _release(version)
        raw, signature = self._manifest(version=version, notes=notes)
        blobs = {_with_manifest(release): raw,
                 "https://example.com/" + sig.SIGNATURE_NAME: signature.encode() + b"\n"}
        return release, blobs

    def test_the_tool_center_is_asked_first_and_decides(self):
        info, calls = self._check(payload=self._tool())
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["version"], "1.10.0")
        self.assertEqual(info["sha256"], "cd" * 32)
        self.assertEqual(info["notes"], "工具中心上的说明")
        self.assertEqual(info["zip_url"], self._download_url(),
                         "下载地址用的是清单里签过的那一个")
        self.assertEqual([u for u in calls if "api.github.com" in u], [],
                         "工具中心拿到了就别再打 GitHub（省一次限流名额）")

    def test_the_github_copy_is_kept_as_a_fallback_url(self):
        """工具中心的包地址挂了，同一版的 GitHub 附件还能兜一下。"""
        info, _ = self._check(payload=self._tool())
        assert info is not None
        self.assertEqual(len(info["alt_urls"]), 1)
        fallback = info["alt_urls"][0]
        self.assertIn("github.com/randomix777/wechat-ai-assistant/releases/download/v1.10.0/",
                      fallback)
        self.assertTrue(fallback.endswith(".zip"), fallback)
        self.assertEqual(unquote(fallback).rsplit("/", 1)[-1],
                         f"{updater.ASSET_PREFIX}1.10.0.zip",
                         "兜底地址就是这个版本的安装包，不用先问 GitHub API")

    def test_a_version_without_a_signed_manifest_falls_back(self):
        """工具中心里这版还没填签名清单（发布的人还没签）→ 当没有，去 GitHub 拿真清单。"""
        release, blobs = self._github_blobs()
        info, _ = self._check(payload=self._tool(manifest=False), release=release, blobs=blobs)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["notes"], "GitHub 上的说明")

    def test_a_forged_signature_does_not_block_the_real_one(self):
        """工具中心那份被换过签名 → 当它不存在，去 GitHub 拿真清单。"""
        other = sig.generate_keypair()[1]
        raw, _ = self._manifest()
        forged = sig.signature_text(sig.sign_bytes(raw, other))
        release, blobs = self._github_blobs()
        info, _ = self._check(payload=self._tool(signature=forged), release=release, blobs=blobs)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["notes"], "GitHub 上的说明")

    def test_a_tampered_manifest_in_the_tool_center_is_refused(self):
        """清单被改过（签名还在）→ 验不过，当没有新版。"""
        raw, signature = self._manifest()
        tampered = raw.replace(self._download_url().encode(),
                               b"https://evil.example.com/app.zip")
        info, _ = self._check(payload=self._tool(asset_over={
            "signedManifest": tampered.decode("utf-8"), "signature": signature}))
        self.assertIsNone(info)

    def test_the_tool_center_having_nothing_at_all_falls_back(self):
        release, blobs = self._github_blobs()
        info, _ = self._check(release=release, blobs=blobs)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["version"], "1.10.0")

    def test_the_tool_center_holding_an_old_version_does_not_hide_a_newer_one(self):
        """工具中心里还挂着当前版本（发布的人忘了传新版）时，GitHub 上的新版照样能查到。"""
        release, blobs = self._github_blobs()
        info, _ = self._check(payload=self._tool(version="1.9.9"),
                              release=release, blobs=blobs)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["version"], "1.10.0")

    def test_a_paused_download_is_not_an_update(self):
        """后台把下载暂停了：连 GitHub 都不问，这次就不更新。"""
        info, calls = self._check(payload=self._tool(downloadsPaused=True))
        self.assertIsNone(info)
        self.assertEqual([u for u in calls if "api.github.com" in u], [],
                         "暂停下载时不该再去别处找同一个包")

    def test_only_a_windows_asset_counts(self):
        """工具中心只挂了 mac 的包 → 当没有新版（不猜别的平台）。"""
        mac = self._tool(asset_over={"platform": "macos", "arch": "arm64"})
        self.assertIsNone(self._check(payload=mac)[0])

    def test_the_windows_x64_asset_wins_over_other_windows_arches(self):
        """同一版挂了两个 Windows 架构：x64 优先，别把 arm64 的包装给商户。"""
        payload = self._tool()
        arm = dict(payload["tool"]["assets"][0])
        arm.update({"arch": "arm64", "filename": f"{updater.ASSET_PREFIX}1.10.0-arm64.zip"})
        payload["tool"]["assets"] = [arm] + payload["tool"]["assets"]
        info, _ = self._check(payload=payload)
        assert info is not None
        self.assertEqual(info["zip_url"], self._download_url())

    def test_empty_manifest_notes_fall_back_to_the_tool_page(self):
        """清单里没写更新说明时，用工具中心那一版的说明。"""
        raw, signature = self._manifest(notes="")
        payload = self._tool(notes="工具中心页面上的说明",
                             asset_over={"signedManifest": raw.decode("utf-8"),
                                         "signature": signature})
        info, _ = self._check(payload=payload)
        assert info is not None
        self.assertEqual(info["notes"], "工具中心页面上的说明")

    def test_the_tools_base_can_be_turned_off_or_pointed_elsewhere(self):
        with unittest.mock.patch.dict(os.environ, {updater.TOOLS_BASE_ENV: ""}):
            self.assertEqual(updater.tools_base(), "", "空串=只用 GitHub")
        with unittest.mock.patch.dict(os.environ, {updater.TOOLS_BASE_ENV: "https://self.test/r/"}):
            self.assertEqual(updater.tools_base(), "https://self.test/r")
        with unittest.mock.patch.dict(os.environ, {updater.TOOL_SLUG_ENV: ""}):
            self.assertEqual(updater.tool_slug(), "", "工具名给空串也等于关掉这条路")
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(updater.TOOLS_BASE_ENV, None)
            os.environ.pop(updater.TOOL_SLUG_ENV, None)
            self.assertEqual(updater.tools_base(), updater.JYI_TOOLS_BASE)
            self.assertEqual(updater.tool_slug(), updater.JYI_TOOL_SLUG)

    def test_the_slug_is_asked_for_by_name(self):
        _, calls = self._check(payload=self._tool())
        self.assertEqual(calls, [f"{self.JYI}/api/tools/{self.SLUG}?lang=zh"])


class StageTests(unittest.TestCase):
    def _stage(self, data: bytes, sha: str):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        info = {"version": "1.9.9", "notes": "",
                "zip_url": "https://example.com/a.zip",
                "sha_url": "https://example.com/a.zip.sha256"}
        with unittest.mock.patch.object(
                updater, "_http_get_bytes",
                side_effect=[data, (sha + "  a.zip\n").encode()]):
            staged = updater.download_and_stage(info, base)
        return base, staged

    def test_happy_path_stages_and_writes_pending(self):
        data = _fake_app_zip()
        digest = hashlib.sha256(data).hexdigest()
        base, staged = self._stage(data, digest)
        try:
            self.assertIsNotNone(staged)
            assert staged is not None
            self.assertTrue((staged / "微信AI智能助理.exe").is_file())
            pending = json.loads((base / updater.PENDING_NAME).read_text(encoding="utf-8"))
            self.assertEqual(pending["version"], "1.9.9")
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_bad_hash_discards_everything(self):
        base, staged = self._stage(_fake_app_zip(), "0" * 64)
        try:
            self.assertIsNone(staged)
            self.assertFalse((base / updater.PENDING_NAME).exists())
            self.assertFalse((base / updater.UPDATES_DIR_NAME).exists())
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_no_sha_file_refuses_install(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        info = {"version": "1.9.9", "notes": "",
                "zip_url": "https://example.com/a.zip", "sha_url": ""}
        try:
            with unittest.mock.patch.object(
                    updater, "_http_get_bytes",
                    return_value=_fake_app_zip()):
                self.assertIsNone(updater.download_and_stage(info, base))
            self.assertFalse((base / updater.PENDING_NAME).exists())
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_garbage_zip_is_discarded(self):
        base, staged = self._stage(b"not-a-zip", hashlib.sha256(b"not-a-zip").hexdigest())
        try:
            self.assertIsNone(staged)
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


    def test_the_manifest_hash_is_used_without_fetching_a_sha_file(self):
        """签名清单里已经写着哈希，就别再去下 .sha256（少一次网络请求）。"""
        data = _fake_app_zip()
        digest = hashlib.sha256(data).hexdigest()
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        info = {"version": "1.9.9", "notes": "", "sha256": digest,
                "zip_url": "https://cdn.example.com/a.zip", "alt_urls": []}
        try:
            with unittest.mock.patch.object(
                    updater, "_http_get_bytes", return_value=data) as fetch:
                staged = updater.download_and_stage(info, base)
            self.assertIsNotNone(staged)
            self.assertEqual(fetch.call_count, 1)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_the_release_asset_is_the_fallback_when_the_manifest_url_dies(self):
        """清单给的地址（多半是 CDN）挂了，要退到 release 附件继续下。"""
        data = _fake_app_zip()
        digest = hashlib.sha256(data).hexdigest()
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        alt = "https://example.com/微信AI智能助理-win64-1.9.9.zip"
        info = {"version": "1.9.9", "notes": "", "sha256": digest,
                "zip_url": "https://cdn.example.com/dead.zip", "alt_urls": [alt]}
        calls = []

        def fetch(url, *args):
            calls.append(url)
            if url == info["zip_url"]:
                raise OSError("cdn 没了")
            return data

        try:
            with unittest.mock.patch.object(updater, "_http_get_bytes", side_effect=fetch):
                staged = updater.download_and_stage(info, base)
            self.assertIsNotNone(staged)
            self.assertEqual(calls, [info["zip_url"], alt])
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_no_downloadable_url_at_all_keeps_the_old_version(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        info = {"version": "1.9.9", "notes": "", "sha256": "ab" * 32,
                "zip_url": "", "alt_urls": []}
        try:
            with unittest.mock.patch.object(updater, "_http_get_bytes",
                                            side_effect=OSError("no")):
                self.assertIsNone(updater.download_and_stage(info, base))
            self.assertFalse((base / updater.PENDING_NAME).exists())
        finally:
            shutil.rmtree(base, ignore_errors=True)


class PendingTests(unittest.TestCase):
    def test_missing_or_broken_is_none(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        try:
            self.assertIsNone(updater.pending_update(base))
            (base / updater.PENDING_NAME).write_text("garbage{", encoding="utf-8")
            self.assertIsNone(updater.pending_update(base))
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class ScriptTests(unittest.TestCase):
    def test_script_waits_swaps_rolls_back(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        try:
            info = {"version": "1.9.9", "dir": "1.9.9", "notes": ""}
            script = updater.build_updater_script(base, info, "微信AI智能助理.exe", 1234)
            text = script.read_text(encoding="utf-8")
            self.assertIn("1234", text)
            self.assertIn("微信AI智能助理.exe", text)
            # 等旧进程 → 搬目录 → 起新版 → 15 秒观测 → 起不来回滚 → 自删
            for needle in ("tasklist", "rmdir", "move", "-n 16 127.0.0.1", 'del "%~f0"'):
                self.assertIn(needle, text, needle)
            # 外部命令走绝对路径（PATH 里的同名工具不是我们想要的，见 updater 注释）
            self.assertIn("%SYS%\\ping.exe", text)
            self.assertIn("%SYS%\\tasklist.exe", text)
            # 等待只能用 ping：timeout.exe 在标准输入被重定向（GUI 拉起的脚本就是这样）
            # 会直接退出，真机上那两处等待就都没了。
            self.assertNotIn("timeout.exe", text)
            self.assertTrue(str(script).endswith(".bat"))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_the_settle_time_is_configurable(self):
        """真机上留 15 秒余量；测试用小的，好把整条路径跑完。"""
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        try:
            info = {"version": "1.9.9", "dir": "1.9.9", "notes": ""}
            quick = updater.build_updater_script(base, info, "app.exe", 1,
                                                 settle_seconds=2)
            text = quick.read_text(encoding="utf-8")
            self.assertIn("-n 3 127.0.0.1", text)   # 等 2 秒 → ping -n 3
            self.assertNotIn("-n 16 127.0.0.1", text)
        finally:
            shutil.rmtree(base, ignore_errors=True)


@unittest.skipUnless(sys.platform == "win32", "换版脚本只在 Windows 上跑")
class ScriptExecutionTests(unittest.TestCase):
    """**真的把 .bat 跑起来**：等进程、搬目录、起"新版"、备份、回滚、便携 data 迁移。

    "新版主程序"用的是系统 ``ping.exe`` 改名的副本 —— 真程序起来会弹界面，测试里
    不能这么干；而这段脚本判断"新版活了没有"靠的是 tasklist 里有没有这个映像名，
    一个同名的系统进程就够把整条路径走完。这也正是"换版演练"要证明的东西：
    中文路径、中文主程序名、便携数据在真 cmd 下都过得去。
    """

    def _system_ping(self) -> Path:
        root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        source = root / "System32" / "ping.exe"
        if not source.is_file():  # pragma: no cover - 正常 Windows 都有
            self.skipTest("这台机器上没有 ping.exe 可以拿来当假主程序")
        return source

    def _layout(self, version: str) -> tuple[Path, Path]:
        base = Path(tempfile.mkdtemp(prefix="pyw-swap-")) / "微信AI智能助理"
        (base / "_internal").mkdir(parents=True)
        (base / "微信AI智能助理.exe").write_bytes(b"OLD")
        (base / "data").mkdir()
        (base / "data" / "shop.json").write_text('{"旧": 1}', encoding="utf-8")
        # 启动时读到的"待换版"标记（真流程里由 download_and_stage 写）。脚本必须把它
        # 收掉：脚本收尾会重新启动软件，那一次启动又会读到它 —— 不收就是死循环。
        (base / updater.PENDING_NAME).write_text(
            '{"version": "%s", "dir": "%s"}' % (version, version), encoding="utf-8")
        staged = base / updater.UPDATES_DIR_NAME / version
        (staged / "_internal").mkdir(parents=True)
        (staged / "data").mkdir()  # 新版包自带的空 data（真包常带）
        (staged / "新版标记.txt").write_text("new", encoding="utf-8")
        return base, staged

    def _generate(self, base: Path, version: str) -> Path:
        # 用一个真的已经退出的 pid，别用可能被复用的假号
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        # 过程日志是追加写的（同一个版本反复演练会叠在一起），先清掉再断言
        self._log_path(version).unlink(missing_ok=True)
        return updater.build_updater_script(
            base, {"version": version, "dir": version, "notes": ""},
            "微信AI智能助理.exe", gone.pid, settle_seconds=2)

    def _log_path(self, version: str) -> Path:
        log = Path(tempfile.gettempdir()) / f"pyw-apply-{version}.log"
        self.addCleanup(log.unlink, missing_ok=True)
        return log

    def _log_text(self, version: str) -> str:
        log = self._log_path(version)
        return log.read_text(encoding="utf-8") if log.is_file() else ""

    def _run(self, script: Path) -> None:
        done = subprocess.run(["cmd", "/c", str(script)],
                              capture_output=True, timeout=120)
        # 退出码不说明什么：cmd 里内部命令成功时不会把 errorlevel 清回 0，而脚本末尾
        # 的 `del "%~f0"` 又常常继承着刚才那次"追加失败"的 1。真正说明"跑到底了"的
        # 是脚本把自己删掉了。
        self.assertFalse(script.exists(),
                         "脚本没跑到底：" + done.stdout.decode("utf-8", "replace"))

    def test_a_working_new_version_is_swapped_in_and_the_backup_is_dropped(self):
        version = "1.99.1"
        base, staged = self._layout(version)
        self.addCleanup(shutil.rmtree, base.parent, ignore_errors=True)
        shutil.copy2(self._system_ping(), staged / "微信AI智能助理.exe")
        # 新版起来后是常驻的：这里先替它把进程起上（脚本自己那次 start 不带参数会
        # 立刻退出），让它"活着"，于是走成功分支。
        alive = subprocess.Popen([str(staged / "微信AI智能助理.exe"), "-t", "127.0.0.1"])
        self.addCleanup(alive.kill)
        try:
            time.sleep(0.5)
            self._run(self._generate(base, version))

            self.assertTrue((base / "新版标记.txt").is_file(), "新版应该已经就位")
            self.assertEqual((base / "data" / "shop.json").read_text(encoding="utf-8"),
                             '{"旧": 1}', "便携数据要跟着搬过来")
            self.assertFalse(
                (base.parent / (base.name + updater.BACKUP_PREFIX + version)).exists(),
                "成功了就该把备份删掉")
            self.assertFalse((base / updater.UPDATES_DIR_NAME / version).exists(),
                             "暂存目录已经搬空了")
            self.assertIn("换版完成", self._log_text(version),
                          "换版过程要留下日志，商户机器上出事才查得动")
            self.assertFalse((base / updater.PENDING_NAME).exists(),
                             "换完了就要把待换版标记收掉，不然重启时会再换一遍")
        finally:
            subprocess.run(["taskkill", "/F", "/IM", "微信AI智能助理.exe"],
                           capture_output=True)

    def test_a_new_version_that_dies_is_rolled_back(self):
        version = "1.99.2"
        base, staged = self._layout(version)
        self.addCleanup(shutil.rmtree, base.parent, ignore_errors=True)
        # ping 不带参数会打印用法后立刻退出 —— 正是"新版起不来"
        shutil.copy2(self._system_ping(), staged / "微信AI智能助理.exe")
        self._run(self._generate(base, version))

        self.assertFalse((base / "新版标记.txt").exists(), "新版起不来就该被换掉")
        self.assertEqual((base / "微信AI智能助理.exe").read_bytes(), b"OLD",
                         "旧版主程序应该回来了")
        self.assertEqual((base / "data" / "shop.json").read_text(encoding="utf-8"),
                         '{"旧": 1}', "回滚也不能丢便携数据")
        self.assertIn("回滚旧版", self._log_text(version),
                      "回滚原因要写进日志")
        self.assertFalse((base / updater.PENDING_NAME).exists(),
                         "回滚也要把待换版标记收掉：否则每次启动都要再失败一次")
        self.assertTrue(updater.version_is_blocked(version, base),
                        "换上去没起来的版本要留记号，后台查更新就别再下它了")


class BackgroundTests(unittest.TestCase):
    def test_staged_callback_fires(self):
        info = {"version": "1.9.9", "notes": "", "zip_url": "x", "sha_url": "y"}
        seen = []
        with unittest.mock.patch.object(updater, "is_frozen_app", return_value=True), \
            unittest.mock.patch.object(updater, "check_for_update", return_value=info), \
            unittest.mock.patch.object(updater, "download_and_stage",
                                       return_value=Path("staged")):
            thread = updater.check_in_background(on_staged=seen.append)
            thread.join(timeout=10)
        self.assertEqual(seen, [info])

    def test_nothing_new_calls_nothing(self):
        with unittest.mock.patch.object(updater, "is_frozen_app", return_value=True), \
            unittest.mock.patch.object(updater, "check_for_update", return_value=None):
            thread = updater.check_in_background(
                on_staged=lambda info: (_ for _ in ()).throw(AssertionError("不该被调")))
            thread.join(timeout=10)

    def test_the_check_knows_which_version_is_running(self):
        """不传本机版本就会拿空串去比，界面一启动就去下自己已经在跑的那版。"""
        seen = {}

        def fake_check(repo=updater.DEFAULT_REPO, current=""):
            seen["current"] = current
            return None

        with unittest.mock.patch.object(updater, "is_frozen_app", return_value=True), \
            unittest.mock.patch.object(updater, "check_for_update", side_effect=fake_check), \
            unittest.mock.patch.object(updater, "current_version", return_value="1.9.9"):
            updater.check_in_background().join(timeout=10)
        self.assertEqual(seen.get("current"), "1.9.9",
                         "后台查更新必须带上本机版本号")

    def test_found_but_not_downloaded_is_reported(self):
        """查到新版却没下成，必须回调一句 —— 否则商户以为"再也不更新了"。

        最常见的成因就是没登录：工具中心的下载地址要一枚登录态换来的短效凭据。
        """
        info = {"version": "1.9.9", "zip_url": "x"}
        failures: list[tuple] = []
        staged: list = []
        with unittest.mock.patch.object(updater, "is_frozen_app", return_value=True), \
            unittest.mock.patch.object(updater, "check_for_update", return_value=info), \
            unittest.mock.patch.object(updater, "download_and_stage", return_value=None):
            updater.check_in_background(on_staged=staged.append,
                                        on_failed=lambda i, hint: failures.append((i, hint)),
                                        jyi_client=None).join(timeout=10)
        self.assertEqual(staged, [], "没下成就不能说「已就绪」")
        self.assertEqual(len(failures), 1, "失败要说一次")
        returned_info, hint = failures[0]
        self.assertEqual(returned_info, info)
        self.assertIn("登录", hint, "没登录时要直接说登录，别只说「下载失败」")

    def test_a_download_failure_with_login_says_retry(self):
        """登录着也下不下来时，说的话不能是"去登录"（那会把人指错方向）。"""
        class LoggedIn:
            def tool_access_token(self):
                return "jyi_tool_abc"

        failures: list[str] = []
        with unittest.mock.patch.object(updater, "is_frozen_app", return_value=True), \
            unittest.mock.patch.object(updater, "check_for_update",
                                       return_value={"version": "1.9.9"}), \
            unittest.mock.patch.object(updater, "download_and_stage", return_value=None):
            updater.check_in_background(on_failed=lambda i, hint: failures.append(hint),
                                        jyi_client=LoggedIn()).join(timeout=10)
        self.assertEqual(len(failures), 1)
        self.assertNotIn("登录", failures[0], "已经登录了，别叫他去登录")
        self.assertIn("再试", failures[0])


class ApplyTests(unittest.TestCase):
    def test_dev_mode_does_nothing(self):
        with unittest.mock.patch.object(updater.sys, "frozen", False, create=True):
            self.assertFalse(updater.is_frozen_app())
            self.assertFalse(updater.apply_pending_update(Path(".")))

    def test_frozen_with_pending_spawns_updater(self):
        base = Path(tempfile.mkdtemp(prefix="pyw-upd-"))
        try:
            staged = base / updater.UPDATES_DIR_NAME / "1.9.9"
            staged.mkdir(parents=True)
            (staged / "微信AI智能助理.exe").write_bytes(b"EXE")
            (staged / "_internal").mkdir()
            (base / updater.PENDING_NAME).write_text(
                json.dumps({"version": "1.9.9", "dir": "1.9.9"}), encoding="utf-8")
            (base / "微信AI智能助理.exe").write_bytes(b"OLD")
            spawned = []
            with unittest.mock.patch.object(updater.sys, "frozen", True, create=True), \
                unittest.mock.patch("subprocess.Popen",
                                    side_effect=lambda *a, **k: spawned.append((a, k)) or unittest.mock.Mock()):
                self.assertTrue(updater.apply_pending_update(base))
            self.assertEqual(len(spawned), 1)
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class BlockedVersionTests(unittest.TestCase):
    """换版失败留下的记号：读得出来，而且**不许被清掉**（清了会重下同一个坏版本）。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="pyw-blocked-"))
        self.addCleanup(shutil.rmtree, self.base, True)

    def _mark(self, version: str) -> None:
        updater.blocked_marker(version, self.base).write_text("没起来", encoding="utf-8")

    def test_without_a_marker_there_is_nothing_to_say(self):
        self.assertEqual(updater.blocked_versions(self.base), [])
        self.assertEqual(updater.failed_update_note(self.base), "")

    def test_the_newest_blocked_version_is_the_one_named(self):
        self._mark("1.9.9")
        self._mark("1.10.0")
        self.assertEqual(updater.blocked_versions(self.base), ["1.9.9", "1.10.0"])
        note = updater.failed_update_note(self.base)
        self.assertIn("1.10.0", note)
        self.assertNotIn("1.9.9", note, "说最近那一版就够了")

    def test_the_marker_stays_so_the_broken_version_is_not_downloaded_again(self):
        self._mark("1.10.0")
        self.assertTrue(updater.version_is_blocked("1.10.0", self.base))
        self.assertFalse(updater.version_is_blocked("1.10.1", self.base),
                         "别的版本不该被连坐")


class UpdateNoteTests(unittest.TestCase):
    """新版暂存好了要说一句 —— 静默更新不能等于一声不吭。

    以前这句挂在 Tk 顶栏（界面已删）。现在由 ``web_server`` 写进状态，
    页面顶栏显示；测试直接验那一步，不点窗口。
    """

    def test_staged_version_shows_in_the_state(self):
        import types
        from unittest import mock

        from wechat_ai_assistant import web_server

        state = web_server.WebState(app=types.SimpleNamespace())
        captured = {}

        def fake_check(on_staged=None, **_kwargs):
            captured["cb"] = on_staged
            return types.SimpleNamespace()

        with mock.patch("wechat_ai_assistant.updater.check_in_background", fake_check):
            web_server.start_background_update_check(
                state, types.SimpleNamespace(client=None), delay=0.0)
            for _ in range(50):
                if "cb" in captured:
                    break
                import time as _time

                _time.sleep(0.05)
            captured["cb"]({"version": "1.9.9"})

        note = state.snapshot()["update_note"]
        self.assertIn("1.9.9", note)
        self.assertIn("下次打开", note)

    def test_a_download_failure_is_told_to_the_merchant(self):
        """查到新版却没下成：顶栏要说出来，不能只在日志里。

        最常见的成因是没登录（工具中心的下载地址要登录态换的凭据）。
        不说的话，商户看到的现象是"这软件再也不更新了"，客服也问不出所以然。
        """
        import types
        from unittest import mock

        from wechat_ai_assistant import web_server

        state = web_server.WebState(app=types.SimpleNamespace())
        captured = {}

        def fake_check(on_staged=None, on_failed=None, **_kwargs):
            captured["failed"] = on_failed
            return types.SimpleNamespace()

        with mock.patch("wechat_ai_assistant.updater.check_in_background", fake_check):
            web_server.start_background_update_check(
                state, types.SimpleNamespace(client=None), delay=0.0)
            for _ in range(50):
                if "failed" in captured:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(captured.get("failed"), "要接上失败回调")
            captured["failed"]({"version": "9.9.9"}, "还没登录产品账号，登录后才能下载")

        note = state.snapshot()["update_note"]
        self.assertIn("9.9.9", note, "要说清是哪个版本")
        self.assertIn("没下下来", note)
        self.assertIn("登录", note, "把原因和下一步一起给出来")

    def test_a_failed_swap_is_told_to_the_merchant(self):
        """上次换版没成：界面上要有一句话，不能只躺在 %TEMP% 的运行日志里。"""
        import shutil
        import tempfile
        import types
        from unittest import mock

        from wechat_ai_assistant import updater, web_server

        base = Path(tempfile.mkdtemp(prefix="pyw-blocked-ui-"))
        self.addCleanup(shutil.rmtree, base, True)
        updater.blocked_marker("9.9.9", base).write_text("没起来", encoding="utf-8")
        state = web_server.WebState(app=types.SimpleNamespace())

        with mock.patch.object(updater, "app_dir", return_value=base), \
                mock.patch("wechat_ai_assistant.updater.check_in_background",
                           lambda **_kwargs: types.SimpleNamespace()):
            web_server.start_background_update_check(
                state, types.SimpleNamespace(client=None), delay=0.0)
            for _ in range(50):
                if state.update_note:
                    break
                time.sleep(0.05)

        self.assertIn("9.9.9", state.update_note)

    def test_empty_info_stays_quiet(self):
        import types
        from unittest import mock

        from wechat_ai_assistant import web_server

        state = web_server.WebState(app=types.SimpleNamespace())
        captured = {}

        def fake_check(on_staged=None, **_kwargs):
            captured["cb"] = on_staged
            return types.SimpleNamespace()

        with mock.patch("wechat_ai_assistant.updater.check_in_background", fake_check):
            web_server.start_background_update_check(
                state, types.SimpleNamespace(client=None), delay=0.0)
            for _ in range(50):
                if "cb" in captured:
                    break
                import time as _time

                _time.sleep(0.05)
            captured["cb"]({})

        self.assertEqual(state.snapshot()["update_note"], "")


class StartupStaysOfflineTests(unittest.TestCase):
    """启动路径上不许再联网下载（用户点名的「不再卡启动」）。

    以前是启动时同步查 GitHub、下完 70 兆才显示窗口；现在启动只应用已经下好的
    暂存更新，查/下都搬到界面起来之后的后台线程里。
    """

    def _source(self, name: str) -> str:
        return (_ROOT / "src" / "wechat_ai_assistant" / name).read_text(encoding="utf-8")

    def test_the_launcher_only_applies_what_is_already_staged(self):
        text = self._source("launcher.py")
        self.assertIn("apply_pending_update", text)
        # 只揪"真的调了"，文档字符串里提一句不算（launcher 的注释解释了为什么搬走）
        for banned in ("check_for_update(", "download_and_stage(", "check_in_background("):
            self.assertNotIn(banned, text,
                             f"启动器里不该再调用 {banned}：那是启动期联网下载，会卡启动")

    def test_the_window_checks_in_the_background_after_it_is_up(self):
        """界面起来**之后**才查新版：启动路径上不联网，双击就该看见窗口。"""
        text = self._source("web_app.py")
        self.assertIn("start_background_update_check", text)
        self.assertIn("open_window", text)
        server = self._source("web_server.py")
        self.assertIn("delay: float = 2.0", server, "至少要等窗口起来再查")
        self.assertIn("check_in_background(", server)

    def test_nothing_pending_means_nothing_to_do(self):
        from wechat_ai_assistant import launcher

        with unittest.mock.patch.object(updater, "is_frozen_app", lambda: True), \
                unittest.mock.patch.object(updater, "pending_update", lambda *a, **k: None):
            self.assertFalse(launcher._check_and_apply_update())

    def test_a_staged_update_is_handed_to_the_script(self):
        from wechat_ai_assistant import launcher

        seen: list[str] = []
        with unittest.mock.patch.object(updater, "is_frozen_app", lambda: True), \
                unittest.mock.patch.object(updater, "pending_update",
                                           lambda *a, **k: {"version": "9.9.9"}), \
                unittest.mock.patch.object(updater, "apply_pending_update",
                                           lambda *a, **k: seen.append("go") or True), \
                unittest.mock.patch.object(launcher, "_show_update_splash",
                                           lambda *a, **k: None):
            self.assertTrue(launcher._check_and_apply_update())
        self.assertEqual(seen, ["go"])


if __name__ == "__main__":
    unittest.main()
