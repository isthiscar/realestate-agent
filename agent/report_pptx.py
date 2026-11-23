"""
시장·입지 보고서 PPT 렌더러 (IM 양식: A4 가로, 파란 그라데이션 헤더, JB Asset Management)
- 지도: 브이월드 배경지도 + PIL로 반경 원·기준 별·번호 마커·범례
- 표·차트: PowerPoint 기본 개체(편집 가능)
- PDF: Windows PowerPoint(COM) → LibreOffice 순
"""
from __future__ import annotations

import copy
import io
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION, XL_TICK_LABEL_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Cm, Emu, Pt

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = None

FONT = "맑은 고딕"
NAVY = RGBColor(0x0B, 0x1F, 0x4B)
BLUE = RGBColor(0x0A, 0x5C, 0xC0)
TXT = RGBColor(0x33, 0x33, 0x33)
GREY = RGBColor(0x80, 0x80, 0x80)
LINE = RGBColor(0xBF, 0xC5, 0xCF)
HEAD_FILL = RGBColor(0xE4, 0xEA, 0xF3)
BAR = RGBColor(0x9D, 0xC3, 0xE6)
BAR2 = RGBColor(0xBD, 0xD7, 0xEE)
GOLD = RGBColor(0xFF, 0xD9, 0x66)
GOLD_TXT = RGBColor(0x7F, 0x60, 0x00)
SLIDE_W, SLIDE_H = Cm(29.7), Cm(21.0)


# ---------------------------------------------------------------------------
# 텍스트 유틸
# ---------------------------------------------------------------------------
def _font(run, size: float, bold: bool = False, color: RGBColor = TXT, name: str = FONT) -> None:
    f = run.font
    f.size = Pt(size)
    f.bold = bold
    f.color.rgb = color
    f.name = name
    rpr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rpr.find(qn(tag))
        if el is None:
            el = etree.SubElement(rpr, qn(tag))
        el.set("typeface", name)


def _rich(paragraph, text: str, size: float, color: RGBColor = TXT, bold_color: RGBColor | None = None) -> None:
    """**굵게** 표시를 지원하는 문단."""
    parts = re.split(r"(\*\*.+?\*\*)", text)
    for p in parts:
        if not p:
            continue
        r = paragraph.add_run()
        if p.startswith("**") and p.endswith("**"):
            r.text = p[2:-2]
            _font(r, size, True, bold_color or color)
        else:
            r.text = p
            _font(r, size, False, color)


def textbox(slide, x, y, w, h, text: str = "", size: float = 11, bold: bool = False, color: RGBColor = TXT,
            align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, wrap: bool = True):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(tf, side, Cm(0.05))
    lines = text.split("\n") if text else [""]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        if bold:
            r = p.add_run()
            r.text = line
            _font(r, size, True, color)
        else:
            _rich(p, line, size, color)
    return tb


def _fit_size(items: list[str], w, h, size: float, min_size: float = 9.0) -> float:
    """줄 수를 세어 상자(h)를 넘지 않는 글자 크기(최소 min_size)."""
    plain = [re.sub(r"\*\*", "", t) for t in items]
    fs = size
    while fs > min_size:
        cap = max(10.0, (w / 12700 - 14) / fs)
        lines = sum(max(1, math.ceil((_text_units(t) + 1.5) / cap)) for t in plain)
        need = lines * fs * 1.1 * 1.25 + len(plain) * 3  # pt
        if need <= h / 12700:
            break
        fs -= 0.5
    return fs


def bullets(slide, items: list[str], x=Cm(2.0), y=Cm(3.45), w=Cm(26.2), h=Cm(1.8), size: float = 11):
    size = _fit_size(items, w, h, size)
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(tf, side, Cm(0.05))
    for i, t in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        pPr = p._p.get_or_add_pPr()
        pPr.set("marL", str(Cm(0.4)))
        pPr.set("indent", str(-Cm(0.4)))
        p.space_after = Pt(3)
        p.line_spacing = 1.1
        r = p.add_run()
        r.text = "• "
        _font(r, size, False, TXT)
        _rich(p, t, size, TXT, RGBColor(0x1A, 0x1A, 0x1A))
    return tb


# ---------------------------------------------------------------------------
# 슬라이드 틀
# ---------------------------------------------------------------------------
def header(slide, title: str, subtitle: str, brand: str) -> None:
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUND_1_RECTANGLE, Cm(1.32), Cm(0.23), Cm(27.2), Cm(2.74))
    sh.rotation = 180  # 둥근 모서리를 왼쪽 아래로
    sh.adjustments[0] = 0.33
    sh.line.fill.background()
    sh.shadow.inherit = False
    fill = sh.fill
    fill.gradient()
    fill.gradient_angle = 0
    stops = fill.gradient_stops
    stops[0].color.rgb = RGBColor(0x0A, 0x1C, 0x45)  # 회전 180° → 오른쪽이 남색
    stops[0].position = 0.0
    stops[1].color.rgb = RGBColor(0x0B, 0x63, 0xC9)
    stops[1].position = 1.0
    textbox(slide, Cm(2.0), Cm(0.85), Cm(18), Cm(1.0), title, 20, True, RGBColor(255, 255, 255))
    textbox(slide, Cm(2.0), Cm(1.85), Cm(18), Cm(0.7), subtitle, 11.5, False, RGBColor(255, 255, 255))
    textbox(slide, Cm(19.0), Cm(1.95), Cm(8.8), Cm(0.7), brand, 11.5, True, RGBColor(255, 255, 255), PP_ALIGN.RIGHT)


def footer(slide, page: int, note: str = "Strictly Private and Confidential") -> None:
    textbox(slide, Cm(17.0), Cm(19.85), Cm(10.6), Cm(0.5), note, 7.5, False, GREY, PP_ALIGN.RIGHT)
    ln = slide.shapes.add_connector(1, Cm(27.85), Cm(19.9), Cm(27.85), Cm(20.3))
    ln.line.color.rgb = GREY
    ln.line.width = Pt(0.75)
    textbox(slide, Cm(28.0), Cm(19.85), Cm(1.0), Cm(0.5), str(page), 7.5, False, GREY)


