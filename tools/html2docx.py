"""Конвертер простого семантического HTML (h1-h3, p, ul/ol/li, table, div.box, b/i/code/br/span) в DOCX.

Используется для рабочих документов ВКР: исходник пишется в HTML (из него же печатается PDF),
DOCX получается с нормальными стилями Word, а не с «рамкой на каждую строку», как у LibreOffice.
Запуск: python html2docx.py input.html output.docx
"""
import sys
from html.parser import HTMLParser

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ACCENT = RGBColor(0x1F, 0x4E, 0x8C)
MUTED = RGBColor(0x5B, 0x61, 0x70)
BODY_FONT = "Times New Roman"
MONO_FONT = "Menlo"


def shade(cell_or_par, hex_fill):
    """Заливка ячейки таблицы или абзаца."""
    el = cell_or_par._tc if hasattr(cell_or_par, "_tc") else cell_or_par._p
    props = el.get_or_add_tcPr() if hasattr(cell_or_par, "_tc") else el.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    props.append(shd)


def left_border(par, hex_color="1F4E8C"):
    p_pr = par._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), "18")
    left.set(qn("w:space"), "6")
    left.set(qn("w:color"), hex_color)
    bdr.append(left)
    p_pr.append(bdr)


class Converter(HTMLParser):
    def __init__(self, doc):
        super().__init__(convert_charrefs=True)
        self.doc = doc
        self.par = None          # текущий абзац
        self.fmt = []            # стек форматирования: 'b', 'i', 'code', 'small', 'muted'
        self.lists = []          # стек списков: ('ul'|'ol', счётчик)
        self.in_box = False
        self.table = None        # (таблица docx, текущая строка, класс)
        self.rows = []           # строки текущей таблицы: список списков (текст-абзацев не храним — пишем сразу)
        self.cell = None
        self.cell_is_th = False
        self.skip = 0            # внутри <head>/<style>/<title>
        self.heading = None
        self.td_num = False

    # --- создание абзацев ---
    def new_par(self, style=None):
        if self.cell is not None:
            # первый абзац ячейки уже существует
            if self.cell_fresh:
                self.par = self.cell.paragraphs[0]
                self.cell_fresh = False
            else:
                self.par = self.cell.add_paragraph()
            self.par.paragraph_format.space_after = Pt(0)
            if self.td_num:
                self.par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            return self.par
        self.par = self.doc.add_paragraph(style=style)
        if self.in_box:
            shade(self.par, "F3F5F8")
            left_border(self.par)
            self.par.paragraph_format.left_indent = Cm(0.3)
        return self.par

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if tag in ("head", "style", "title", "script"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3"):
            level = int(tag[1])
            self.par = self.doc.add_heading(level=level)
            self.heading = level
        elif tag == "p":
            self.new_par()
            if "small" in cls:
                self.fmt.append("small")
                self._p_small = True
            else:
                self._p_small = False
        elif tag == "div" and "box" in cls:
            self.in_box = True
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li":
            kind = self.lists[-1][0] if self.lists else "ul"
            if self.lists:
                self.lists[-1][1] += 1
            if self.cell is None and "refs" in getattr(self, "_list_cls", ""):
                pass
            style = "List Number" if kind == "ol" else "List Bullet"
            self.new_par(style=style)
            if kind == "ol" and self._numbered_manual:
                self.par.style = self.doc.styles["Normal"]
                self.par.paragraph_format.left_indent = Cm(0.9)
                self.par.paragraph_format.first_line_indent = Cm(-0.7)
                run = self.par.add_run(f"{self.lists[-1][1]}. ")
                self._style_run(run)
        elif tag == "table":
            self.table = self.doc.add_table(rows=0, cols=0)
            self.table_cls = cls
            self.table_rows = []
        elif tag == "tr":
            self.table_rows.append([])
        elif tag in ("td", "th"):
            self.table_rows[-1].append({"th": tag == "th", "num": "num" in cls, "parts": []})
            self._cell_buf = self.table_rows[-1][-1]["parts"]
        elif tag == "img":
            import os
            src = a.get("src", "")
            path = src if os.path.isabs(src) else os.path.join(self.base_dir, src)
            if os.path.exists(path):
                self.doc.add_picture(path, width=Cm(16.0))
                self.doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                self.par = None
        elif tag == "br":
            if self._cell_buf is not None and self.table is not None:
                self._cell_buf.append(("br", None))
            elif self.par is not None:
                self.par.add_run().add_break()
        elif tag == "b" or tag == "strong":
            self.fmt.append("b")
        elif tag == "i" or tag == "em":
            self.fmt.append("i")
        elif tag == "code":
            self.fmt.append("code")
        elif tag == "span":
            if "small" in cls:
                self.fmt.append("small")
            elif "tag" in cls:
                self.fmt.append("muted")
            elif "rec" in cls:
                self.fmt.append("b")
            else:
                self.fmt.append("none")

    def handle_endtag(self, tag):
        if tag in ("head", "style", "title", "script"):
            self.skip -= 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3"):
            self.heading = None
            self.par = None
        elif tag == "p":
            if getattr(self, "_p_small", False) and self.fmt and self.fmt[-1] == "small":
                self.fmt.pop()
            self.par = None
        elif tag == "div":
            self.in_box = False
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
        elif tag == "li":
            self.par = None
        elif tag in ("b", "strong", "i", "em", "code", "span"):
            if self.fmt:
                self.fmt.pop()
        elif tag in ("td", "th"):
            self._cell_buf = None
        elif tag == "table":
            self._flush_table()
            self.table = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.table is not None:
            if getattr(self, "_cell_buf", None) is not None:
                self._cell_buf.append(("text", data, tuple(self.fmt)))
            return
        if not data.strip() and self.par is None:
            return
        if self.par is None:
            self.new_par()
        run = self.par.add_run(data.replace("\n", " "))
        self._style_run(run)

    def _style_run(self, run, fmt=None):
        fmt = self.fmt if fmt is None else fmt
        if "b" in fmt:
            run.bold = True
        if "i" in fmt:
            run.italic = True
        if "code" in fmt:
            run.font.name = MONO_FONT
            run.font.size = Pt(9.5)
        if "small" in fmt:
            run.font.size = Pt(10)
            run.font.color.rgb = MUTED
        if "muted" in fmt:
            run.font.size = Pt(9)
            run.font.color.rgb = MUTED
        if self.heading is not None:
            run.font.color.rgb = ACCENT if self.heading > 1 else RGBColor(0x1B, 0x1D, 0x21)

    def _flush_table(self):
        rows = self.table_rows
        ncols = max(len(r) for r in rows)
        tbl = self.table
        meta = "meta" in self.table_cls
        for _ in range(ncols):
            tbl.add_column(Cm(1))
        for r in rows:
            cells = tbl.add_row().cells
            for j, c in enumerate(r):
                cell = cells[j]
                par = cell.paragraphs[0]
                par.paragraph_format.space_after = Pt(0)
                if c["num"]:
                    par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                for part in c["parts"]:
                    if part[0] == "br":
                        par.add_run().add_break()
                        continue
                    _, text, fmt = part
                    text = text.replace("\n", " ")
                    if not text.strip() and not par.runs:
                        continue
                    run = par.add_run(text)
                    self._style_run(run, fmt)
                    run.font.size = Pt(10 if not meta else 10.5)
                    if c["th"]:
                        run.bold = True
                if c["th"]:
                    shade(cell, "F3F5F8")
        total_cm = 16.8
        if meta:
            tbl.style = self.doc.styles["Normal Table"]
            widths_cm = [3.2, total_cm - 3.2]
        else:
            tbl.style = self.doc.styles["Table Grid"]
            # ширина колонки пропорциональна корню из объёма текста, но не меньше 2.1 см
            load = [0.0] * ncols
            for r in rows:
                for j, c in enumerate(r):
                    n = sum(len(p[1]) for p in c["parts"] if p[0] == "text")
                    load[j] = max(load[j], n ** 0.5)
            s = sum(load) or 1.0
            widths_cm = [max(2.1, total_cm * x / s) for x in load]
            k = total_cm / sum(widths_cm)
            widths_cm = [w * k for w in widths_cm]
        tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
        tbl.autofit = False
        grid = tbl._tbl.tblGrid
        for j, gc in enumerate(grid.findall(qn("w:gridCol"))):
            if j < len(widths_cm):
                gc.set(qn("w:w"), str(int(widths_cm[j] * 567)))
        for row in tbl.rows:
            for j, cell in enumerate(row.cells):
                if j < len(widths_cm):
                    cell.width = Cm(widths_cm[j])
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)


