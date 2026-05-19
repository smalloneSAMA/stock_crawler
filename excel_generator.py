#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Excel 生成器 —— 用 Python 内置模块原生生成 .xlsx 文件

为什么不用 openpyxl？
  xlsx 本质上是 ZIP 压缩包，内部为 XML 文件。
  本模块直接构造这些 XML 并打包为 .xlsx，无需安装任何第三方库。

字段说明：
  - 前12列为 K 线基础数据
  - 后9列为财务指标（已由 financial_fetcher 逐行填充）
"""

import os
import zipfile
from datetime import datetime
from typing import Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape


class ExcelGenerateError(Exception):
    """Excel 生成异常"""
    pass


# ── 列定义：header = 列名，width = Excel 列宽 ──
COLUMN_DEFS = [
    # === K线基础数据（前12列） ===
    {"header": "日期",          "width": 14},
    {"header": "开盘价",        "width": 12},
    {"header": "最高价",        "width": 12},
    {"header": "最低价",        "width": 12},
    {"header": "收盘价",        "width": 12},
    {"header": "成交量(万手)",   "width": 14},
    {"header": "成交额（亿）",   "width": 14},
    {"header": "涨跌幅%",       "width": 10},
    {"header": "振幅%",         "width": 10},
    {"header": "日内波动区间%",  "width": 14},
    {"header": "上涨幅度%",     "width": 14},
    {"header": "下跌幅度%",     "width": 14},
    # === 财务指标（后9列） ===
    {"header": "当前市值(亿)",  "width": 14},
    {"header": "市盈率TTM",     "width": 12},
    {"header": "股息率TTM",     "width": 10},
    {"header": "市净率",        "width": 10},
    {"header": "市销率",        "width": 10},
    {"header": "净资产收益率%",  "width": 14},
    {"header": "每股收益",      "width": 12},
    {"header": "每股股息TTM",   "width": 12},
    {"header": "分红率",       "width": 10},
]


def _fmt_cell_value(value) -> str:
    """
    将 Python 值转为 Excel XML 单元格内容。
    返回 (type_attr, value_text) 元组。
    """
    if value is None or value == "":
        return ('', '')

    if isinstance(value, str):
        # 字符串 → 内联字符串格式
        return ('t="inlineStr"', f"<is><t>{xml_escape(value)}</t></is>")

    if isinstance(value, (int, float)):
        return ('', f'{value}')

    return ('', str(value))


def generate_excel(
    stock_name: str,
    stock_code: str,
    start_date: str,
    end_date: str,
    data: List[Dict],
    output_path: str,
) -> str:
    """
    生成格式化 Excel 文件（原生 XML 方式）

    财务指标字段已由 enrich_kline_with_financials 嵌入到每行数据中，
    因此所有列都从每行的 record 中直接读取。

    Args:
        stock_name:  股票名称（用于标题行）
        stock_code:  股票代码（用于标题行）
        start_date:  起始日期
        end_date:    截止日期
        data:        含财务字段的K线数据列表（最新在前）
        output_path: 输出文件路径

    Returns:
        生成的文件绝对路径
    """
    num_cols = len(COLUMN_DEFS)
    col_letters = [_col_letter(i) for i in range(1, num_cols + 1)]

    # ── 准备 sheet 数据行 XML ──
    sheet_rows_xml = []

    # 第 1 行：合并标题行
    start_display = start_date if start_date else "最早"
    end_display = end_date if end_date else "最新"
    date_range = f"({start_display} ~ {end_display})"
    title_text = f"{stock_name} ({stock_code}) 日K线数据  {date_range}"

    sheet_rows_xml.append(
        f'<row r="1" spans="1:{num_cols}" s="1" customFormat="1" ht="36" customHeight="1">'
        f'<c r="A1" s="1" t="inlineStr"><is><t>{xml_escape(title_text)}</t></is></c>'
        + ''.join(f'<c r="{col_letters[i]}1" s="1"/>' for i in range(1, num_cols))
        + '</row>'
    )

    # 第 2 行：表头
    header_cells = ''
    for i, col_def in enumerate(COLUMN_DEFS):
        col_letter = col_letters[i]
        header_cells += (
            f'<c r="{col_letter}2" s="2" t="inlineStr">'
            f'<is><t>{xml_escape(col_def["header"])}</t></is>'
            f'</c>'
        )
    sheet_rows_xml.append(
        f'<row r="2" spans="1:{num_cols}" s="2" ht="24" customHeight="1">{header_cells}</row>'
    )

    # 第 3+ 行：数据（所有字段都已嵌入到每行 record 中）
    for row_idx, record in enumerate(data):
        row_num = row_idx + 3
        cells = ''

        for col_idx, col_def in enumerate(COLUMN_DEFS):
            col_letter = col_letters[col_idx]
            header = col_def["header"]
            raw_val = record.get(header)

            if header == "日期" and raw_val:
                # 日期用内联字符串，显示为文本
                cells += (
                    f'<c r="{col_letter}{row_num}" s="4" t="inlineStr">'
                    f'<is><t>{xml_escape(str(raw_val))}</t></is>'
                    f'</c>'
                )
            elif isinstance(raw_val, (int, float)):
                cells += f'<c r="{col_letter}{row_num}" s="4"><v>{raw_val}</v></c>'
            else:
                val_str = str(raw_val) if raw_val is not None else ''
                cells += (
                    f'<c r="{col_letter}{row_num}" s="4" t="inlineStr">'
                    f'<is><t>{xml_escape(val_str)}</t></is>'
                    f'</c>'
                )

        sheet_rows_xml.append(
            f'<row r="{row_num}" spans="1:{num_cols}" s="3">{cells}</row>'
        )

    # ── 合并单元格信息（标题行跨列）──
    merge_cell = f'A1:{col_letters[-1]}1'

    # ── 构建 sheet.xml ──
    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetPr>
    <outlinePr summaryBelow="1"/>
  </sheetPr>
  <dimension ref="A1:{col_letters[-1]}{len(data)+2}"/>
  <sheetViews>
    <sheetView tabSelected="1" workbookViewId="0">
      <pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>
      <selection pane="bottomLeft"/>
    </sheetView>
  </sheetViews>
  <sheetFormatPr defaultRowHeight="20"/>
  <cols>
{chr(10).join(f'    <col min="{i+1}" max="{i+1}" width="{col_def["width"]}" customWidth="1"/>' for i, col_def in enumerate(COLUMN_DEFS))}
  </cols>
  <sheetData>
{chr(10).join(sheet_rows_xml)}
  </sheetData>
  <mergeCells count="1">
    <mergeCell ref="{merge_cell}"/>
  </mergeCells>
</worksheet>'''

    # ── 样式 XML（字体、颜色、边框、对齐）──
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="11"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="14"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF4472C4"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9E2F3"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color auto="1"/></left>
      <right style="thin"><color auto="1"/></right>
      <top style="thin"><color auto="1"/></top>
      <bottom style="thin"><color auto="1"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center"/>
    </xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center"/>
    </xf>
  </cellXfs>
  <numFmts count="1">
    <numFmt numFmtId="164" formatCode="0.00"/>
  </numFmts>
</styleSheet>'''

    # ── 工作簿 XML ──
    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="K线数据" sheetId="1" r:id="rId1"/>
  </sheets>
  <calcPr calcId="152511"/>
</workbook>'''

    # ── 工作簿关系 XML ──
    workbook_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>'''

    # ── [Content_Types].xml ──
    content_types_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>'''

    # ── _rels/.rels ──
    root_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    # ── sharedStrings.xml（空，因为使用了 inlineStr）──
    shared_strings_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="0" uniqueCount="0"/>'''

    # ── 打包为 ZIP (.xlsx) ──
    try:
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', content_types_xml.encode('utf-8'))
            zf.writestr('_rels/.rels', root_rels_xml.encode('utf-8'))
            zf.writestr('xl/workbook.xml', workbook_xml.encode('utf-8'))
            zf.writestr('xl/_rels/workbook.xml.rels', workbook_rels_xml.encode('utf-8'))
            zf.writestr('xl/worksheets/sheet1.xml', sheet_xml.encode('utf-8'))
            zf.writestr('xl/styles.xml', styles_xml.encode('utf-8'))
            zf.writestr('xl/sharedStrings.xml', shared_strings_xml.encode('utf-8'))
    except Exception as e:
        raise ExcelGenerateError(f"写入 Excel 文件失败: {e}")

    return os.path.abspath(output_path)


def _col_letter(index: int) -> str:
    """将列索引（1-based）转为 Excel 列字母 A, B, C, ... Z, AA, AB..."""
    letter = ''
    while index > 0:
        index -= 1
        letter = chr(65 + (index % 26)) + letter
        index //= 26
    return letter
