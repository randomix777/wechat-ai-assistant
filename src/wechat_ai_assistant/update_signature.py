"""更新包的离线签名与验签：把公钥钉进程序，更新只认签过的清单。

## 为什么需要它

以前的更新包和它的 ``.sha256`` 来自**同一个地方**（GitHub release）。哈希只能
证明"下载没损坏"，证明不了"这是发布方发的"—— 谁能改 release，谁就能同时换掉
包和哈希，然后让所有商户装上他自己那份程序。这里把**公钥钉进客户端**：发布时
用离线私钥签一份清单（``latest.json``），客户端只接受签过的清单，清单里写着
这一版该装哪个包、包的哈希是多少。于是"知道从哪下载"不再等于"能让你装上"。

## 用什么签

Windows 自带的 CNG（``bcrypt.dll``）ECDSA P-256 + SHA-256，没有第三方依赖 ——
这台机器上 ``cryptography``/``pynacl``/``ecdsa`` 一个都没装，也不该为了验个
签名给商户多塞几个轮子。验签走的全是 Windows 自带组件。

## 谁能用哪一半

* 客户端只调 :func:`verify_manifest`；
* :func:`generate_keypair` / :func:`sign_bytes` 只给发版工具（``deploy/sign_release.py``）
  和测试用 —— **私钥永远不进仓库、不进安装包**，离线保管（见 ``docs/release.md``）。

任何一步出错都返回 False / None，不抛异常：更新路径上永远不能因为加密组件
缺失或密钥写坏就把客户端带崩。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import struct
import sys
from ctypes import wintypes
from typing import Any

LOGGER = logging.getLogger(__name__)

#: 发布方公钥：P-256 公钥点的 ``X||Y``，128 个十六进制字符。这是**钉死的信任根**。
#:
#: 换私钥等于换发版人：只能用旧私钥签一个带新公钥的版本，让客户端自己换过来。
#: 空字符串（历史默认）表示还没钉，退回旧的"只比对哈希"老路 —— 只应该出现在
#: 还没做过签名发版的部署里。
PUBLISHER_PUBKEY = "6563d61e61390cf080500520606a865c12389b2eeb93b5b07df4c850eceb8fdd8af6be83d73acef56ee9b5e1df6a90ed3aa776ab6d938f8cf4fbeed40918e2ae"

#: 清单与签名在 release 里的文件名。
MANIFEST_NAME = "latest.json"
SIGNATURE_NAME = "latest.json.sig"

KEY_BYTES = 32
DIGEST_BYTES = 32
SIGNATURE_BYTES = 64
PUBKEY_BYTES = KEY_BYTES * 2
PUBKEY_HEX_LENGTH = PUBKEY_BYTES * 2

_ALGORITHM = "ECDSA_P256"
_PUBLIC_BLOB_TYPE = "ECCPUBLICBLOB"
_PRIVATE_BLOB_TYPE = "ECCPRIVATEBLOB"
_ECDSA_PUBLIC_MAGIC = 0x31534345  # "ECS1"：BCRYPT_ECDSA_PUBLIC_MAGIC
_ECDSA_PRIVATE_MAGIC = 0x32534345  # "ECS2"：BCRYPT_ECDSA_PRIVATE_MAGIC


def _load_cng() -> dict[str, Any] | None:
    """加载 Windows CNG 并声明函数签名。拿不到就返回 None（验签一律失败）。"""
    if sys.platform != "win32":
        return None
    try:
        library = ctypes.WinDLL("bcrypt.dll")
    except Exception as exc:  # pragma: no cover - 只在异常环境走到
        LOGGER.warning("Windows 加密组件不可用（bcrypt.dll）：%s", exc)
        return None
    handle = ctypes.c_void_p
    ulong = wintypes.ULONG
    pointer = ctypes.POINTER
    c_status = ctypes.c_long
    declarations = {
        "open": (library.BCryptOpenAlgorithmProvider, c_status,
                 [pointer(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong]),
        "close": (library.BCryptCloseAlgorithmProvider, c_status, [handle, ulong]),
        "import": (library.BCryptImportKeyPair, c_status,
                   [handle, handle, wintypes.LPCWSTR, pointer(handle), handle, ulong, ulong]),
        "destroy": (library.BCryptDestroyKey, c_status, [handle]),
        "verify": (library.BCryptVerifySignature, c_status,
                   [handle, handle, handle, ulong, handle, ulong, ulong]),
        "generate": (library.BCryptGenerateKeyPair, c_status,
                     [handle, pointer(handle), ulong, ulong]),
        "finalize": (library.BCryptFinalizeKeyPair, c_status, [handle, ulong]),
        "export": (library.BCryptExportKey, c_status,
                   [handle, handle, wintypes.LPCWSTR, handle, ulong, pointer(ulong), ulong]),
        "sign": (library.BCryptSignHash, c_status,
                 [handle, handle, handle, ulong, handle, ulong, pointer(ulong), ulong]),
    }
    functions: dict[str, Any] = {}
    for name, (function, restype, argtypes) in declarations.items():
        function.restype = restype
        function.argtypes = argtypes
        functions[name] = function
    return functions


_CNG = _load_cng()


def available() -> bool:
    """这台机器能不能签名/验签（Windows 上一般都能）。"""
    return _CNG is not None


def pinned_key_hex() -> str:
    """钉死的公钥（小写十六进制）。没钉就是空字符串。"""
    return str(PUBLISHER_PUBKEY or "").strip().lower()


def is_pinned() -> bool:
    """有没有钉公钥。钉了以后没签名的更新一律不装。"""
    return _is_public_key(pinned_key_hex())


def _is_public_key(text: str) -> bool:
    if len(text) != PUBKEY_HEX_LENGTH:
        return False
    try:
        bytes.fromhex(text)
    except ValueError:
        return False
    return True


def _buffer(data: bytes) -> tuple[Any, Any]:
    """给 CNG 用的输入缓冲：返回 (持有者, 指针)，两者都要活到调用结束。"""
    holder = ctypes.create_string_buffer(data, len(data))
    return holder, ctypes.cast(holder, ctypes.c_void_p)


def _public_blob(public_xy: bytes) -> bytes:
    return struct.pack("<II", _ECDSA_PUBLIC_MAGIC, KEY_BYTES) + public_xy


def _open_provider() -> Any:
    if _CNG is None:
        raise OSError("这台机器没有可用的 Windows 加密组件")
    provider = ctypes.c_void_p()
    status = _CNG["open"](ctypes.byref(provider), _ALGORITHM, None, 0)
    if status < 0:
        raise OSError(f"打开 ECDSA 算法失败：0x{status & 0xFFFFFFFF:08X}")
    return provider


def _close_provider(provider: Any) -> None:
    try:
        _CNG["close"](provider, 0)
    except Exception:  # pragma: no cover - 收尾不抛
        pass


def _import_key(provider: Any, blob: bytes, blob_type: str) -> Any:
    key = ctypes.c_void_p()
    holder, pointer = _buffer(blob)
    status = _CNG["import"](provider, None, blob_type, ctypes.byref(key), pointer, len(blob), 0)
    del holder  # 导入是同步拷贝，缓冲区到此为止
    if status < 0:
        raise OSError(f"导入密钥失败：0x{status & 0xFFFFFFFF:08X}")
    return key


def _export_key(key: Any, blob_type: str) -> bytes:
    needed = wintypes.ULONG()
    status = _CNG["export"](key, None, blob_type, None, 0, ctypes.byref(needed), 0)
    if status < 0:
        raise OSError(f"导出密钥失败：0x{status & 0xFFFFFFFF:08X}")
    buffer = ctypes.create_string_buffer(needed.value)
    status = _CNG["export"](
        key, None, blob_type, ctypes.cast(buffer, ctypes.c_void_p), needed.value,
        ctypes.byref(needed), 0)
    if status < 0:
        raise OSError(f"导出密钥失败：0x{status & 0xFFFFFFFF:08X}")
    return buffer.raw[:needed.value]


def _verify(public_xy: bytes, digest: bytes, signature: bytes) -> bool:
    """CNG 验签：签名是 64 字节的 r||s，摘要必须恰好 32 字节（SHA-256）。"""
    if len(public_xy) != PUBKEY_BYTES or len(digest) != DIGEST_BYTES:
        return False
    if len(signature) != SIGNATURE_BYTES:
        return False
    try:
        provider = _open_provider()
    except OSError as exc:
        LOGGER.warning("验签不可用：%s", exc)
        return False
    key = ctypes.c_void_p()
    try:
        key = _import_key(provider, _public_blob(public_xy), _PUBLIC_BLOB_TYPE)
        hash_holder, hash_pointer = _buffer(digest)
        signature_holder, signature_pointer = _buffer(signature)
        status = _CNG["verify"](
            key, None, hash_pointer, len(digest), signature_pointer, len(signature), 0)
        del hash_holder, signature_holder
        return status >= 0
    except Exception as exc:
        LOGGER.warning("验签失败：%s", exc)
        return False
    finally:
        if key:
            try:
                _CNG["destroy"](key)
            except Exception:  # pragma: no cover
                pass
        _close_provider(provider)


def _coerce_signature(signature: bytes) -> bytes | None:
    """签名文件的三种写法都认：原始 64 字节、base64 文本、128 位十六进制。"""
    if len(signature) == SIGNATURE_BYTES:
        return signature
    text = signature.decode("ascii", errors="ignore").strip()
    compact = "".join(text.split())
    if not compact:
        return None
    if len(compact) == SIGNATURE_BYTES * 2:
        try:
            raw = bytes.fromhex(compact)
        except ValueError:
            raw = b""
        if len(raw) == SIGNATURE_BYTES:
            return raw
    try:
        raw = base64.b64decode(compact, validate=True)
    except Exception:
        return None
    return raw if len(raw) == SIGNATURE_BYTES else None


def verify_manifest(manifest: bytes, signature: bytes,
                    public_key_hex: str = "") -> bool:
    """清单字节 + 签名 → 是不是这把公钥签的。任何异常都返回 False。"""
    key_hex = str(public_key_hex or "").strip().lower() or pinned_key_hex()
    if not manifest or not signature or not key_hex:
        return False
    if not _is_public_key(key_hex):
        LOGGER.warning("公钥位数不对（%d 个十六进制字符），验签失败", len(key_hex))
        return False
    raw = _coerce_signature(signature)
    if raw is None:
        return False
    return _verify(bytes.fromhex(key_hex), hashlib.sha256(manifest).digest(), raw)


def canonical_manifest(version: str, zip_url: str, sha256: str,
                       notes: str = "", released_at: str = "") -> bytes:
    """清单的规范字节：键排序、紧凑分隔符、中文不转义。

    签名签的是**字节**，所以发版时写进文件必须正是这里的这份字节；客户端验的
    也是下载到的原始字节（绝不重新序列化 —— 重新序列化出来的字节不一样，签名
    自然对不上）。
    """
    payload = {
        "version": str(version or "").strip().lstrip("vV"),
        "zip_url": str(zip_url or "").strip(),
        "sha256": str(sha256 or "").strip().lower(),
        "notes": str(notes or ""),
        "released_at": str(released_at or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def parse_manifest(raw: bytes) -> dict[str, Any] | None:
    """校验并解析清单。字段不全/哈希不是哈希/地址不是 http(s) 都算无效。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "").strip().lstrip("vV")
    digest = str(data.get("sha256") or "").strip().lower()
    url = str(data.get("zip_url") or "").strip()
    if not version or "." not in version:
        return None
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        return None
    if not url.lower().startswith(("http://", "https://")):
        return None
    return {
        "version": version,
        "sha256": digest,
        "zip_url": url,
        "notes": str(data.get("notes") or ""),
        "released_at": str(data.get("released_at") or ""),
    }