def section_label(slide, x, y, text: str, size: float = 13) -> None:
    box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y + Cm(0.1), Cm(0.42), Cm(0.42))
    box.fill.solid()
    box.fill.fore_color.rgb = NAVY
    box.line.fill.background()
    tri = slide.shapes.add_shape(MSO_SHAPE.RIGHT_TRIANGLE, x + Cm(0.06), y + Cm(0.22), Cm(0.3), Cm(0.24))
    tri.fill.solid()
    tri.fill.fore_color.rgb = RGBColor(255, 255, 255)
    tri.line.fill.background()
    tri.rotation = 0
    textbox(slide, x + Cm(0.55), y, Cm(14), Cm(0.7), text, size, True, RGBColor(0x11, 0x11, 0x11))


def small_note(slide, x, y, w, text: str, align=PP_ALIGN.LEFT, size: float = 7.5) -> None:
    textbox(slide, x, y, w, Cm(0.5), text, size, False, RGBColor(0x55, 0x55, 0x55), align)


# ---------------------------------------------------------------------------
# 표
# ---------------------------------------------------------------------------
def _cell_border(cell, sides: str = "LRTB", color: str = "BFC5CF", width_pt: float = 0.75) -> None:
    """셀 테두리. a:lnL~lnB는 tcPr의 맨 앞(채우기보다 먼저)에 와야 PowerPoint가 복구 경고를 내지 않음."""
    tcPr = cell._tc.get_or_add_tcPr()
    for s in "LRTB":
        old = tcPr.find(qn(f"a:ln{s}"))
        if old is not None:
            tcPr.remove(old)
    for i, s in enumerate("LRTB"):
        ln = etree.Element(qn(f"a:ln{s}"))
        if s in sides:
            ln.set("w", str(int(width_pt * 12700)))
            sf = etree.SubElement(ln, qn("a:solidFill"))
            c = etree.SubElement(sf, qn("a:srgbClr"))
            c.set("val", color)
        else:
            ln.set("w", "0")
            etree.SubElement(ln, qn("a:noFill"))
        tcPr.insert(i, ln)


def _cell_text(cell, text: str, size: float, bold=False, color=TXT, align=PP_ALIGN.CENTER, fill: RGBColor | None = None):
    cell.text = ""
    tf = cell.text_frame
    tf.word_wrap = True
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    for side in ("margin_left", "margin_right"):
        setattr(cell, side, Cm(0.08))
    cell.margin_top = cell.margin_bottom = Cm(0.03)
    lines = str(text).split("\n")
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = line
        _font(r, size, bold, color)
    if fill is not None:
        cell.fill.solid()
        cell.fill.fore_color.rgb = fill
    else:
        cell.fill.background()


def _strip_table_style(tbl_shape) -> None:
    tblPr = tbl_shape.table._tbl.tblPr
    for attr in ("firstRow", "bandRow"):
        tblPr.set(attr, "0")
    style = tblPr.find(qn("a:tableStyleId"))
    if style is None:
        style = etree.SubElement(tblPr, qn("a:tableStyleId"))
    style.text = "{5940675A-B579-460E-94D1-54222C63F5DA}"  # No Style, Table Grid


def _text_units(s: str) -> float:
    """한글·전각 1, 영숫자·기호 0.55로 센 글자 폭."""
    return sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in s or "")


def kv_table(slide, x, y, w, rows: list[tuple[str, str]], col1=Cm(2.8), row_h=Cm(0.86), size=10, max_h=None):
    """2열 표. 긴 값은 줄 수를 세어 행 높이를 늘리고, 전체가 max_h를 넘으면 글자를 8pt까지 줄임."""
    n = len(rows) + 1
    usable_pt = (w - col1) / 12700 - 14.4  # 셀 좌우 여백
    heights, fsize = [row_h] * n, size
    for fs in [size - 0.5 * i for i in range(int((size - 8) / 0.5) + 1)]:
        cap = max(8.0, usable_pt / fs)
        hs = [row_h] + [row_h + int(max(0, math.ceil(_text_units(str(v)) / cap) - 1) * Pt(fs * 1.3)) for _, v in rows]
        heights, fsize = hs, fs
        if not max_h or sum(hs) <= max_h:
            break
    if max_h and sum(heights) > max_h:  # 그래도 넘치면 기본 높이를 비율로 줄임
        k = max_h / sum(heights)
        heights = [int(h * k) for h in heights]
    shape = slide.shapes.add_table(n, 2, x, y, w, sum(heights))
    _strip_table_style(shape)
    t = shape.table
    t.columns[0].width = col1
    t.columns[1].width = w - col1
    _cell_text(t.cell(0, 0), "구분", fsize, True, TXT, fill=HEAD_FILL)
    _cell_text(t.cell(0, 1), "내용", fsize, True, TXT, fill=HEAD_FILL)
    for i, (k, v) in enumerate(rows, 1):
        _cell_text(t.cell(i, 0), k, fsize, True, TXT, fill=RGBColor(0xF2, 0xF4, 0xF8))
        _cell_text(t.cell(i, 1), v, fsize, False, TXT)
    for r in range(n):
        t.rows[r].height = heights[r]
        for c in range(2):
            _cell_border(t.cell(r, c), "LRTB", "7F7F7F", 0.5)
    return shape


def list_table(slide, x, y, w, head: list[str], rows: list[list[str]], widths: list[float], row_h=Cm(0.82),
               size=10.5, first_col_small=True, title: str | None = None, notes: list[str] | None = None):
    """주요 시설 접근성 같은 가로줄만 있는 표."""
    if title:
        textbox(slide, x, y, w, Cm(0.9), title, 14, True, NAVY)
        y = y + Cm(1.0)
    shape = slide.shapes.add_table(len(rows) + 1, len(head), x, y, w, row_h * (len(rows) + 1))
    _strip_table_style(shape)
    t = shape.table
    total = sum(widths)
    for i, wd in enumerate(widths):
        t.columns[i].width = int(w * wd / total)
    for c, h in enumerate(head):
        _cell_text(t.cell(0, c), h, size, True, NAVY, PP_ALIGN.LEFT, HEAD_FILL)
    for r, row in enumerate(rows, 1):
        for c, v in enumerate(row):
            small = first_col_small and c == 0
            _cell_text(t.cell(r, c), v, 8 if small else size, False, GREY if small else TXT, PP_ALIGN.LEFT)
    for r in range(len(rows) + 1):
        t.rows[r].height = row_h
        for c in range(len(head)):
            sides = "B"
            color = "1F2D4F" if r == len(rows) else "D0D5DD"
            _cell_border(t.cell(r, c), sides, color, 1.25 if r == len(rows) else 0.5)
    yy = y + row_h * (len(rows) + 1) + Cm(0.15)
    for n in notes or []:
        small_note(slide, x, yy, w, n, size=8)
        yy += Cm(0.45)
    return shape


