"""生成商品/客户名单的 **xlsx 模板**，并把上传回来的行整理成 CSV。

## 为什么不给"列名说明"

商户大多不懂电脑。"请按「编码、名称、单位、价格、别名」建表头"这句话，对他们是
一张考卷 —— 少一个字、多一个空格就导不进去，而报错还看不出是表头的问题。

所以改成给他一个**已经填好表头和一行示例**的文件：下载 → 填 → 传回来。
模板里那行示例在导入时会被自动跳过（首列以「示例」开头），商户不用删它。

模板只用标准库生成（和读那边同一个理由：不加依赖）。
"""
from __future__ import annotations

import csv
import io
import zipfile
from typing import Sequence
from xml.sax.saxutils import escape

#: 两种名单的表头。**和导入器认的名字一致**（见 orders.import_*_csv 的说明）。
PRODUCT_HEADERS = ["编码", "名称", "单位", "价格", "别名"]
CUSTOMER_HEADERS = ["名字", "群", "地址", "偏好", "别名"]

#: 模板里那行示例：首列以它开头，导入时跳过。
EXAMPLE_MARK = "示例"


def template_rows(kind: str) -> list[list[str]]:
    """模板内容：表头 + 一行示例。``kind`` 取 products / customers。"""
    if kind == "products":
        return [PRODUCT_HEADERS,
                [f"{EXAMPLE_MARK}：APL", "苹果", "箱", "2600", "苹果,红富士"]]
    if kind == "customers":
        return [CUSTOMER_HEADERS,
                [f"{EXAMPLE_MARK}：张姐", "鲜果批发群", "朝阳区XX路", "只要脆的", "张姐,姐"]]
    raise ValueError(f"不认识的名单类型：{kind}")


def template_filename(kind: str) -> str:
    return "商品名单模板.xlsx" if kind == "products" else "客户名单模板.xlsx"


def is_example_row(row: Sequence[str]) -> bool:
    """首列以「示例」开头就当示例行跳过 —— 商户不删它也能直接导。"""
    for cell in row:
        text = str(cell or "").strip()
        if not text:
            continue
        return text.startswith(EXAMPLE_MARK)
    return False


def rows_from_csv_text(text: str) -> list[list[str]]:
    """CSV 文本 → 行。空行丢掉。"""
    reader = csv.reader(io.StringIO(str(text or "").lstrip("\ufeff")))
    return [list(row) for row in reader if any(str(cell).strip() for cell in row)]


#: 转换时插入的「行号」列。导入器报错时用它，商户才能对着 Excel 找到那一行。
ROW_COLUMN = "行号"


def clean_rows(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    """去掉示例行和空行；其余原样。"""
    return [list(row) for row in rows
            if any(str(cell).strip() for cell in row) and not is_example_row(row)]


def numbered_rows(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    """保留每行在**工作表里的行号**，并在最前面加一列。

    表头算第 1 行；示例行和空行照样丢掉，但留下来的行带着自己的原始行号 ——
    否则删掉一行示例，后面所有行的行号都会往前挪，商户照着报错去 Excel 里找
    会找到**另一行**。
    """
    out: list[list[str]] = []
    for index, row in enumerate(rows, start=1):
        cells = list(row)
        if not any(str(cell).strip() for cell in cells) or is_example_row(cells):
            continue
        # 表头那一行的第一格写列名，其它行写自己的行号。
        out.append([ROW_COLUMN if index == 1 else str(index)] + cells)
    return out


def template_notes(kind: str) -> list[list[str]]:
    """模板第二张表「填写说明」：每列填什么、价格单位、示例行怎么处理。

    放在表里而不是只写在界面上 —— 商户填表时看的是这张表，不是软件窗口。
    """
    if kind == "products":
        return [
            ["商品名单怎么填"],
            ["列", "填什么"],
            ["编码", "商品编号，随便你自己定，例：APL。留空会自动生成"],
            ["名称", "商品名，例：苹果（必填）"],
            ["单位", "箱 / 斤 / 个，例：箱"],
            ["价格", "单价，**单位是元**：可以写 2600 或 26.50；不确定就留空"],
            ["别名", "客户在群里可能怎么叫它，多个用逗号隔开，例：苹果,红富士"],
            ["", ""],
            ["注意", "整行第一个单元格以「示例」开头的会被自动跳过，可以不删"],
            ["注意", "同一个编码重复上传只会更新价格和名称，不会变两条"],
        ]
    if kind == "customers":
        return [
            ["客户名单怎么填"],
            ["列", "填什么"],
            ["名字", "你平时怎么称呼这位客户，例：张姐（必填）"],
            ["群", "他在哪个群，留空表示所有群都算"],
            ["地址", "送货地址，例：朝阳区XX路"],
            ["偏好", "口味/习惯，例：只要脆的"],
            ["别名", "群里还可能怎么叫他，多个用逗号隔开，例：张姐,张大姐"],
            ["", ""],
            ["注意", "整行第一个单元格以「示例」开头的会被自动跳过，可以不删"],
            ["注意", "按「名字」认人：同一个名字重复上传只会更新地址和偏好"],
        ]
    raise ValueError(f"不认识的名单类型：{kind}")


def _sheet_xml(rows: Sequence[Sequence[str]]) -> str:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    body = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            column = _column_name(c)
            text = escape(str(value if value is not None else ""))
            cells.append(
                f'<c r="{column}{r}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        body.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{ns}"><sheetData>' + "".join(body) + "</sheetData></worksheet>")


def build_xlsx(rows: Sequence[Sequence[str]],
               notes: Sequence[Sequence[str]] | None = None) -> bytes:
    """把行写成一个最小可用的 xlsx（inline string，不用共享字符串表）。

    ``notes`` 给了就再加一张「填写说明」表。**数据永远在第一张** —— 读取端只读
    第一张，说明表放后面才不会把说明当成名单导进去。
    """
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    sheets = [_sheet_xml(rows)] + ([_sheet_xml(notes)] if notes else [])

    workbook_sheets = '<sheet name="名单" sheetId="1" r:id="rId1"/>'
    rels = ('<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>')
    overrides = ('<Override PartName="/xl/worksheets/sheet1.xml" ContentType='
                 '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    if len(sheets) > 1:
        workbook_sheets += '<sheet name="填写说明" sheetId="2" r:id="rId2"/>'
        rels += ('<Relationship Id="rId2" Target="worksheets/sheet2.xml" '
                 'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>')
        overrides += ('<Override PartName="/xl/worksheets/sheet2.xml" ContentType='
                      '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    workbook = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<workbook xmlns="{ns}" xmlns:r="{rel}">'
                f'<sheets>{workbook_sheets}</sheets></workbook>')
    rels_xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<Relationships xmlns="{pkg}">{rels}</Relationships>')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     f'{overrides}'
                     '</Types>')

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{pkg}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="xl/workbook.xml"/></Relationships>'))
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        for index, xml in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", xml)
    return buffer.getvalue()


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


__all__ = ["CUSTOMER_HEADERS", "EXAMPLE_MARK", "PRODUCT_HEADERS", "build_xlsx",
           "clean_rows", "is_example_row", "rows_from_csv_text", "template_filename",
           "template_notes", "template_rows"]
