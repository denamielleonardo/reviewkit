"""Apply edits to a .docx as Word tracked changes (author "ReviewKit").

Blocks are addressed by stable IDs computed in document order:
  P<n>            body paragraph
  T<n>            table
  T<n>.R<r>       table row
  T<n>.R<r>.C<c>  table cell (its paragraphs are edited as one unit)
"""
import copy
import datetime as _dt
import io
import itertools

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

AUTHOR = "ReviewKit"


class EditError(Exception):
    pass


# ---------- addressing ----------

def _index(doc):
    """Return ordered blocks and an id -> object map."""
    blocks, ids = [], {}
    p_n = t_n = 0
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            p_n += 1
            pid = f"P{p_n}"
            ids[pid] = block
            blocks.append((pid, block))
        elif isinstance(block, Table):
            t_n += 1
            tid = f"T{t_n}"
            ids[tid] = block
            blocks.append((tid, block))
            for r, row in enumerate(block.rows, 1):
                ids[f"{tid}.R{r}"] = row
                seen = set()
                for c, cell in enumerate(row.cells, 1):
                    if id(cell._tc) in seen:  # merged cells repeat
                        continue
                    seen.add(id(cell._tc))
                    ids[f"{tid}.R{r}.C{c}"] = cell
    return blocks, ids


def _style(p: Paragraph) -> str:
    try:
        return p.style.name if p.style is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def outline(data: bytes) -> str:
    """Document as ID-tagged text, for Claude to reference when proposing edits."""
    doc = Document(io.BytesIO(data))
    blocks, _ = _index(doc)
    lines = []
    for bid, b in blocks:
        if isinstance(b, Paragraph):
            text = b.text.strip()
            if not text:
                lines.append(f"[{bid}] (empty)")
                continue
            style = _style(b)
            lines.append(f"[{bid}]" + (f" ({style})" if style and style != "Normal" else "") + f" {text}")
        else:
            lines.append(f"[{bid}] TABLE")
            for r, row in enumerate(b.rows, 1):
                seen, cells = set(), []
                for c, cell in enumerate(row.cells, 1):
                    if id(cell._tc) in seen:
                        continue
                    seen.add(id(cell._tc))
                    cells.append(f"[{bid}.R{r}.C{c}] {' '.join(cell.text.split())}")
                lines.append(f"  [{bid}.R{r}] " + " | ".join(cells))
    return "\n".join(lines)


def used_styles(data: bytes) -> list[str]:
    """Paragraph styles the document actually uses: the template new content must follow."""
    doc = Document(io.BytesIO(data))
    names = []
    for p in doc.paragraphs:
        n = _style(p)
        if n and n not in names:
            names.append(n)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    n = _style(p)
                    if n and n not in names:
                        names.append(n)
    return names


# ---------- tracked-change primitives ----------

class _Track:
    def __init__(self):
        self.ids = itertools.count(91000)
        self.date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def mark(self, tag: str):
        el = OxmlElement(tag)
        el.set(qn("w:id"), str(next(self.ids)))
        el.set(qn("w:author"), AUTHOR)
        el.set(qn("w:date"), self.date)
        return el

    def ins_run(self, text: str, rpr=None):
        ins = self.mark("w:ins")
        r = OxmlElement("w:r")
        if rpr is not None:
            r.append(copy.deepcopy(rpr))
        t = OxmlElement("w:t")
        t.set(qn("xml:space"), "preserve")
        t.text = text
        r.append(t)
        ins.append(r)
        return ins

    def delete_runs(self, p_el) -> None:
        """Wrap every run of a paragraph in <w:del> and turn its text into delText."""
        for r in [c for c in p_el if c.tag == qn("w:r")]:
            for t in r.findall(qn("w:t")):
                t.tag = qn("w:delText")
            d = self.mark("w:del")
            r.addprevious(d)
            d.append(r)

    def mark_para(self, p_el, tag: str) -> None:
        """Mark the paragraph mark itself inserted/deleted (needed for whole-paragraph changes)."""
        ppr = p_el.find(qn("w:pPr"))
        if ppr is None:
            ppr = OxmlElement("w:pPr")
            p_el.insert(0, ppr)
        rpr = ppr.find(qn("w:rPr"))
        if rpr is None:
            rpr = OxmlElement("w:rPr")
            ppr.append(rpr)
        rpr.append(self.mark(tag))


def _first_rpr(p_el):
    r = p_el.find(qn("w:r"))
    return r.find(qn("w:rPr")) if r is not None else None


def _style_id(doc, name: str | None) -> str | None:
    if not name:
        return None
    for s in doc.styles:
        if s.name and s.name.lower() == name.lower():
            return s.style_id
    return None


# ---------- operations ----------

def _replace_paragraph(tr: _Track, p_el, text: str) -> None:
    rpr = _first_rpr(p_el)
    tr.delete_runs(p_el)
    p_el.append(tr.ins_run(text, rpr))