def grid_table(slide, x, y, w, head: list[str], rows: list[list[str]], widths: list[float], row_h=Cm(1.3),
               head_h=Cm(1.5), size=10, highlight_first=True):
    shape = slide.shapes.add_table(len(rows) + 1, len(head), x, y, w, head_h + row_h * len(rows))
    _strip_table_style(shape)
    t = shape.table
    total = sum(widths)
    for i, wd in enumerate(widths):
        t.columns[i].width = int(w * wd / total)
    for c, h in enumerate(head):
        _cell_text(t.cell(0, c), h, size, True, TXT, fill=HEAD_FILL)
    t.rows[0].height = head_h
    for r, row in enumerate(rows, 1):
        t.rows[r].height = row_h
        for c, v in enumerate(row):
            fill = RGBColor(0xF2, 0xF2, 0xF2) if (highlight_first and r == 1) else None
            fs = size
            col_pt = t.columns[c].width / 12700 - 10  # 좌우 여백
            longest = max((_text_units(x) for x in str(v).split("\n")), default=0)
            if c >= 2 and longest and longest * fs > col_pt:  # 숫자 칸만: 한 줄에 들어가게
                fs = max(7.5, col_pt / longest)
            _cell_text(t.cell(r, c), v, fs, highlight_first and r == 1 and c <= 1, TXT, fill=fill)
    for r in range(len(rows) + 1):
        for c in range(len(head)):
            _cell_border(t.cell(r, c), "TB" + ("L" if c else "") + ("R" if c < len(head) - 1 else ""), "8C8C8C", 0.5)
    return shape


# ---------------------------------------------------------------------------
# 차트
# ---------------------------------------------------------------------------
def _axis_fmt(axis, size=8.5, color="595959"):
    axis.tick_labels.font.size = Pt(size)
    axis.tick_labels.font.color.rgb = RGBColor.from_string(color)
    axis.tick_labels.font.name = FONT
    axis.format.line.color.rgb = RGBColor(0xD9, 0xD9, 0xD9)


def _nice_range(vals: list[float], pad_ratio=0.15, step=None) -> tuple[float, float, float]:
    vs = [v for v in vals if v is not None]
    if not vs:
        return 0, 1, 1
    lo, hi = min(vs), max(vs)
    span = max(hi - lo, max(hi, 1) * 0.05)
    raw = span * (1 + 2 * pad_ratio) / 8
    mag = 10 ** math.floor(math.log10(raw))
    step = step or next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    lo2 = max(0, math.floor((lo - span * pad_ratio) / step) * step)
    hi2 = math.ceil((hi + span * pad_ratio) / step) * step
    return lo2, hi2, step


def bar_chart(slide, x, y, w, h, cats: list[str], vals: list[float], color: RGBColor = BAR, name="값",
              tight: bool = True, label_fmt='#,##0'):
    cd = CategoryChartData()
    cd.categories = cats
    cd.add_series(name, [v if v is not None else None for v in vals])
    gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, y, w, h, cd)
    ch = gf.chart
    ch.has_title = False
    ch.has_legend = False
    ch.font.name = FONT
    ch.font.size = Pt(9)
    plot = ch.plots[0]
    plot.gap_width = 140
    plot.has_data_labels = True
    dl = plot.data_labels
    dl.font.size = Pt(8.5)
    dl.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
    dl.number_format = label_fmt
    dl.number_format_is_linked = False
    dl.position = XL_LABEL_POSITION.OUTSIDE_END
    ser = plot.series[0]
    ser.format.fill.solid()
    ser.format.fill.fore_color.rgb = color
    va = ch.value_axis
    if tight:
        lo, hi, st = _nice_range(vals, 0.25)
        va.minimum_scale, va.maximum_scale, va.major_unit = lo, hi, st
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = RGBColor(0xE6, 0xE6, 0xE6)
    va.format.line.fill.background()
    va.tick_labels.number_format = '#,##0'
    va.tick_labels.number_format_is_linked = False
    _axis_fmt(va)
    _axis_fmt(ch.category_axis)
    return gf


