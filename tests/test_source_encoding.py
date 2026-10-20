"""源码文件的编码卫生：不许有 BOM，也不许有解坏了的替换字符。

## 为什么单独钉这两条

两个都真的发生过，而且**都不是**"编译一下就报错"那种好抓的形态：

1. **BOM（U+FEFF）。** PowerShell 的 `Set-Content -Encoding utf8` 会**带头写 BOM**，
   用它改一次 Python 源文件就会中招。Python 自己导入带 BOM 的文件是**正常的**
   （解释器会剥掉），所以全套测试里只有那么两三个"读源码文本再解析"的测试会红，
   报错还是 `SyntaxError: invalid non-printable character U+FEFF` 指向一个
   看不见的字符 —— 排查成本远高于修它。

2. **替换字符（U+FFFD）。** 某次按错误编码解码再写回，把中文注释里的字
   换成了这个符号（一次性发现 18 处，其中一处还在**发给模型的字符串**里）。
   它同样不影响运行，只是把话变成乱码。

（这个文件自己也受这条检查管，所以正文里**不写**那个字符本身，只说码位 ——
写上去它就会把自己判成违规。）

两条都是"文本被工具改坏了"的痕迹，与业务无关，所以放在这里一次扫全仓。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

#: 要检查的目录与后缀。只扫源码/文档，不扫构建产物与虚拟环境。
SCAN_ROOTS = ("src", "tests", "examples", "packages", "deploy")
TEXT_SUFFIXES = (".py", ".md", ".txt", ".spec", ".bat", ".json")
SKIP_PARTS = {".venv", "build", "__pycache__", ".git", "node_modules"}

BOM = b"\xef\xbb\xbf"
REPLACEMENT = "\ufffd"


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_ROOTS:
        root = ROOT / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if SKIP_PARTS & set(path.parts) or ".egg-info" in str(path):
                continue
            files.append(path)
    return files


class BatchFileTests(unittest.TestCase):
    """``.bat`` 必须是 CRLF。

    cmd.exe 按字节读批处理，遇到 **LF-only** 的 UTF-8 文件时，多字节中文会把
    行边界读错 —— 注释行会被当成命令执行，屏幕上出现
    ``'...' is not recognized as an internal or external command``，而真正的
    命令可能压根没跑。实测：同一份内容换成 CRLF 就恢复正常。
    """

    def test_every_batch_file_uses_crlf(self):
        offenders: list[str] = []
        # 根目录也有 bat（启动、验收），不能只扫子目录 —— 只扫子目录的话，
        # 恰恰是**天天双击的那几个**漏掉了。
        directories = [ROOT] + [ROOT / name for name in SCAN_ROOTS
                                if (ROOT / name).is_dir()]
        for base in directories:
            for path in sorted(base.glob("*.bat")):
                raw = path.read_bytes()
                if not raw:
                    continue
                lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
                if lone_lf:
                    offenders.append(f"{path.relative_to(ROOT)}（{lone_lf} 个单独 LF）")
        self.assertEqual(
            offenders, [],
            "这些 .bat 是 LF 换行，cmd 会把中文注释当命令执行：\n  "
            + "\n  ".join(offenders) + "\n请另存为 CRLF。")


class SourceEncodingTests(unittest.TestCase):
    def test_no_source_file_starts_with_a_bom(self):
        """带 BOM 的源码会让"读文本再解析"的测试报一个看不见的字符。"""
        offenders = [str(path.relative_to(ROOT)) for path in _scan_files()
                     if path.read_bytes()[:3] == BOM]
        self.assertEqual(
            offenders, [],
            "这些文件带了 UTF-8 BOM，请重新保存为无 BOM（别用 "
            "PowerShell 的 Set-Content -Encoding utf8 改源码）：\n  "
            + "\n  ".join(offenders))

    def test_no_source_file_contains_a_replacement_character(self):
        """U+FFFD 是"按错编码解码过"的痕迹：中文会变成乱码方块。"""
        offenders: list[str] = []
        for path in _scan_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # 解不开 = 更严重的问题，但那是另一条（不是"解坏了"，是干脆不是UTF-8）。
                continue
            if REPLACEMENT in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders, [],
            "这些文件里有替换字符（U+FFFD），说明中文被写坏过：\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