def _sample_of_style(doc, style_id: str):
    """An existing paragraph using this style, to copy its exact formatting (template)."""
    for p in doc.element.body.iter(qn("w:p")):
        ps = p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        if ps is not None and ps.get(qn("w:val")) == style_id and p.find(qn("w:r")) is not None:
            return p
    return None


def _new_paragraph_after(tr: _Track, anchor_el, text: str, ref_p_el):
    """New paragraph formatted exactly like ref_p_el (paragraph + first-run formatting)."""
    p = OxmlElement("w:p")
    ref_ppr = ref_p_el.find(qn("w:pPr")) if ref_p_el is not None else None
    if ref_ppr is not None:
        ppr = copy.deepcopy(ref_ppr)
        for old in ppr.findall(qn("w:rPr")) + ppr.findall(qn("w:sectPr")):
            ppr.remove(old)
        p.append(ppr)
    tr.mark_para(p, "w:ins")
    p.append(tr.ins_run(text, _first_rpr(ref_p_el) if ref_p_el is not None else None))
    anchor_el.addnext(p)
    return p


def apply_edits(data: bytes, edits: list[dict]) -> tuple[bytes, list[dict]]:
    """Apply edits as tracked changes; returns (new docx bytes, per-edit results).

    New paragraphs only ever use styles the document already uses, copying the
    formatting of an existing paragraph of that style; otherwise they copy the
    neighbouring paragraph. Nothing outside the edited elements is touched.
    """
    allowed = {s.lower() for s in used_styles(data)}
    doc = Document(io.BytesIO(data))
    _, ids = _index(doc)
    tr = _Track()
    results = []
    last_inserted: dict[str, object] = {}  # keeps repeated inserts after one target in order
    for e in edits:
        op, target = e.get("op"), (e.get("target") or "").strip()
        text = (e.get("text") or "").strip()
        try:
            obj = ids.get(target)
            if obj is None:
                raise EditError(f"unknown target {target!r}")
            wanted = (e.get("style") or "").strip()
            style_id = _style_id(doc, wanted) if wanted.lower() in allowed else None

            if op == "replace":
                if not text:
                    raise EditError("replace needs text")
                if isinstance(obj, Paragraph):
                    _replace_paragraph(tr, obj._p, text)
                elif hasattr(obj, "_tc"):  # cell
                    paras = obj.paragraphs
                    _replace_paragraph(tr, paras[0]._p, text)
                    for extra in paras[1:]:
                        tr.delete_runs(extra._p)
                        tr.mark_para(extra._p, "w:del")
                else:
                    raise EditError("replace targets a paragraph or a cell")

            elif op == "insert_after":
                if not text:
                    raise EditError("insert_after needs text")
                if isinstance(obj, Paragraph):
                    anchor, ref = obj._p, obj._p
                elif isinstance(obj, Table):
                    anchor, ref = obj._tbl, None
                elif hasattr(obj, "_tc"):
                    anchor = ref = obj.paragraphs[-1]._p
                else:
                    raise EditError("insert_after targets a paragraph, table or cell")
                if style_id:
                    sample = _sample_of_style(doc, style_id)
                    if sample is not None:
                        ref = sample
                if ref is None:  # inserting after a table: copy the paragraph before it
                    prev = anchor.getprevious()
                    ref = prev if prev is not None and prev.tag == qn("w:p") else None
                anchor = last_inserted.get(target, anchor)
                for line in [ln.strip() for ln in text.split("\n") if ln.strip()]:
                    anchor = _new_paragraph_after(tr, anchor, line, ref)
                last_inserted[target] = anchor

            elif op == "delete":
                if not isinstance(obj, Paragraph):
                    raise EditError("delete targets a paragraph")
                tr.delete_runs(obj._p)
                tr.mark_para(obj._p, "w:del")

            elif op == "add_row":
                row = obj if hasattr(obj, "_tr") else (obj.rows[-1] if isinstance(obj, Table) else None)
                if row is None:
                    raise EditError("add_row targets a table or a row")
                cells = e.get("cells") or []
                new_tr = copy.deepcopy(row._tr)
                trpr = new_tr.find(qn("w:trPr"))
                if trpr is None:
                    trpr = OxmlElement("w:trPr")
                    new_tr.insert(0, trpr)
                trpr.append(tr.mark("w:ins"))
                for i, tc in enumerate(new_tr.findall(qn("w:tc"))):
                    ps = tc.findall(qn("w:p"))
                    for extra in ps[1:]:
                        tc.remove(extra)
                    p = ps[0]
                    rpr = _first_rpr(p)
                    for child in [c for c in p if c.tag != qn("w:pPr")]:
                        p.remove(child)
                    tr.mark_para(p, "w:ins")
                    value = str(cells[i]) if i < len(cells) else ""
                    if value:
                        p.append(tr.ins_run(value, rpr))
                row._tr.addnext(new_tr)
            else:
                raise EditError(f"unknown op {op!r}")
            results.append({**e, "ok": True})
        except EditError as err:
            results.append({**e, "ok": False, "error": str(err)})

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), results