def combo_chart(slide, x, y, w, h, cats: list[str], counts: list[float], prices: list[float | None],
                count_name="거래량", price_name="평균평단가"):
    """막대(거래량, 오른쪽 축) + 꺾은선(공급 3.3㎡당 평균가, 왼쪽 축) — 칠곡 IM 레퍼런스 배치."""
    cd = CategoryChartData()
    cd.categories = cats
    cd.add_series(count_name, counts)
    cd.add_series(price_name, prices)
    gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, y, w, h, cd)
    ch = gf.chart
    ch.has_title = False
    ch.font.name = FONT
    ch.font.size = Pt(9)
    ch.has_legend = True
    ch.legend.position = XL_LEGEND_POSITION.BOTTOM
    ch.legend.include_in_layout = False
    ch.legend.font.size = Pt(9)
    plot = ch.plots[0]
    plot.gap_width = 90
    bar = plot.series[0]
    bar.format.fill.solid()
    bar.format.fill.fore_color.rgb = BAR
    va = ch.value_axis  # 기본축(왼쪽) = 평단가(꺾은선)
    cmax = max([c for c in counts if c is not None] or [1])
    pv0 = [p for p in prices if p is not None]
    p_lo, p_hi = _line_range(pv0)
    # 막대 꼭대기가 선의 가장 낮은 점보다 아래(그 높이의 85%)에 오도록 거래량 축 상한을 정함
    low_frac = ((min(pv0) - p_lo) / (p_hi - p_lo)) if pv0 and p_hi > p_lo else 0.5
    bar_frac = min(0.5, max(0.1, low_frac * 0.7 - 0.04))  # 막대 레이블 높이까지 띄움
    top = max(4, cmax / bar_frac)
    unit = next(u for u in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 10 ** 9) if u * 5 >= top)
    va.minimum_scale = p_lo
    va.maximum_scale = p_hi
    va.tick_labels.number_format = "#,##0"
    va.tick_labels.number_format_is_linked = False
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = RGBColor(0xE6, 0xE6, 0xE6)
    va.format.line.fill.background()
    _axis_fmt(va)
    _axis_fmt(ch.category_axis)
    # 막대 데이터 레이블
    plot.has_data_labels = True
    plot.data_labels.font.size = Pt(8.5)
    plot.data_labels.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
    plot.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
    plot.data_labels.number_format = '#,##0;-#,##0;;'  # 0건은 표시 안 함
    plot.data_labels.number_format_is_linked = False

    # 두 번째 계열 → 꺾은선 + 보조축 (XML)
    cs = ch._chartSpace
    plotArea = cs.find(qn("c:chart")).find(qn("c:plotArea"))
    barChart = plotArea.find(qn("c:barChart"))
    sers = barChart.findall(qn("c:ser"))
    ser2 = sers[1]
    barChart.remove(ser2)
    for tag in ("c:invertIfNegative", "c:dLbls"):
        el = ser2.find(qn(tag))
        if el is not None:
            ser2.remove(el)
    C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    spPr = ser2.find(qn("c:spPr"))
    if spPr is not None:
        ser2.remove(spPr)
    spPr = etree.fromstring(
        f'<c:spPr xmlns:c="{C}" xmlns:a="{A}"><a:ln w="31750" cap="rnd"><a:solidFill><a:srgbClr val="FFD966"/>'
        f'</a:solidFill><a:round/></a:ln></c:spPr>')
    marker = etree.fromstring(
        f'<c:marker xmlns:c="{C}" xmlns:a="{A}"><c:symbol val="circle"/><c:size val="6"/><c:spPr>'
        f'<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:ln w="19050"><a:solidFill><a:srgbClr val="FFD966"/>'
        f'</a:solidFill></a:ln></c:spPr></c:marker>')
    dlbls = etree.fromstring(
        f'<c:dLbls xmlns:c="{C}" xmlns:a="{A}"><c:numFmt formatCode="#,##0" sourceLinked="0"/><c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>'
        f'<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="850" b="1"><a:solidFill><a:srgbClr val="7F6000"/>'
        f'</a:solidFill><a:latin typeface="{FONT}"/><a:ea typeface="{FONT}"/></a:defRPr></a:pPr><a:endParaRPr lang="ko-KR"/></a:p></c:txPr>'
        f'<c:dLblPos val="t"/><c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/>'
        f'<c:showSerName val="0"/><c:showPercent val="0"/><c:showBubbleSize val="0"/></c:dLbls>')
    # 순서: idx, order, tx, spPr, marker, dLbls, cat, val, smooth
    tx = ser2.find(qn("c:tx"))
    insert_at = list(ser2).index(tx) + 1
    ser2.insert(insert_at, spPr)
    ser2.insert(insert_at + 1, marker)
    ser2.insert(insert_at + 2, dlbls)
    smooth = etree.SubElement(ser2, qn("c:smooth"))
    smooth.set("val", "0")
    lc = etree.fromstring(f'<c:lineChart xmlns:c="{C}"><c:grouping val="standard"/><c:varyColors val="0"/></c:lineChart>')
    lc.append(ser2)
    for tag, val in (("c:marker", "1"),):
        el = etree.SubElement(lc, qn(tag))
        el.set("val", val)
    for ax in ("50050", "50060"):
        el = etree.SubElement(lc, qn("c:axId"))
        el.set("val", ax)
    barChart.addnext(lc)
    cat_ax = etree.fromstring(
        f'<c:catAx xmlns:c="{C}"><c:axId val="50050"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
        f'<c:delete val="1"/><c:axPos val="b"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/>'
        f'<c:tickLblPos val="nextTo"/><c:crossAx val="50060"/><c:crosses val="autoZero"/><c:auto val="1"/>'
        f'<c:lblAlgn val="ctr"/><c:lblOffset val="100"/><c:noMultiLvlLbl val="0"/></c:catAx>')
    val_ax = etree.fromstring(
        f'<c:valAx xmlns:c="{C}" xmlns:a="{A}"><c:axId val="50060"/><c:scaling><c:orientation val="minMax"/>'
        f'<c:max val="{unit * 5}"/><c:min val="0"/></c:scaling><c:delete val="0"/><c:axPos val="r"/>'
        f'<c:numFmt formatCode="#,##0" sourceLinked="0"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/>'
        f'<c:tickLblPos val="nextTo"/><c:spPr><a:ln><a:noFill/></a:ln></c:spPr>'
        f'<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="850"><a:solidFill><a:srgbClr val="595959"/></a:solidFill>'
        f'<a:latin typeface="{FONT}"/><a:ea typeface="{FONT}"/></a:defRPr></a:pPr><a:endParaRPr lang="ko-KR"/></a:p></c:txPr>'
        f'<c:crossAx val="50050"/><c:crosses val="max"/><c:crossBetween val="between"/><c:majorUnit val="{unit}"/></c:valAx>')
    last_ax = plotArea.findall(qn("c:valAx"))[-1]
    last_ax.addnext(cat_ax)
    cat_ax.addnext(val_ax)
    # 축 맞바꿈: 막대 → 보조축(오른쪽 거래량), 꺾은선 → 기본축(왼쪽 평단가)
    bar_ids = [e.get("val") for e in barChart.findall(qn("c:axId"))]
    line_ids = [e.get("val") for e in lc.findall(qn("c:axId"))]
    for e, v in zip(barChart.findall(qn("c:axId")), line_ids):
        e.set("val", v)
    for e, v in zip(lc.findall(qn("c:axId")), bar_ids):
        e.set("val", v)
    _span_blanks(ch)
    return gf


