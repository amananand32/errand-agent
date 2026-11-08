"""Render DESIGN.md to docs/DESIGN.pdf and fail if it exceeds one page."""
import re
import sys

from reportlab import rl_config

rl_config.invariant = 1   # reproducible output (no timestamps/random ids)

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate

src = open("DESIGN.md").read()
body = ParagraphStyle("b", fontName="Helvetica", fontSize=8.1, leading=10.0, spaceAfter=3.2)
h1 = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=12, leading=14, spaceAfter=4)
code = ParagraphStyle("c", fontName="Courier", fontSize=6.9, leading=8.2, spaceAfter=3, leftIndent=6)


def inline(t):
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    codes = re.findall(r"`([^`]+)`", t)
    t = re.sub(r"`([^`]+)`", "\x00", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"\*(.+?)\*", r"<i>\1</i>", t)
    for c in codes:
        t = t.replace("\x00", f"<font face='Courier'>{c}</font>", 1)
    return t.replace("₹", "Rs ").replace("→", "-&gt;").replace("≤", "&lt;=").replace("⇒", "=&gt;").replace("←", "&lt;-")


flow, blocks = [], re.split(r"\n\n+", src.strip())
for b in blocks:
    if b.startswith("```"):
        flow.append(Preformatted("\n".join(b.strip("`").splitlines()[1:]).replace("←", "<-").replace("→", "->")
                                 .replace("…", "...").replace("✅", "[ok]").replace("₹", "Rs "), code))
    elif b.startswith("# "):
        flow.append(Paragraph(inline(b[2:]), h1))
    else:
        flow.append(Paragraph(inline(b.replace("\n", " ")), body))
pages = []
doc = SimpleDocTemplate("docs/DESIGN.pdf", pagesize=A4, leftMargin=1.4 * cm, rightMargin=1.4 * cm,
                        topMargin=1.2 * cm, bottomMargin=1.2 * cm, title="Errand Agent - design note")
doc.build(flow, onLaterPages=lambda c, d: pages.append(d.page), onFirstPage=lambda c, d: pages.append(d.page))
print(f"DESIGN.pdf pages: {len(pages)}; words: {len(src.split())}")
sys.exit(0 if len(pages) == 1 else 1)
