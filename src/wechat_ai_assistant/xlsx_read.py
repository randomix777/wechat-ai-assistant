"""读 Excel（.xlsx）前几列，转成和 CSV 一样的行。

## 为什么自己写

商户给的商品/客户名单就是 Excel 表。仓库的规矩是**尽量不加依赖**（打包漏一个
模块就是整片功能失效，前面已经栽过一次）。`.xlsx` 本质是 zip 里的一堆 XML，
读第一张表的单元格用标准库就够，不需要 openpyxl。

## 能读什么、不能读什么

* 读：第一张工作表；共享字符串、内联字符串、数字、公式结果（缓存值）；
  空单元格按位置留空，所以列不会串位。
* 不算：单元格格式、合并单元格、日期格式。日期会显示成 Excel 的序列号 ——
  名单里正常没有日期；真遇到会照原样变成数字，不猜。
* 只认 ``.xlsx``。老的二进制 ``.xls`` 读不了，要商户另存为 ``.xlsx``。
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

#: 单元格引用里的列字母，如 ``C12`` → 3。
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def is_xlsx(data: bytes) -> bool:
    """看着像不像 xlsx：zip 头 + 里面有 xl/workbook.xml。"""
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return "xl/workbook.xml" in archive.namelist()
    except Exception:
        return False


def _column_index(ref: str) -> int:
    match = _CELL_REF.match(ref or "")
    if not match:
        return -1
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _text_of(node: ET.Element) -> str:
    """一个 ``<si>`` / ``<is>`` 里的文字：把所有 ``<t>`` 拼起来（富文本会有多段）。"""
    return "".join(part.text or "" for part in node.iter(f"{_NS}t"))


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    return [_text_of(item) for item in root.findall(f"{_NS}si")]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    """按 workbook.xml 的顺序取第一张表；读不到就退回 worksheets/sheet1.xml。"""
    names = set(archive.namelist())
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = workbook.find(f"{_NS}sheets")
        if sheets is not None:
            first = sheets.find(f"{_NS}sheet")
            if first is not None:
                rel_id = first.get(f"{_REL_NS}id") or ""
                rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                for rel in rels.findall(f"{_PKG_REL_NS}Relationship"):
                    if rel.get("Id") == rel_id:
                        target = str(rel.get("Target") or "").lstrip("/")
                        path = target if target.startswith("xl/") else f"xl/{target}"
                        if path in names:
                            return path
    except Exception:
        pass
    for candidate in ("xl/worksheets/sheet1.xml",):
        if candidate in names:
            return candidate
    sheets = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
    return sheets[0] if sheets else ""


def _number(value: str) -> str:
    """``1.0`` → ``1``；其它原样。名单里的数量/价格不该带无意义的 .0。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return text


def rows_from_bytes(data: bytes) -> list[list[str]]:
    """读第一张工作表的全部行（跳过完全空白的行）。"""
    rows: list[list[str]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared = _shared_strings(archive)
        path = _first_sheet_path(archive)
        if not path:
            return []
        sheet = ET.fromstring(archive.read(path))
        sheet_data = sheet.find(f"{_NS}sheetData")
        if sheet_data is None:
            return rows
        for row in sheet_data.findall(f"{_NS}row"):
            cells: dict[int, str] = {}
            for cell in row.findall(f"{_NS}c"):
                index = _column_index(str(cell.get("r") or ""))
                if index < 0:
                    index = len(cells)
                kind = str(cell.get("t") or "")
                if kind == "s":
                    raw = cell.findtext(f"{_NS}v") or ""
                    try:
                        cells[index] = shared[int(raw)]
                    except (ValueError, IndexError):
                        cells[index] = ""
                elif kind == "inlineStr":
                    node = cell.find(f"{_NS}is")
                    cells[index] = _text_of(node) if node is not None else ""
                elif kind == "str":
                    cells[index] = str(cell.findtext(f"{_NS}v") or "")
                else:
                    cells[index] = _number(str(cell.findtext(f"{_NS}v") or ""))
            if not any(value.strip() for value in cells.values()):
                continue
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    return rows


def to_csv_text(rows: list[list[str]]) -> str:
    """把行转成 CSV 文本，直接喂给现有的导入逻辑（它只认 CSV）。"""
    import csv

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


__all__ = ["is_xlsx", "rows_from_bytes", "to_csv_text"]