def _span_blanks(ch) -> None:
    """값이 빈 분기는 선을 끊지 않고 이어서 그림."""
    chart_el = ch._chartSpace.find(qn("c:chart"))
    el = chart_el.find(qn("c:dispBlanksAs"))
    if el is None:
        el = etree.Element(qn("c:dispBlanksAs"))
        pvo = chart_el.find(qn("c:plotVisOnly"))
        if pvo is not None:
            pvo.addnext(el)
        else:
            chart_el.find(qn("c:plotArea")).addnext(el)
    el.set("val", "span")


def _line_range(pv: list[float]) -> tuple[float, float]:
    """꺾은선이 막대 위쪽(높이 55~90%)에 오도록 보조축 범위."""
    if not pv:
        return 0, 1
    pmin, pmax = min(pv), max(pv)
    span = (pmax - pmin) / 0.35 if pmax > pmin else max(pmax, 1) * 0.6
    lo = pmin - 0.55 * span
    hi = lo + span
    raw = span / 5
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    lo = max(0, math.floor(lo / step) * step)
    hi = math.ceil(hi / step) * step
    return lo, hi


# ---------------------------------------------------------------------------
# 지도 그리기 (PIL)
# ---------------------------------------------------------------------------
_FONT_PATHS = {
    False: [r"C:\Windows\Fonts\malgun.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc"],
    True: [r"C:\Windows\Fonts\malgunbd.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
           "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc"],
}
_font_cache: dict = {}


def pil_font(size: int, bold: bool = False):
    k = (size, bold)
    if k in _font_cache:
        return _font_cache[k]
    for p in _FONT_PATHS[bold] + _FONT_PATHS[False]:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                _font_cache[k] = f
                return f
            except OSError:
                continue
    f = ImageFont.load_default()
    _font_cache[k] = f
    return f


def merc(lon: float, lat: float) -> tuple[float, float]:
    x = lon * 20037508.342789244 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * 20037508.342789244 / math.pi
    return x, y


class MapCanvas:
    def __init__(self, png: bytes | None, clat: float, clon: float, zoom: int, w: int, h: int):
        self.clat, self.clon, self.zoom, self.w, self.h = clat, clon, zoom, w, h
        self.res = 156543.03392804097 / (2 ** zoom)
        if png:
            img = Image.open(io.BytesIO(png)).convert("RGBA")
            if img.size != (w, h):
                img = img.resize((w, h))
        else:
            img = Image.new("RGBA", (w, h), (238, 241, 245, 255))
            d = ImageDraw.Draw(img)
            for gx in range(0, w, 64):
                d.line([gx, 0, gx, h], fill=(225, 229, 235, 255))
            for gy in range(0, h, 64):
                d.line([0, gy, w, gy], fill=(225, 229, 235, 255))
            d.text((12, h - 30), "배경지도를 불러오지 못했습니다(브이월드)", font=pil_font(16), fill=(120, 120, 120, 255))
        self.img = img
        self.cx, self.cy = merc(clon, clat)

    def px(self, lat: float, lon: float) -> tuple[float, float]:
        x, y = merc(lon, lat)
        return self.w / 2 + (x - self.cx) / self.res, self.h / 2 - (y - self.cy) / self.res

    def km_px(self, km: float) -> float:
        return km * 1000 / math.cos(math.radians(self.clat)) / self.res

    def overlay(self) -> "ImageDraw.ImageDraw":
        return ImageDraw.Draw(self.img, "RGBA")

    def dashed_circle(self, km: float, color=(200, 30, 45, 230), width=3, dash_deg=4.0) -> None:
        d = self.overlay()
        cx, cy = self.px(self.clat, self.clon)
        r = self.km_px(km)
        a = 0.0
        while a < 360:
            d.arc([cx - r, cy - r, cx + r, cy + r], a, a + dash_deg, fill=color, width=width)
            a += dash_deg * 2

    def star(self, lat: float, lon: float, r: int = 16, label: str | None = None) -> None:
        d = self.overlay()
        x, y = self.px(lat, lon)
        d.ellipse([x - r - 3, y - r - 3, x + r + 3, y + r + 3], fill=(255, 255, 255, 235))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(216, 30, 45, 255))
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rr = r * 0.78 if i % 2 == 0 else r * 0.32
            pts.append((x + rr * math.cos(ang), y + rr * math.sin(ang)))
        d.polygon(pts, fill=(255, 255, 255, 255))
        if label:
            self.label_box(x + r + 8, y - 13, label, color=(200, 20, 35, 255), bold=True, border=(200, 20, 35, 255))

    def label_box(self, x: float, y: float, text: str, color=(20, 35, 75, 255), bold=False, border=(120, 130, 150, 255),
                  size: int = 17, avoid: list | None = None) -> tuple:
        d = self.overlay()
        f = pil_font(size, bold)
        tb = d.textbbox((0, 0), text, font=f)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        pad = 5
        bw, bh = tw + pad * 2, th + pad * 2 + 2
        if x + bw > self.w - 4:
            x = max(4, x - bw - 40)
        y = min(max(4, y), self.h - bh - 4)
        box = (x, y, x + bw, y + bh)
        if avoid is not None:
            for _ in range(8):
                hit = next((b for b in avoid if not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3])), None)
                if not hit:
                    break
                y = hit[3] + 3
                if y + bh > self.h - 4:
                    y = hit[1] - bh - 3
                box = (x, y, x + bw, y + bh)
            avoid.append(box)
        d.rounded_rectangle(box, radius=4, fill=(255, 255, 255, 235), outline=border, width=1)
        d.text((x + pad, y + pad - tb[1]), text, font=f, fill=color)
        return box

    def marker(self, lat: float, lon: float, n: int, label: str | None = None, fill=(27, 42, 82, 255),
               avoid: list | None = None) -> None:
        d = self.overlay()
        x, y = self.px(lat, lon)
        if not (-20 < x < self.w + 20 and -20 < y < self.h + 20):
            return
        r = 13
        d.ellipse([x - r - 2, y - r - 2, x + r + 2, y + r + 2], fill=(255, 255, 255, 240))
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)
        f = pil_font(15, True)
        s = str(n)
        tb = d.textbbox((0, 0), s, font=f)
        d.text((x - (tb[2] - tb[0]) / 2 - tb[0], y - (tb[3] - tb[1]) / 2 - tb[1]), s, font=f, fill=(255, 255, 255, 255))
        if label:
            self.label_box(x + r + 6, y - 13, f"{n}. {label}", avoid=avoid)

    def legend(self, lines: list[str], x: int = 14, bottom: int = 14, size: int = 14) -> None:
        if not lines:
            return
        d = self.overlay()
        f = pil_font(size)
        widths = [d.textbbox((0, 0), s, font=f)[2] for s in lines]
        lh = size + 6
        bw, bh = max(widths) + 20, lh * len(lines) + 14
        y = self.h - bottom - bh
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=6, fill=(255, 255, 255, 235), outline=(170, 175, 185, 255))
        for i, s in enumerate(lines):
            d.text((x + 10, y + 7 + i * lh), s, font=f, fill=(40, 40, 40, 255))

    def legend_colored(self, items: list[tuple[str, tuple]], x: int = 14, bottom: int = 14, size: int = 14) -> None:
        if not items:
            return
        d = self.overlay()
        f = pil_font(size)
        lh = size + 8
        bw = max(d.textbbox((0, 0), t, font=f)[2] for t, _ in items) + 44
        bh = lh * len(items) + 14
        y = self.h - bottom - bh
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=6, fill=(255, 255, 255, 235), outline=(170, 175, 185, 255))
        for i, (t, col) in enumerate(items):
            yy = y + 7 + i * lh
            d.ellipse([x + 10, yy + 2, x + 10 + size, yy + 2 + size], fill=col)
            d.text((x + 16 + size, yy), t, font=f, fill=(40, 40, 40, 255))

    def title(self, line1: str, line2: str | None = None) -> None:
        d = self.overlay()
        f1, f2 = pil_font(17, True), pil_font(13)
        w1 = d.textbbox((0, 0), line1, font=f1)[2]
        w2 = d.textbbox((0, 0), line2, font=f2)[2] if line2 else 0
        bw = max(w1, w2) + 20
        bh = 34 + (22 if line2 else 0)
        d.rounded_rectangle([12, 12, 12 + bw, 12 + bh], radius=6, fill=(255, 255, 255, 235), outline=(170, 175, 185, 255))
        d.text((22, 18), line1, font=f1, fill=(20, 20, 20, 255))
        if line2:
            d.text((22, 44), line2, font=f2, fill=(90, 90, 90, 255))

    def attribution(self, text: str = "배경지도 © 브이월드(국토교통부)") -> None:
        d = self.overlay()
        f = pil_font(11)
        tb = d.textbbox((0, 0), text, font=f)
        x, y = self.w - tb[2] - 12, self.h - tb[3] - 8
        d.rectangle([x - 4, y - 2, x + tb[2] + 4, y + tb[3] + 3], fill=(255, 255, 255, 200))
        d.text((x, y), text, font=f, fill=(80, 80, 80, 255))

    def png(self, crop: tuple | None = None) -> bytes:
        img = self.img.convert("RGB")
        if crop:
            img = img.crop(crop)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()


