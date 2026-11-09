"""Turn a .docx into Markdown-ish text, keeping paragraphs and tables in document order."""
import io

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "/")


def docx_to_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    out: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            style = (block.style.name or "").lower() if block.style is not None else ""
            if style.startswith("heading"):
                digits = "".join(ch for ch in style if ch.isdigit()) or "1"
                out.append("#" * min(int(digits), 6) + " " + text)
            elif style == "title":
                out.append("# " + text)
            elif "list" in style:
                out.append("- " + text)
            else:
                out.append(text)
        elif isinstance(block, Table):
            rows = [[_cell(c.text) for c in r.cells] for r in block.rows]
            if not rows:
                continue
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("|" + " --- |" * width)
            out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
        out.append("")
    text = "\n".join(out).strip()
    if not text:
        raise ValueError("The document has no readable text.")
    return text