def build(src, dst):
    doc = Document()
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    sec.left_margin, sec.right_margin = Cm(2.5), Cm(1.5)
    sec.top_margin, sec.bottom_margin = Cm(2.0), Cm(2.0)
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    normal.font.size = Pt(12)
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.line_spacing = 1.15
    for name, size in (("Heading 1", 16), ("Heading 2", 13.5), ("Heading 3", 12.5)):
        st = doc.styles[name]
        st.font.name = BODY_FONT
        st.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
        st.font.size = Pt(size)
        st.font.bold = True
        st.paragraph_format.space_before = Pt(12 if name != "Heading 1" else 0)
        st.paragraph_format.space_after = Pt(4)
    conv = Converter(doc)
    import os
    conv.base_dir = os.path.dirname(os.path.abspath(src))
    conv._cell_buf = None
    conv.cell = None
    conv._numbered_manual = True  # нумерация вручную: стабильно в Word/LibreOffice/Pages
    conv.feed(open(src, encoding="utf-8").read())
    import re as _re
    m = _re.search(r"<title>(.*?)</title>", open(src, encoding="utf-8").read(), _re.S)
    doc.core_properties.title = m.group(1).strip() if m else ""
    doc.core_properties.author = "Евстигнеев Н. А."
    doc.save(dst)


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