CAT_COLORS = {"산업": (125, 60, 152, 255), "교통": (230, 120, 20, 255), "행정": (40, 140, 80, 255),
              "광역": (100, 100, 110, 255), "교육": (30, 110, 200, 255), "생활": (220, 90, 50, 255),
              "의료": (200, 40, 80, 255), "자연": (60, 150, 70, 255)}


def draw_wide_map(base: dict, target: dict, facilities: list[dict]) -> bytes:
    m = MapCanvas(base.get("png"), base["lat"], base["lon"], base["zoom"], base["w"], base["h"])
    avoid: list = []
    for i, f in enumerate(facilities, 1):
        if f.get("lat") is None:
            continue
        m.marker(f["lat"], f["lon"], i, f["시설"], fill=CAT_COLORS.get(f["구분"], (27, 42, 82, 255)), avoid=avoid)
    m.star(target["lat"], target["lon"], 17, target.get("name"))
    cats = []
    for f in facilities:
        if f["구분"] not in cats:
            cats.append(f["구분"])
    m.legend_colored([(c, CAT_COLORS.get(c, (27, 42, 82, 255))) for c in cats], size=14)
    m.attribution()
    return m.png()


def draw_near_map(base: dict, target: dict, radius_km: float, complexes: list[dict], title: str, crop_ratio: float | None = None,
                  extra: list[dict] | None = None) -> bytes:
    png, w, h = base.get("png"), base["w"], base["h"]
    if crop_ratio:  # 가운데를 세로로 잘라 쓰는 경우: 먼저 자르고 그 위에 그림(제목·범례가 잘리지 않게)
        nw = min(w, int(h * crop_ratio))
        x0 = (w - nw) // 2
        if png:
            im = Image.open(io.BytesIO(png)).convert("RGBA").resize((w, h)).crop((x0, 0, x0 + nw, h))
            buf = io.BytesIO()
            im.save(buf, "PNG")
            png = buf.getvalue()
        w = nw
    m = MapCanvas(png, base["lat"], base["lon"], base["zoom"], w, h)
    m.dashed_circle(radius_km)
    avoid: list = []
    for f in extra or []:
        if f.get("lat") is None:
            continue
        x, y = m.px(f["lat"], f["lon"])
        d = m.overlay()
        col = CAT_COLORS.get(f["구분"], (90, 90, 90, 255))
        d.rectangle([x - 6, y - 6, x + 6, y + 6], fill=col, outline=(255, 255, 255, 255), width=2)
    for c in complexes:
        m.marker(c["lat"], c["lon"], c["no"], c["단지명"], avoid=avoid)
    m.star(target["lat"], target["lon"], 16, f"기준 {target.get('name') or '본 건'}")
    m.title(title, "빨간 별: 기준 단지 · 번호: 비교 단지(표 번호와 같음) · 점선: 반경 %gkm" % radius_km)
    leg = []
    for c in complexes[:12]:
        meta = ", ".join(x for x in [str(c.get("준공연도") or ""),
                                     f"{c['세대수']:,}세대" if c.get("세대수") else ""] if x)
        leg.append(f"{c['no']} {c['단지명']}" + (f" ({meta})" if meta else ""))
    m.legend(leg, size=15)
    m.attribution()
    return m.png()


