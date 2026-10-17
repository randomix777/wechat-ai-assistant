"""发版签名的回归测试：真签真验（走 Windows CNG），不联网、不碰真程序目录。

验签是更新链上唯一的信任根，所以这里必须打真签名 —— mock 掉 CNG 就等于
把这条链最该被证明的一段变成假设。
"""
from __future__ import annotations

import base64
import sys
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from wechat_ai_assistant import update_signature as sig  # noqa: E402

needs_cng = unittest.skipUnless(sig.available(), "需要 Windows 自带的加密组件（bcrypt.dll）")


class PinnedKeyTests(unittest.TestCase):
    """钉死的公钥本身该永远合法：写坏了等于更新永久停摆（fail-closed）。"""

    def test_the_pinned_key_is_a_well_formed_p256_point(self):
        self.assertTrue(sig.is_pinned(), "应该在源码里钉了发布方公钥")
        text = sig.pinned_key_hex()
        self.assertEqual(len(text), sig.PUBKEY_HEX_LENGTH)
        self.assertEqual(text, text.lower())
        int(text, 16)  # 全是十六进制

    def test_an_empty_key_means_not_pinned(self):
        self.assertFalse(sig._is_public_key(""))
        self.assertFalse(sig._is_public_key("abc"))


@needs_cng
class KeyPairTests(unittest.TestCase):
    def test_a_fresh_pair_agrees_with_itself(self):
        public, private = sig.generate_keypair()
        self.assertEqual(len(public), sig.PUBKEY_HEX_LENGTH)
        self.assertEqual(sig.public_hex_from_private(private), public)

    def test_the_private_blob_is_the_cng_shape(self):
        _, private = sig.generate_keypair()
        blob = base64.b64decode(private)
        # magic + cbKey + X + Y + d
        self.assertEqual(len(blob), 8 + sig.PUBKEY_BYTES + sig.KEY_BYTES)

    def test_two_pairs_differ(self):
        first, _ = sig.generate_keypair()
        second, _ = sig.generate_keypair()
        self.assertNotEqual(first, second)

    def test_a_broken_private_key_is_reported(self):
        for bad in ("", "not-base64!!", base64.b64encode(b"short").decode()):
            with self.assertRaises(OSError):
                sig.public_hex_from_private(bad)


@needs_cng
class VerifyTests(unittest.TestCase):
    def _signed(self, private: str | None = None, **overrides):
        if private is None:
            public, private = sig.generate_keypair()
        else:
            public = sig.public_hex_from_private(private)
        fields = {"version": "1.10.0", "zip_url": "https://example.com/app.zip",
                  "sha256": "ab" * 32, "notes": "修好了几处"}
        fields.update(overrides)
        manifest = sig.canonical_manifest(**fields)
        return public, manifest, sig.sign_bytes(manifest, private), private

    def test_a_signed_manifest_verifies(self):
        public, manifest, signature, _ = self._signed()
        self.assertTrue(sig.verify_manifest(manifest, signature, public))
        # 不给公钥时用的是钉死的那把 —— 于是这份签名当然不该通过
        self.assertFalse(sig.verify_manifest(manifest, signature))

    def test_signature_text_form_verifies(self):
        public, manifest, signature, _ = self._signed()
        text = sig.signature_text(signature).encode()
        self.assertTrue(sig.verify_manifest(manifest, text, public))

    def test_a_tampered_manifest_is_rejected(self):
        public, manifest, signature, _ = self._signed()
        tampered = manifest.replace("修好了几处".encode(), "换了份安装包".encode())
        self.assertNotEqual(tampered, manifest)
        self.assertFalse(sig.verify_manifest(tampered, signature, public))

    def test_another_key_cannot_sign_for_us(self):
        _, manifest, signature, _ = self._signed()
        other, _ = sig.generate_keypair()
        self.assertFalse(sig.verify_manifest(manifest, signature, other))

    def test_junk_is_rejected_without_raising(self):
        public, manifest, signature, _ = self._signed()
        self.assertFalse(sig.verify_manifest(b"", signature, public))
        self.assertFalse(sig.verify_manifest(manifest, b"", public))
        self.assertFalse(sig.verify_manifest(manifest, signature, "zzzz"))
        self.assertFalse(sig.verify_manifest(manifest, b"not a signature", public))
        self.assertFalse(sig.verify_manifest(manifest, b"a" * 10, public))


@needs_cng
class EndToEndManifestTests(unittest.TestCase):
    """发版工具那一步 → 客户端那一步：字节必须原样对接。"""

    def test_what_the_tool_writes_is_what_the_client_verifies(self):
        public, private = sig.generate_keypair()
        digest = "3f" * 32
        # 发版工具：规范化字节 → 签名 → 写两个文件（这里只在内存里模拟）
        raw = sig.canonical_manifest(version="v1.10.0", zip_url="https://example.com/a.zip",
                                    sha256=digest, notes="折腾了一下")
        signature_file = sig.signature_text(sig.sign_bytes(raw, private)).encode() + b"\n"

        # 客户端：原样下载这两个文件 → 验签 → 解析
        self.assertTrue(sig.verify_manifest(raw, signature_file, public))
        parsed = sig.parse_manifest(raw)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["version"], "1.10.0")
        self.assertEqual(parsed["sha256"], digest)
        self.assertEqual(parsed["zip_url"], "https://example.com/a.zip")
        self.assertEqual(parsed["notes"], "折腾了一下")


class ManifestTests(unittest.TestCase):
    def test_canonical_bytes_are_stable_and_sorted(self):
        first = sig.canonical_manifest("1.10.0", "https://e.com/a.zip", "AB" * 32, notes="中文")
        second = sig.canonical_manifest("1.10.0", "https://e.com/a.zip", "ab" * 32, notes="中文")
        self.assertEqual(first, second)  # 大小写/顺序都不影响那份字节
        self.assertIn("中文".encode(), first)  # 不转义成 \uXXXX
        self.assertLess(first.index(b"notes"), first.index(b"sha256"))  # 键有序

    def test_parse_accepts_a_good_manifest(self):
        raw = sig.canonical_manifest("1.10.0", "https://e.com/a.zip", "ab" * 32)
        parsed = sig.parse_manifest(raw)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["version"], "1.10.0")

    def test_parse_refuses_broken_manifests(self):
        cases = [
            b"not json",
            b"[]",
            b"{}",
            b'{"version": "1.10"}',                                  # 没有哈希和地址
            b'{"version": "1.10.0", "sha256": "zz", "zip_url": "https://e.com/a.zip"}',
            b'{"version": "1.10.0", "sha256": "' + b"ab" * 32
            + b'", "zip_url": "ftp://e.com/a.zip"}',                 # 不是 http(s)
        ]
        for raw in cases:
            self.assertIsNone(sig.parse_manifest(raw), raw)


class LegacyPathTests(unittest.TestCase):
    """公钥没钉（空）时不该再去验签：那是老部署的老路。"""

    def test_an_empty_key_skips_verification(self):
        with unittest.mock.patch.object(sig, "PUBLISHER_PUBKEY", ""):
            self.assertFalse(sig.is_pinned())
            self.assertFalse(sig.verify_manifest(b"{}", b"x"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
