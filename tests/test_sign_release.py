"""发版签名工具：本地签、远程签、私钥格式转换。

远程签真正要保证的不是「能签出来」，而是**本机一定会自己验一遍**：
签名服务在「外面」，它算错、串了钥匙、或者路上被换掉，都必须在这里被拦住 ——
一份签名不对的清单发出去，全量商户客户端都会拒收这一版更新。
所以这里的重点用例是「验不过就不落盘」。
"""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from wechat_ai_assistant import update_signature as signing

ROOT = Path(__file__).resolve().parents[1]
ZIP_NAME = "wechat-ai-assistant-win64-1.101.0.zip"
TOKEN = "tok-1234"

needs_cng = unittest.skipUnless(
    signing.available(), "需要 Windows 自带的加密组件（bcrypt.dll）")


def _load_signer():
    """deploy/sign_release.py 不在包里的模块，按路径加载。"""
    path = ROOT / "deploy" / "sign_release.py"
    spec = importlib.util.spec_from_file_location("sign_release_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _SignService:
    """冒充 JYI worker 里的签名路由：Bearer 校验 + 返回 raw r||s 的 base64。"""

    def __init__(self, private_key: str, *, token: str = TOKEN,
                 public_key: str | None = None, tamper: bool = False,
                 status: int = 200) -> None:
        self.private_key = private_key
        self.token = token
        self.public_key = public_key
        self.tamper = tamper
        self.status = status
        self.seen: list[bytes] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server 的命名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.seen.append(body)
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error": "bad token"}')
                    return
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": "nope"}')
                    return
                signed = body + b" " if outer.tamper else body
                payload: dict[str, str] = {
                    "signature": base64.b64encode(
                        signing.sign_bytes(signed, outer.private_key)).decode("ascii"),
                }
                if outer.public_key is not None:
                    payload["public_key"] = outer.public_key
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/api/internal/release/sign"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _SignerTestCase(unittest.TestCase):
    """公共夹具：一对一次性密钥、一个假安装包、把客户端钉的公钥换成这把。"""

    def setUp(self) -> None:
        self.signer = _load_signer()
        self.public_hex, self.private_b64 = signing.generate_keypair()
        tmp = tempfile.TemporaryDirectory(prefix="pyw-sign-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.zip = self.base / ZIP_NAME
        self.zip.write_bytes(b"PK\x03\x04 fake package; only the hash matters")
        self.out = self.base / "dist"
        patcher = mock.patch.object(signing, "PUBLISHER_PUBKEY", self.public_hex)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv: list[str]) -> int:
        """跑命令行本体，同时把输出收纳掉（控制台是 cp1252，中文直接打会炸）。"""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.signer.main(argv)

    def run_cli_expecting_failure(self, argv: list[str]) -> str:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as caught:
                self.signer.main(argv)
        return f"{caught.exception}\n{err.getvalue()}"

    def sign_args(self, url: str, token: str = TOKEN) -> list[str]:
        return ["sign", str(self.zip), "--remote", url, "--token", token,
                "--out", str(self.out), "--notes", "换版了"]

    def key_file(self) -> Path:
        path = self.base / "publisher-key.json"
        path.write_text(json.dumps(
            {"version": 1, "public_key": self.public_hex, "private_key": self.private_b64}),
            encoding="utf-8")
        return path


@needs_cng
class ExportJwkTests(_SignerTestCase):
    def test_the_exported_jwk_rebuilds_the_same_public_key(self):
        key_path = self.key_file()
        out_path = self.base / "jwk.json"
        self.assertEqual(
            self.run_cli(["export-jwk", "--key", str(key_path), "--out", str(out_path)]), 0)
        jwk = json.loads(out_path.read_text(encoding="ascii"))
        self.assertEqual(jwk["kty"], "EC")
        self.assertEqual(jwk["crv"], "P-256")
        # 把 JWK 拼回 CNG 的 ECCPRIVATEBLOB，公钥应当和原钥匙一模一样
        def raw(name: str) -> bytes:
            text = jwk[name]
            return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        blob = (b"\x45\x43\x53\x32" + (32).to_bytes(4, "little")
                + raw("x") + raw("y") + raw("d"))
        rebuilt = signing.public_hex_from_private(base64.b64encode(blob).decode("ascii"))
        self.assertEqual(rebuilt, self.public_hex)

    def test_it_prints_only_the_jwk_on_stdout(self):
        key_path = self.key_file()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            self.signer.main(["export-jwk", "--key", str(key_path)])
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "stdout 上只能有一行 JWK，否则管道喂不进 secret")
        self.assertEqual(json.loads(lines[0])["crv"], "P-256")


