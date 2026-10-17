"""发版签名工具：生成离线私钥、给安装包签一份更新清单。

这个脚本**只在发版机器上跑**，不进安装包（``deploy/`` 不在打包内容里）。
它用 Windows 自带的 CNG 签 ECDSA P-256，和客户端 :mod:`wechat_ai_assistant.update_signature`
的验签是同一套算法。

    :: 第一次发签名版：生成一对密钥（私钥写到仓库外面）
    python deploy/sign_release.py new-key

    :: 把打印出来的公钥粘进 src/wechat_ai_assistant/update_signature.py 的 PUBLISHER_PUBKEY

    :: 每个版本：签安装包，产出 latest.json + latest.json.sig
    python deploy/sign_release.py sign 微信AI智能助理-win64-1.10.0.zip --notes-file notes.md

    :: 上传前自检（用客户端那把钉死的公钥验一遍）
    python deploy/sign_release.py verify latest.json --sig latest.json.sig

完整发版流程见 ``docs/release.md``。

私钥默认落在 ``%USERPROFILE%\\.wechat-ai-assistant\\publisher-key.json``：**不要提交、
不要放进安装包**。丢了就只能用旧私钥签一个"带新公钥"的版本让客户端换过来，
所以务必另存一份离线备份。
"""
from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant import update_signature as signing  # noqa: E402
from wechat_ai_assistant import updater  # noqa: E402

DEFAULT_KEY_PATH = Path.home() / ".wechat-ai-assistant" / "publisher-key.json"
KEY_FILE_VERSION = 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_from_name(name: str) -> str:
    """从 ``微信AI智能助理-win64-1.10.0.zip`` 里抠出版本号。"""
    stem = Path(name).name
    prefix = updater.ASSET_PREFIX
    if stem.startswith(prefix):
        stem = stem[len(prefix):]
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    return stem.strip().lstrip("vV")


def _source_version() -> str:
    from wechat_ai_assistant.version import __version__
    return str(__version__ or "").strip()


def _load_key(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"找不到私钥文件：{path}\n先跑一次 python deploy/sign_release.py new-key")
    except Exception as exc:
        raise SystemExit(f"私钥文件读不出来（{path}）：{exc}")
    if not isinstance(data, dict) or not data.get("private_key"):
        raise SystemExit(f"私钥文件内容不对：{path}")
    return data


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
    except ValueError:
        return False
    return True


def cmd_new_key(args: argparse.Namespace) -> int:
    path = Path(args.key).expanduser()
    if path.exists() and not args.force:
        raise SystemExit(f"{path} 已经存在。要重新生成请加 --force（旧私钥立即作废！）")
    if _inside_repo(path):
        raise SystemExit(
            f"拒绝把私钥写进仓库：{path}\n"
            "私钥进了仓库就等于公开（历史里删不干净）。换到仓库外的目录，"
            "默认位置是 %USERPROFILE%\\.wechat-ai-assistant\\publisher-key.json")
    public_hex, private_b64 = signing.generate_keypair()
    derived = signing.public_hex_from_private(private_b64)
    if derived != public_hex:
        raise SystemExit("生成出来的公私钥对不上，中止（不该发生）")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": KEY_FILE_VERSION,
        "created_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds"),
        "algorithm": "ECDSA-P256-SHA256 (Windows CNG)",
        "public_key_hex": public_hex,
        "private_key": private_b64,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"私钥已写入：{path}")
    print()
    print("把下面这行粘进 src/wechat_ai_assistant/update_signature.py（替换 PUBLISHER_PUBKEY）：")
    print()
    print(f'PUBLISHER_PUBKEY = "{public_hex}"')
    print()
    print("提醒：")
    print("· 私钥只在这台机器上，另存一份离线备份（U 盘/密码管理器），丢了就要走换密钥流程；")
    print("· 换好公钥后记得重新打包、发版，否则包里钉的还是旧的（或空的）那把。")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.key).expanduser()
    pinned = signing.pinned_key_hex()
    print(f"客户端里钉死的公钥：{pinned or '（空：还没做签名发版）'}")
    print(f"客户端能不能验签：{'能' if signing.available() else '不能（没有 bcrypt.dll）'}")
    print(f"私钥文件：{path}")
    if not path.exists():
        print("  状态：文件不存在")
        return 1
    data = _load_key(path)
    derived = signing.public_hex_from_private(str(data.get("private_key") or ""))
    print(f"  对应公钥：{derived}")
    print(f"  和钉死的公钥是否一致：{'一致' if derived == pinned else '不一致！'}")
    if _inside_repo(path):
        print("  警告：私钥文件在仓库目录里，非常危险，请立刻挪走并作废重签。")
    return 0 if derived == pinned else 1