def generate_keypair() -> tuple[str, str]:
    """生成一对 P-256 密钥，返回 ``(公钥十六进制, 私钥 base64)``。

    只给发版工具和测试用。私钥是 CNG 可导出的 ``ECCPRIVATEBLOB``，换台机器
    也能接着签。
    """
    provider = _open_provider()
    key = ctypes.c_void_p()
    try:
        status = _CNG["generate"](provider, ctypes.byref(key), KEY_BYTES * 8, 0)
        if status < 0:
            raise OSError(f"生成密钥对失败：0x{status & 0xFFFFFFFF:08X}")
        status = _CNG["finalize"](key, 0)
        if status < 0:
            raise OSError(f"生成密钥对失败：0x{status & 0xFFFFFFFF:08X}")
        private_blob = _export_key(key, _PRIVATE_BLOB_TYPE)
        public_blob = _export_key(key, _PUBLIC_BLOB_TYPE)
    finally:
        if key:
            try:
                _CNG["destroy"](key)
            except Exception:  # pragma: no cover
                pass
        _close_provider(provider)
    offset = 8  # magic + cbKey
    public_xy = public_blob[offset:offset + PUBKEY_BYTES]
    if len(public_xy) != PUBKEY_BYTES or len(private_blob) < offset + PUBKEY_BYTES:
        raise OSError("密钥导出格式不是预期的那一种")
    return public_xy.hex(), base64.b64encode(private_blob).decode("ascii")