@needs_cng
class RemoteSignTests(_SignerTestCase):
    def test_a_remote_signature_lands_in_a_verifiable_manifest(self):
        service = _SignService(self.private_b64)
        self.addCleanup(service.close)
        self.assertEqual(self.run_cli(self.sign_args(service.url)), 0)
        manifest = (self.out / signing.MANIFEST_NAME).read_bytes()
        signature = (self.out / signing.SIGNATURE_NAME).read_bytes()
        self.assertTrue(signing.verify_manifest(manifest, signature, self.public_hex))
        self.assertEqual(service.seen, [manifest],
                         "发给服务端、以及验签用的，必须是同一段清单原始字节")
        parsed = signing.parse_manifest(manifest)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["version"], "1.101.0")
        self.assertEqual(parsed["notes"], "换版了")

    def test_a_signature_that_does_not_verify_never_touches_the_disk(self):
        service = _SignService(self.private_b64, tamper=True)
        self.addCleanup(service.close)
        message = self.run_cli_expecting_failure(self.sign_args(service.url))
        self.assertIn("验不过", message)
        self.assertFalse((self.out / signing.SIGNATURE_NAME).exists(),
                         "验不过的签名绝不能落盘，更不能上传")
        self.assertFalse((self.out / signing.MANIFEST_NAME).exists())

    def test_a_service_with_another_key_is_refused(self):
        other_public, _other_private = signing.generate_keypair()
        service = _SignService(self.private_b64, public_key=other_public)
        self.addCleanup(service.close)
        message = self.run_cli_expecting_failure(self.sign_args(service.url))
        self.assertIn("不是客户端钉的那把钥匙", message)

    def test_without_a_token_the_service_is_not_even_called(self):
        service = _SignService(self.private_b64)
        self.addCleanup(service.close)
        with mock.patch.dict(os.environ, {"RELEASE_SIGN_TOKEN": ""}):
            message = self.run_cli_expecting_failure(self.sign_args(service.url, token=""))
        self.assertIn("token", message)
        self.assertEqual(service.seen, [])

    def test_the_env_var_can_carry_the_token(self):
        service = _SignService(self.private_b64)
        self.addCleanup(service.close)
        with mock.patch.dict(os.environ, {"RELEASE_SIGN_TOKEN": TOKEN}):
            self.assertEqual(self.run_cli(self.sign_args(service.url, token="")), 0)
        self.assertEqual(len(service.seen), 1)

    def test_a_rejected_token_says_so(self):
        service = _SignService(self.private_b64, token="the-right-one")
        self.addCleanup(service.close)
        message = self.run_cli_expecting_failure(self.sign_args(service.url))
        self.assertIn("401", message)

    def test_local_signing_still_works_with_a_key_file(self):
        key_path = self.key_file()
        self.assertEqual(self.run_cli(
            ["sign", str(self.zip), "--key", str(key_path), "--out", str(self.out)]), 0)
        manifest = (self.out / signing.MANIFEST_NAME).read_bytes()
        signature = (self.out / signing.SIGNATURE_NAME).read_bytes()
        self.assertTrue(signing.verify_manifest(manifest, signature, self.public_hex))


if __name__ == "__main__":
    unittest.main()
