"""Render broker reports to PDF with a diagonal SYNTHETIC watermark (design §14.7). Needs `reportlab`."""
from __future__ import annotations

import os
import re

from seed.gen_docs.text import WATERMARK


def render_pdf(path: str, title: str, body: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()

    def wm(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 42)
        canvas.setFillColorRGB(0.85, 0.85, 0.85)
        canvas.translate(A4[0] / 2, A4[1] / 2)
        canvas.rotate(45)
        canvas.drawCentredString(0, 0, WATERMARK)
        canvas.restoreState()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = SimpleDocTemplate(path, pagesize=A4, title=title)
    flow = [Paragraph(title, styles["Title"]), Spacer(1, 12)]
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para or para.startswith("|"):
            continue
        para = para.replace("&", "&amp;").replace("<", "&lt;")
        if para.startswith("#"):
            flow.append(Paragraph(para.lstrip("# ").strip(), styles["Heading2"]))
        else:
            flow.append(Paragraph(para, styles["BodyText"]))
        flow.append(Spacer(1, 6))
    doc.build(flow, onFirstPage=wm, onLaterPages=wm)