def public_hex_from_private(private_key_b64: str) -> str:
    """从私钥里取出公钥十六进制（登记/自检用，不解私钥以外的信息）。"""
    try:
        blob = base64.b64decode(str(private_key_b64 or "").strip())
    except Exception as exc:
        raise OSError(f"私钥不是合法的 base64：{exc}") from exc
    if len(blob) < 8 + PUBKEY_BYTES:
        raise OSError("私钥内容太短，不像是 CNG 的 ECCPRIVATEBLOB")
    magic = struct.unpack("<I", blob[:4])[0]
    if magic != _ECDSA_PRIVATE_MAGIC:
        raise OSError("私钥不是 CNG 的 P-256 私钥")
    return blob[8:8 + PUBKEY_BYTES].hex()


def sign_bytes(data: bytes, private_key_b64: str) -> bytes:
    """用离线私钥签 `data` 的 SHA-256，返回 64 字节的 r||s。"""
    try:
        blob = base64.b64decode(str(private_key_b64 or "").strip())
    except Exception as exc:
        raise OSError(f"私钥不是合法的 base64：{exc}") from exc
    provider = _open_provider()
    key = ctypes.c_void_p()
    try:
        key = _import_key(provider, blob, _PRIVATE_BLOB_TYPE)
        digest = hashlib.sha256(data).digest()
        hash_holder, hash_pointer = _buffer(digest)
        needed = wintypes.ULONG()
        status = _CNG["sign"](key, None, hash_pointer, len(digest), None, 0,
                              ctypes.byref(needed), 0)
        if status < 0:
            raise OSError(f"签名失败：0x{status & 0xFFFFFFFF:08X}")
        buffer = ctypes.create_string_buffer(needed.value)
        status = _CNG["sign"](
            key, None, hash_pointer, len(digest),
            ctypes.cast(buffer, ctypes.c_void_p), needed.value,
            ctypes.byref(needed), 0)
        del hash_holder
        if status < 0:
            raise OSError(f"签名失败：0x{status & 0xFFFFFFFF:08X}")
        return buffer.raw[:needed.value]
    finally:
        if key:
            try:
                _CNG["destroy"](key)
            except Exception:  # pragma: no cover
                pass
        _close_provider(provider)


def signature_text(signature: bytes) -> str:
    """签名写文件用的写法：一行 base64。"""
    return base64.b64encode(signature).decode("ascii")


__all__ = [
    "MANIFEST_NAME",
    "PUBKEY_HEX_LENGTH",
    "PUBLISHER_PUBKEY",
    "SIGNATURE_BYTES",
    "SIGNATURE_NAME",
    "available",
    "canonical_manifest",
    "generate_keypair",
    "is_pinned",
    "parse_manifest",
    "pinned_key_hex",
    "public_hex_from_private",
    "sign_bytes",
    "signature_text",
    "verify_manifest",
]
