"""Deterministic renderers: WrittenDocument -> file bytes in the requested format.

Every output carries a visible "synthetic document" marker so generated reports (e.g. medical ones)
can never be mistaken for real records.
"""
from __future__ import annotations

import html
import json
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from ..contracts.docs import WrittenDocument

FONT_DIR = Path(__file__).parent / "fonts"
SYNTHETIC_NOTE = "SYNTETICKÝ DOKUMENT – vygenerováno nástrojem Story2Data, neobsahuje skutečné osobní údaje."
EXT = {"pdf": "pdf", "html": "html", "md": "md", "txt": "txt", "json": "json", "eml": "eml"}
MIME = {"pdf": "application/pdf", "html": "text/html; charset=utf-8", "md": "text/markdown; charset=utf-8",
        "txt": "text/plain; charset=utf-8", "json": "application/json", "eml": "message/rfc822"}


def doc_text(doc: WrittenDocument) -> str:
    """Plain text of the document (used for checks, txt/eml output and previews)."""
    out = [doc.title, ""]
    for s in doc.sections:
        out.append(s.title.upper())
        if s.body:
            out.append(s.body)
        out += [f"• {i}" for i in s.items]
        out += [f"{p[0]}: {p[1]}" for p in s.pairs if len(p) >= 2]
        out += [" | ".join(r) for r in s.rows]
        out.append("")
    return "\n".join(out).strip()


def _markdown(doc: WrittenDocument) -> str:
    out = [f"# {doc.title}", ""]
    for s in doc.sections:
        out += [f"## {s.title}", ""]
        if s.body:
            out += [s.body, ""]
        if s.items:
            out += [f"- {i}" for i in s.items] + [""]
        if s.pairs:
            out += [f"- **{p[0]}:** {p[1]}" for p in s.pairs if len(p) >= 2] + [""]
        if s.rows:
            head, *body = s.rows
            out += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
            out += ["| " + " | ".join(r) + " |" for r in body] + [""]
    out.append(f"_{SYNTHETIC_NOTE}_")
    return "\n".join(out)


def _html(doc: WrittenDocument) -> str:
    e = html.escape
    parts = [f"<!doctype html><html lang='cs'><head><meta charset='utf-8'><title>{e(doc.title)}</title><style>"
             "body{font-family:system-ui,sans-serif;max-width:780px;margin:32px auto;color:#191919;line-height:1.5}"
             "h1{font-size:22px;border-bottom:2px solid #257de0;padding-bottom:6px}h2{font-size:16px;margin-top:22px}"
             "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:4px 8px;text-align:left}"
             "th{background:#f3f6fa}.kv td:first-child{font-weight:600;width:35%}"
             ".note{margin-top:32px;font-size:11px;color:#666;border-top:1px solid #ddd;padding-top:8px}</style></head><body>",
             f"<h1>{e(doc.title)}</h1>"]
    for s in doc.sections:
        parts.append(f"<h2>{e(s.title)}</h2>")
        if s.body:
            parts += [f"<p>{e(p)}</p>" for p in s.body.split("\n") if p.strip()]
        if s.items:
            parts.append("<ul>" + "".join(f"<li>{e(i)}</li>" for i in s.items) + "</ul>")
        if s.pairs:
            parts.append("<table class='kv'>" + "".join(f"<tr><td>{e(p[0])}</td><td>{e(p[1])}</td></tr>"
                                                          for p in s.pairs if len(p) >= 2) + "</table>")
        if s.rows:
            head, *body = s.rows
            parts.append("<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr>"
                         + "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in body) + "</table>")
    parts.append(f"<div class='note'>{e(SYNTHETIC_NOTE)}</div></body></html>")
    return "".join(parts)


def _pdf(doc: WrittenDocument, meta: dict[str, Any]) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    class PDF(FPDF):
        def footer(self):
            self.set_y(-14)
            self.set_font("DejaVu", size=7)
            self.set_text_color(110, 110, 110)
            self.cell(0, 5, f"{SYNTHETIC_NOTE}   ·   strana {self.page_no()}/{{nb}}", align="C")

    pdf = PDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_font("DejaVu", "", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
    pdf.set_title(doc.title)
    pdf.set_author("Story2Data (synthetic)")
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 9)
    pdf.set_text_color(37, 125, 224)
    pdf.cell(0, 5, str(meta.get("document_type", "")).upper(), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(25, 25, 25)
    pdf.set_font("DejaVu", "B", 16)
    pdf.multi_cell(0, 8, doc.title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(37, 125, 224)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y() + 1, pdf.w - pdf.r_margin, pdf.get_y() + 1)
    pdf.ln(4)
    pdf.set_draw_color(200, 205, 212)
    pdf.set_line_width(0.2)
    for s in doc.sections:
        pdf.set_font("DejaVu", "B", 11)
        pdf.multi_cell(0, 6, s.title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 10)
        if s.body:
            pdf.multi_cell(0, 5, s.body, new_x="LMARGIN", new_y="NEXT")
        for i in s.items:
            pdf.multi_cell(0, 5, f"•  {i}", new_x="LMARGIN", new_y="NEXT")
        rows = [p[:2] for p in s.pairs if len(p) >= 2]
        if rows:
            with pdf.table(col_widths=(35, 65), first_row_as_headings=False, line_height=5.5,
                           borders_layout="HORIZONTAL_LINES") as t:
                for r in rows:
                    row = t.row()
                    row.cell(r[0], style=FontFace(emphasis="BOLD"))
                    row.cell(r[1])
        if s.rows:
            width = max(len(r) for r in s.rows)
            norm = [list(r) + [""] * (width - len(r)) for r in s.rows]
            with pdf.table(line_height=5.5, headings_style=FontFace(emphasis="BOLD", fill_color=(235, 242, 250))) as t:
                for r in norm:
                    row = t.row()
                    for c in r:
                        row.cell(str(c))
        pdf.ln(3)
    return bytes(pdf.output())


def _eml(doc: WrittenDocument, meta: dict[str, Any]) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = doc.email_subject or doc.title
    msg["From"] = doc.email_from or "noreply@example.invalid"
    msg["To"] = doc.email_to or "recipient@example.invalid"
    if meta.get("date"):
        msg["Date"] = format_datetime(meta["date"])
    msg["X-Synthetic"] = "Story2Data"
    msg.set_content(doc_text(doc) + "\n\n-- \n" + SYNTHETIC_NOTE)
    return bytes(msg)


def render(doc: WrittenDocument, fmt: str, meta: dict[str, Any], facts: dict[str, Any]) -> bytes:
    if fmt == "pdf":
        return _pdf(doc, meta)
    if fmt == "html":
        return _html(doc).encode()
    if fmt == "md":
        return _markdown(doc).encode()
    if fmt == "txt":
        return (doc_text(doc) + "\n\n" + SYNTHETIC_NOTE + "\n").encode()
    if fmt == "json":
        return json.dumps({"synthetic": True, "note": SYNTHETIC_NOTE, "facts": facts, "document": doc.model_dump()},
                          ensure_ascii=False, indent=1, default=str).encode()
    if fmt == "eml":
        return _eml(doc, meta)
    raise ValueError(f"unknown format {fmt}")


def preview_html(doc: WrittenDocument) -> str:
    return _html(doc)