def draw_aerial(base: dict, overlay: bytes | None, rings: list) -> bytes:
    m = MapCanvas(base.get("png"), base["lat"], base["lon"], base["zoom"], base["w"], base["h"])
    if overlay:
        ov = Image.open(io.BytesIO(overlay)).convert("RGBA").resize(m.img.size)
        m.img = Image.alpha_composite(m.img, ov)
    d = m.overlay()
    for ring in rings:
        pts = [m.px(y, x) for x, y in ring]
        if len(pts) >= 3:
            d.polygon(pts, fill=(255, 40, 40, 45))
            d.line(pts + [pts[0]], fill=(255, 30, 30, 255), width=4, joint="curve")
    m.attribution("항공사진·지적도 © 브이월드(국토교통부)")
    return m.png()


def draw_plain(base: dict, target: dict | None = None) -> bytes:
    m = MapCanvas(base.get("png"), base["lat"], base["lon"], base["zoom"], base["w"], base["h"])
    if target:
        m.star(target["lat"], target["lon"], 14, target.get("name"))
    m.attribution()
    return m.png()


# ---------------------------------------------------------------------------
# 프레젠테이션
# ---------------------------------------------------------------------------
def _pic(slide, png: bytes | None, x, y, w, h, border=True, fill=True):
    if not png:
        box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
        box.fill.solid()
        box.fill.fore_color.rgb = RGBColor(0xEE, 0xF1, 0xF7)
        box.line.color.rgb = LINE
        textbox(slide, x, y + h / 2 - Cm(0.4), w, Cm(0.8), "[이미지 없음]", 12, False, GREY, PP_ALIGN.CENTER)
        return box
    img = Image.open(io.BytesIO(png))
    iw, ih = img.size
    if fill:  # 칸을 꽉 채우도록 가운데를 잘라 붙임 (비율 유지)
        r_slot, r_img = w / h, iw / ih
        if abs(r_slot - r_img) > 0.01:
            if r_img > r_slot:
                nw = int(ih * r_slot)
                img = img.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
            else:
                nh = int(iw / r_slot)
                img = img.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG" if not img.mode == "RGB" else "JPEG", quality=90)
            png = buf.getvalue()
        pic = slide.shapes.add_picture(io.BytesIO(png), x, y, w, h)
        if border:
            pic.line.color.rgb = LINE
            pic.line.width = Pt(0.75)
        return pic
    scale = min(w / iw, h / ih)
    pw, ph = int(iw * scale), int(ih * scale)
    px, py = x + (w - pw) // 2, y + (h - ph) // 2
    pic = slide.shapes.add_picture(io.BytesIO(png), px, py, pw, ph)
    if border:
        pic.line.color.rgb = LINE
        pic.line.width = Pt(0.75)
    return pic


def won_eok(v: float | None) -> str:
    if not v:
        return "-"
    return f"{v / 10000:.2f}억원"


