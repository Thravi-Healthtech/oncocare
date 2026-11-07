"""Small PDF writer (standard library only) for documents sent to patients on WhatsApp.

The clinician app prints summaries from HTML in the browser; WhatsApp needs a real file, and the server has no browser
or PDF library. This writes simple A4 documents: a header band, section headings, wrapped paragraphs, label/value
grids and tables, with page numbers. Text uses the PDF base-14 Helvetica fonts (WinAnsi encoding), so English text and
common symbols render exactly; characters outside Windows-1252 (e.g. Indic scripts) are replaced with "?".
"""
from __future__ import annotations

import zlib
from typing import Iterable, Sequence

PAGE_W, PAGE_H = 595.28, 841.89  # A4 in points
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 42.0, 46.0, 54.0
ACCENT = (0.059, 0.420, 0.392)   # teal, as in the print view
INK = (0.12, 0.14, 0.16)
MUTED = (0.42, 0.45, 0.48)
RULE = (0.84, 0.86, 0.88)
HEAD_FILL = (0.95, 0.96, 0.96)

# Advance widths (1/1000 em) of Helvetica / Helvetica-Bold for Windows-1252 bytes 32..255, from the Adobe core AFM files.
_WIDTHS = {
    "regular": [278,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,278,278,584,584,584,556,1015,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,278,278,278,469,556,333,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,334,260,334,584,556,556,0,222,556,333,1000,556,556,333,1000,667,333,1000,0,611,0,0,222,222,333,333,350,556,1000,333,1000,500,333,944,0,500,667,278,333,556,556,556,556,260,556,333,737,370,556,584,333,737,333,400,584,333,333,333,556,537,278,333,333,365,556,834,834,834,611,667,667,667,667,667,667,1000,722,667,667,667,667,278,278,278,278,722,722,778,778,778,778,778,584,778,722,722,722,722,667,667,611,556,556,556,556,556,556,889,500,556,556,556,556,278,278,278,278,556,556,556,556,556,556,556,584,611,556,556,556,556,500,556,500],
    "bold": [278,333,474,556,556,889,722,238,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,333,333,584,584,584,611,975,722,722,722,722,667,611,778,722,278,556,722,611,833,722,778,667,778,722,667,611,722,667,944,667,667,611,333,278,333,584,556,333,556,611,556,611,556,333,611,611,278,278,556,278,889,611,611,611,611,389,556,333,611,556,778,556,556,500,389,280,389,584,556,556,0,278,556,500,1000,556,556,333,1000,667,333,1000,0,611,0,0,278,278,500,500,350,556,1000,333,1000,556,333,944,0,500,667,278,333,556,556,556,556,280,556,333,737,370,556,584,333,737,333,400,584,333,333,333,611,556,278,333,333,365,556,834,834,834,611,722,722,722,722,722,722,1000,722,667,667,667,667,278,278,278,278,722,722,778,778,778,778,778,584,778,722,722,722,722,667,667,611,556,556,556,556,556,556,889,556,556,556,556,556,278,278,278,278,611,611,611,611,611,611,611,584,611,611,611,611,611,556,611,556],
}
_SUBST = {"≤": "<=", "≥": ">=", "→": "->", "←": "<-", "✓": "v", "✗": "x", "−": "-", " ": " ", " ": " ", "​": "", "₹": "Rs "}


def _encode(text: str) -> bytes:
    text = "".join(_SUBST.get(ch, ch) for ch in str(text).replace("\r", "").replace("\t", " "))
    return text.encode("cp1252", errors="replace")


def text_width(text: str | bytes, size: float, bold: bool = False) -> float:
    table = _WIDTHS["bold" if bold else "regular"]
    data = text if isinstance(text, bytes) else _encode(text)
    return sum(table[b - 32] if b >= 32 else 0 for b in data) * size / 1000