def cmd_sign(args: argparse.Namespace) -> int:
    zip_path = Path(args.zip).expanduser()
    if not zip_path.is_file():
        raise SystemExit(f"找不到安装包：{zip_path}")
    version = (args.version or _version_from_name(zip_path.name) or _source_version()).lstrip("vV")
    if not updater.parse_version(version):
        raise SystemExit(f"版本号解析不出来：{version!r}（用 --version 指定）")
    key_path = Path(args.key).expanduser()
    data = _load_key(key_path)
    key_hex = signing.public_hex_from_private(str(data.get("private_key") or ""))
    pinned = signing.pinned_key_hex()
    if pinned and key_hex != pinned:
        raise SystemExit(
            "这把私钥和客户端里钉死的公钥对不上。\n"
            "要么你拿错了私钥，要么公钥还没更新 —— 先跑 show 看一眼。")
    notes = args.notes
    if args.notes_file:
        notes = Path(args.notes_file).expanduser().read_text(encoding="utf-8").strip()
    digest = _sha256_file(zip_path)
    name = zip_path.name
    url = args.url or (
        f"https://github.com/{updater.DEFAULT_REPO}/releases/download/v{version}/"
        f"{quote(name, safe='')}")
    released_at = args.released_at or _datetime.datetime.now(
        _datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest = signing.canonical_manifest(version, url, digest, notes, released_at)
    signature = signing.sign_bytes(manifest, str(data.get("private_key") or ""))
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / signing.MANIFEST_NAME
    signature_path = out_dir / signing.SIGNATURE_NAME
    manifest_path.write_bytes(manifest)
    signature_path.write_text(signing.signature_text(signature) + "\n", encoding="ascii")
    if not signing.verify_manifest(manifest, signature, key_hex):
        raise SystemExit("刚签出来的东西自己都验不过，中止（不该发生）")
    print(f"安装包：{zip_path.name}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）")
    print(f"版本：{version}")
    print(f"sha256：{digest}")
    print(f"下载地址：{url}")
    print(f"清单：{manifest_path}")
    print(f"签名：{signature_path}")
    print()
    print("上传到 release 的资源（四个都要）：")
    print(f"  1. {name}")
    print(f"  2. {name}.sha256        <- 过渡期保留：老客户端只认这个")
    print(f"  3. {signing.MANIFEST_NAME}")
    print(f"  4. {signing.SIGNATURE_NAME}")
    print()
    print(f"发版前自检：python deploy/sign_release.py verify \"{manifest_path}\" "
          f"--sig \"{signature_path}\"")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser()
    signature_path = Path(args.sig).expanduser()
    try:
        manifest = manifest_path.read_bytes()
        signature = signature_path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"读文件失败：{exc}")
    parsed = signing.parse_manifest(manifest)
    if parsed is None:
        raise SystemExit("清单内容不合法（缺字段/哈希不对/地址不是 http(s)）")
    ok = signing.verify_manifest(manifest, signature)
    print(f"版本：{parsed['version']}")
    print(f"sha256：{parsed['sha256']}")
    print(f"下载地址：{parsed['zip_url']}")
    print(f"签名：{'通过' if ok else '不通过'}")
    if args.zip:
        zip_path = Path(args.zip).expanduser()
        actual = _sha256_file(zip_path)
        match = actual == parsed["sha256"]
        print(f"安装包哈希：{'一致' if match else '不一致'}（{zip_path.name}）")
        return 0 if (ok and match) else 1
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="微信AI智能助理发版签名工具（Windows CNG / ECDSA P-256）")
    sub = parser.add_subparsers(dest="command", required=True)

    new_key = sub.add_parser("new-key", help="生成一对离线密钥（私钥写在仓库外）")
    new_key.add_argument("--key", default=str(DEFAULT_KEY_PATH), help="私钥文件路径")
    new_key.add_argument("--force", action="store_true", help="覆盖已有私钥（旧私钥作废）")
    new_key.set_defaults(func=cmd_new_key)

    show = sub.add_parser("show", help="看客户端钉的公钥和手上私钥是否一致")
    show.add_argument("--key", default=str(DEFAULT_KEY_PATH), help="私钥文件路径")
    show.set_defaults(func=cmd_show)

    sign = sub.add_parser("sign", help="签安装包，产出 latest.json + latest.json.sig")
    sign.add_argument("zip", help="安装包路径")
    sign.add_argument("--version", default="", help="版本号（默认从文件名/version.py 推）")
    sign.add_argument("--url", default="", help="下载地址（默认按 GitHub release 拼）")
    sign.add_argument("--notes", default="", help="更新说明（一行）")
    sign.add_argument("--notes-file", default="", help="更新说明文件")
    sign.add_argument("--released-at", default="", help="发布时间（默认现在，UTC）")
    sign.add_argument("--key", default=str(DEFAULT_KEY_PATH), help="私钥文件路径")
    sign.add_argument("--out", default="dist", help="清单输出目录")
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser("verify", help="用客户端钉死的公钥验一遍清单")
    verify.add_argument("manifest", help="latest.json 路径")
    verify.add_argument("--sig", default=str(Path("dist") / signing.SIGNATURE_NAME),
                        help="签名文件路径")
    verify.add_argument("--zip", default="", help="顺便核对安装包哈希")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if not signing.available() and args.command != "show":
        raise SystemExit("这台机器没有可用的 Windows 加密组件（bcrypt.dll），没法签名")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