def build_pptx(data: dict, out_path: Path, brand: str = "JB Asset Management") -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H
    blank = prs.slide_layouts[6]
    page = int(data.get("start_page") or 1)
    sec = data.get("sections", {})
    name = data.get("asset_name") or "본 건"

    def new_slide(title, sub):
        nonlocal page
        s = prs.slides.add_slide(blank)
        header(s, title, sub, brand)
        footer(s, page)
        page += 1
        return s

    # 1. 자산개요 ---------------------------------------------------------
    ov = sec.get("overview")
    if ov:
        s = new_slide(ov["title"], ov["subtitle"])
        bullets(s, ov["bullets"], h=Cm(1.6))
        section_label(s, Cm(2.04), Cm(5.45), "자산개요")
        rows = ov["rows"]
        kv_table(s, Cm(2.04), Cm(6.15), Cm(12.9), rows, col1=Cm(2.8), row_h=Cm(min(0.86, 12.6 / (len(rows) + 1))),
                 max_h=Cm(12.6))
        section_label(s, Cm(15.25), Cm(5.45), ov.get("img1_title", "항공사진"))
        _pic(s, ov.get("img1"), Cm(15.25), Cm(6.15), Cm(11.85), Cm(6.3))
        section_label(s, Cm(15.25), Cm(12.85), ov.get("img2_title", "위치도"))
        _pic(s, ov.get("img2"), Cm(15.25), Cm(13.55), Cm(11.85), Cm(5.95))
        if ov.get("note"):
            small_note(s, Cm(2.04), Cm(19.4), Cm(13), ov["note"])

    # 1-2. 현장사진 (3장 이상일 때) ---------------------------------------
    ph = sec.get("photos")
    if ph:
        s = new_slide(ph["title"], ph["subtitle"])
        bullets(s, ph["bullets"], h=Cm(1.2))
        n = len(ph["photos"])
        cols = 3 if n > 4 else 2
        rows_n = math.ceil(n / cols)
        gw, gh = Cm(25.6), Cm(14.2)
        cw, ch_ = (gw - Cm(0.5) * (cols - 1)) / cols, (gh - Cm(0.9) * rows_n) / rows_n
        for i, (cap, img) in enumerate(ph["photos"]):
            cx = Cm(2.04) + (i % cols) * (cw + Cm(0.5))
            cy = Cm(5.2) + (i // cols) * (ch_ + Cm(0.9))
            section_label(s, cx, cy, cap, 11)
            _pic(s, img, int(cx), int(cy + Cm(0.75)), int(cw), int(ch_ - Cm(0.1)))

    # 2. 광역 입지 -------------------------------------------------------
    wd = sec.get("wide")
    if wd:
        s = new_slide(wd["title"], wd["subtitle"])
        bullets(s, wd["bullets"], h=Cm(1.6))
        section_label(s, Cm(2.04), Cm(5.45), "광역위치도")
        _pic(s, wd.get("map"), Cm(2.18), Cm(6.2), Cm(14.9), Cm(12.7))
        rows = [[r["구분"], f"{i}. {r['시설']}", f"약 {r['거리km']:.1f}km"] for i, r in enumerate(wd["rows"], 1)]
        list_table(s, Cm(17.6), Cm(6.2), Cm(9.6), ["구분", "시설", "거리(직선)"], rows, [1.1, 5.6, 2.2],
                   row_h=Cm(min(0.82, 10.5 / (len(rows) + 1))), size=10, title="주요 시설 접근성", notes=wd.get("notes"))

    # 3. 협역 입지 -------------------------------------------------------
    nr = sec.get("near")
    if nr:
        s = new_slide(nr["title"], nr["subtitle"])
        bullets(s, nr["bullets"], h=Cm(1.6))
        section_label(s, Cm(2.04), Cm(5.45), "협역위치도")
        _pic(s, nr.get("map"), Cm(2.04), Cm(6.2), Cm(13.3), Cm(13.25))
        rows = [[r["구분"], r["시설"], r["거리"]] for r in nr["rows"]]
        list_table(s, Cm(15.8), Cm(6.2), Cm(11.8), ["구분", "시설", "거리(직선)"], rows, [1.1, 6.5, 2.2],
                   row_h=Cm(min(0.82, 10.8 / (len(rows) + 1))), size=10.5, title="주요 생활 인프라", notes=nr.get("notes"))

    # 4. 미분양 ----------------------------------------------------------
    us = sec.get("unsold")
    if us:
        s = new_slide(us["title"], us["subtitle"])
        bullets(s, us["bullets"], h=Cm(1.6))
        for i, ch in enumerate(us["charts"]):
            x = Cm(2.04) + i * Cm(13.15)
            section_label(s, x, Cm(5.9), ch["title"])
            small_note(s, x + Cm(9.0), Cm(6.35), Cm(3.6), f"단위 : {ch.get('unit', '세대수')}", PP_ALIGN.RIGHT)
            bar_chart(s, x, Cm(6.8), Cm(12.6), Cm(11.0), ch["cats"], ch["vals"], BAR if i == 0 else BAR2, ch["title"])
            small_note(s, x, Cm(18.3), Cm(12.6), ch.get("source", ""), PP_ALIGN.CENTER)

    # 5. 거래 추이 -------------------------------------------------------
    tr = sec.get("trend")
    if tr:
        s = new_slide(tr["title"], tr["subtitle"])
        bullets(s, tr["bullets"], h=Cm(2.1))
        for i, ch in enumerate(tr["charts"]):
            x = Cm(2.04) + i * Cm(13.15)
            section_label(s, x, Cm(5.9), ch["title"])
            small_note(s, x + Cm(8.0), Cm(6.35), Cm(4.6), "단위 : 건, 만원/평(공급)", PP_ALIGN.RIGHT)
            combo_chart(s, x, Cm(6.8), Cm(12.6), Cm(11.2), ch["cats"], ch["counts"], ch["prices"])
            small_note(s, x, Cm(18.3), Cm(12.6), ch.get("source", ""), PP_ALIGN.CENTER)

    # 6. 시세 현황 -------------------------------------------------------
    pr = sec.get("prices")
    if pr:
        s = new_slide(pr["title"], pr["subtitle"])
        bullets(s, pr["bullets"], h=Cm(2.35))
        section_label(s, Cm(2.04), Cm(5.95), pr.get("label", "주요 단지 시세 현황"))
        _pic(s, pr.get("map"), Cm(2.02), Cm(6.78), Cm(10.33), Cm(12.7))
        head = ["", "구분", "준공\n년도", "세대수", "전용\n면적", "매매가", "매매공급\n평단가", "전세가", "전세공급\n평단가", "시공사"]
        rows = pr["rows"]
        notes = pr.get("notes", [])[:4]
        cap = (Cm(15.7) / 12700 - 6) / 7.0  # 7pt 한 줄 글자 수
        n_lines = [max(1, math.ceil(_text_units(n) / cap)) for n in notes]
        note_h = sum(0.12 + 0.3 * k for k in n_lines)
        rh = min(1.45, (19.3 - 6.78 - 1.4 - note_h - 0.2) / max(1, len(rows)))
        grid_table(s, Cm(12.9), Cm(6.78), Cm(15.7), head, rows, [0.6, 3.25, 1.15, 1.0, 1.0, 1.6, 1.55, 1.6, 1.55, 1.5],
                   row_h=Cm(rh), head_h=Cm(1.4), size=9 if len(rows) > 6 else 10, highlight_first=pr.get("has_target", True))
        ny = 6.78 + 1.4 + rh * len(rows) + 0.12
        for n, k in zip(notes, n_lines):
            small_note(s, Cm(12.9), Cm(ny), Cm(15.7), n, size=7)
            ny += 0.12 + 0.3 * k

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return out_path


# ---------------------------------------------------------------------------
# PDF 변환
# ---------------------------------------------------------------------------
def _soffice() -> str | None:
    for c in ("soffice", "libreoffice"):
        p = shutil.which(c)
        if p:
            return p
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.path.exists(p):
            return p
    return None


def to_pdf(pptx: Path) -> tuple[Path | None, str]:
    pdf = pptx.with_suffix(".pdf")
    errors = []
    if sys.platform == "win32":
        try:
            import comtypes  # type: ignore
            import comtypes.client  # type: ignore
            comtypes.CoInitialize()
            try:
                app = comtypes.client.CreateObject("PowerPoint.Application")
                pres = app.Presentations.Open(str(pptx.resolve()), True, False, False)  # 읽기전용·창 없음
                try:
                    pres.SaveAs(str(pdf.resolve()), 32)  # ppSaveAsPDF
                finally:
                    pres.Close()
                try:
                    if app.Presentations.Count == 0:
                        app.Quit()
                except Exception:
                    pass
            finally:
                comtypes.CoUninitialize()
            if pdf.exists():
                return pdf, "PowerPoint"
        except Exception as e:  # PowerPoint 없음 등
            errors.append(f"PowerPoint: {e}")
    so = _soffice()
    if so:
        try:
            subprocess.run([so, "--headless", "--convert-to", "pdf", "--outdir", str(pptx.parent), str(pptx)],
                           check=True, timeout=180, capture_output=True)
            if pdf.exists():
                return pdf, "LibreOffice"
        except Exception as e:
            errors.append(f"LibreOffice: {e}")
    else:
        errors.append("LibreOffice 없음")
    return None, "; ".join(errors)