def wrap(text: str, size: float, width: float, bold: bool = False) -> list[str]:
    """Greedy word wrap; keeps explicit line breaks; splits words longer than a line."""
    lines: list[str] = []
    for paragraph in str(text or "").split("\n"):
        words, line = paragraph.split(" "), ""
        for word in words:
            candidate = f"{line} {word}" if line else word
            if text_width(candidate, size, bold) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while text_width(word, size, bold) > width:  # a single over-long token (URL, code)
                cut = len(word)
                while cut > 1 and text_width(word[:cut], size, bold) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _esc(data: bytes) -> bytes:
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Pdf:
    """Builds the page content streams top-down; call bytes() at the end."""

    def __init__(self, title: str, subtitle: str = "", right: str = "", footer: str = "") -> None:
        self.title, self.subtitle, self.right, self.footer = title, subtitle, right, footer
        self.pages: list[list[bytes]] = []
        self.content_w = PAGE_W - 2 * MARGIN_X
        self._new_page()

    # ------------------------------------------------------------------ primitives
    def _op(self, s: str | bytes) -> None:
        self.ops.append(s.encode("latin-1") if isinstance(s, str) else s)

    def _text(self, x: float, y: float, text: str, size: float, bold: bool = False, color=INK) -> None:
        self._op(f"BT /{'F2' if bold else 'F1'} {size:.2f} Tf {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg {x:.2f} {y:.2f} Td (")
        self._op(_esc(_encode(text)))
        self._op(") Tj ET\n")

    def _line(self, x1: float, y1: float, x2: float, y2: float, color=RULE, width: float = 0.6) -> None:
        self._op(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S\n")

    def _rect(self, x: float, y: float, w: float, h: float, color) -> None:
        self._op(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f\n")

    def _new_page(self) -> None:
        self.ops: list[bytes] = []
        self.pages.append(self.ops)
        top = PAGE_H - MARGIN_TOP
        if len(self.pages) == 1:
            self._text(MARGIN_X, top - 16, self.title, 18, bold=True)
            if self.right:
                self._text(PAGE_W - MARGIN_X - text_width(self.right, 9), top - 12, self.right, 9, color=MUTED)
            if self.subtitle:
                self._text(MARGIN_X, top - 32, self.subtitle, 9.5, color=MUTED)
            self._line(MARGIN_X, top - 42, PAGE_W - MARGIN_X, top - 42, ACCENT, 1.4)
            self.y = top - 58
        else:
            self._text(MARGIN_X, top - 8, self.title, 9, bold=True, color=MUTED)
            self._line(MARGIN_X, top - 14, PAGE_W - MARGIN_X, top - 14, RULE)
            self.y = top - 30

    def _need(self, height: float) -> None:
        if self.y - height < MARGIN_BOTTOM:
            self._new_page()

    # ------------------------------------------------------------------ blocks
    def space(self, h: float = 6) -> None:
        self.y -= h

    def heading(self, text: str) -> None:
        self._need(34)
        self.y -= 8
        self._text(MARGIN_X, self.y - 9, text.upper(), 8.5, bold=True, color=ACCENT)
        self.y -= 17

    def para(self, text: str, size: float = 10, bold: bool = False, color=INK, indent: float = 0, leading: float | None = None) -> None:
        leading = leading or size * 1.38
        for line in wrap(text, size, self.content_w - indent, bold):
            self._need(leading)
            self._text(MARGIN_X + indent, self.y - size, line, size, bold, color)
            self.y -= leading
        self.y -= 3

    def kv(self, pairs: Sequence[tuple[str, str]], cols: int = 4) -> None:
        """Label above value, in a grid (e.g. patient details)."""
        col_w = self.content_w / cols
        for start in range(0, len(pairs), cols):
            row = pairs[start:start + cols]
            wrapped = [wrap(str(v or "-"), 10, col_w - 8) for _, v in row]
            height = 12 + 13 * max(len(w) for w in wrapped) + 4
            self._need(height)
            for i, ((label, _), lines) in enumerate(zip(row, wrapped)):
                x = MARGIN_X + i * col_w
                self._text(x, self.y - 7, label.upper(), 7, bold=True, color=MUTED)
                for j, line in enumerate(lines):
                    self._text(x, self.y - 20 - 13 * j, line, 10)
            self.y -= height

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[str]], fractions: Sequence[float] | None = None, size: float = 9) -> None:
        rows = [[str(c if c not in (None, "") else "-") for c in r] for r in rows]
        n = len(headers)
        fractions = fractions or [1 / n] * n
        total = sum(fractions)
        widths = [self.content_w * f / total for f in fractions]
        pad, leading = 4.0, size * 1.3

        def draw_header() -> None:
            h = leading + 2 * pad
            self._need(h + leading + 2 * pad)
            self._rect(MARGIN_X, self.y - h, self.content_w, h, HEAD_FILL)
            x = MARGIN_X
            for head, w in zip(headers, widths):
                self._text(x + pad, self.y - pad - size, head, size - 0.5, bold=True, color=MUTED)
                x += w
            self.y -= h

        draw_header()
        for row in rows:
            cells = [wrap(cell, size, w - 2 * pad) for cell, w in zip(row, widths)]
            h = leading * max(len(c) for c in cells) + 2 * pad
            if self.y - h < MARGIN_BOTTOM:
                self._new_page()
                draw_header()
            x = MARGIN_X
            for lines, w in zip(cells, widths):
                for j, line in enumerate(lines):
                    self._text(x + pad, self.y - pad - size - leading * j, line, size)
                x += w
            self.y -= h
            self._line(MARGIN_X, self.y, MARGIN_X + self.content_w, self.y)
        self.y -= 6

    # ------------------------------------------------------------------ output
    def bytes(self) -> bytes:
        total = len(self.pages)
        objects: list[bytes] = []
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(total))
        objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {total} >>".encode())
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        for number, ops in enumerate(self.pages, start=1):
            self.ops = list(ops)  # footer goes on a copy, so bytes() can be called again
            footer_y = MARGIN_BOTTOM - 24
            self._line(MARGIN_X, footer_y + 12, PAGE_W - MARGIN_X, footer_y + 12, RULE)
            if self.footer:
                self._text(MARGIN_X, footer_y, wrap(self.footer, 7.5, self.content_w - 70)[0], 7.5, color=MUTED)
            label = f"Page {number} of {total}"
            self._text(PAGE_W - MARGIN_X - text_width(label, 7.5), footer_y, label, 7.5, color=MUTED)
            stream = zlib.compress(b"".join(self.ops))
            page_obj = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                        f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * (number - 1)} 0 R >>").encode()
            objects.append(page_obj)
            objects.append(f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream")
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)
