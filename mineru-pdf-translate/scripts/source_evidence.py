"""Pair MinerU OCR with crops from the input PDF, never reconstructed formulas."""
from __future__ import annotations

import base64
import html
import json
import re
from collections import defaultdict, deque
from html.parser import HTMLParser
from pathlib import Path


TABLE_PATTERN = r"<table\b[^>]*>[\s\S]*?</table>"


def normalize(value: str) -> str:
    value = re.sub(r"<eq>([\s\S]*?)</eq>", r"$\1$", value)
    return re.sub(r"\s+", "", value)


def walk_spans(block: dict):
    for line in block.get("lines", []):
        yield from line.get("spans", [])
    for child in block.get("blocks", []):
        yield from walk_spans(child)


def build_evidence(pdf_path: Path, markdown_path: Path) -> dict:
    """Build in-memory lookup queues in PDF reading order. Assets are resumable."""
    import fitz

    layout_path = markdown_path.parent / "layout.json"
    if not layout_path.exists():
        raise ValueError("Source correction requires MinerU layout.json; use --ocr-correction off for legacy parses.")
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    assets = markdown_path.parent / "source_assets"
    assets.mkdir(exist_ok=True)
    equations = defaultdict(deque)
    tables = deque()
    figures = {}
    with fitz.open(pdf_path) as doc:
        serial = 0

        def crop(page_idx, bbox, kind, page_size):
            nonlocal serial
            page = doc[page_idx]
            sx, sy = page.rect.width / page_size[0], page.rect.height / page_size[1]
            rect = fitz.Rect(bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy)
            # Include equation numbers when they occupy a separate text span.
            # Only same-line short numeric labels are included, never an entire column.
            if kind == "display":
                for word in page.get_text("words"):
                    if (re.fullmatch(r"\(\d+[a-z]?\)", word[4])
                            and word[0] >= rect.x1 - 2
                            and abs((word[1] + word[3]) / 2 - (rect.y0 + rect.y1) / 2) < max(10, rect.height / 2)):
                        # Do not cross into a second column.
                        if word[0] - rect.x1 < 85:
                            rect |= fitz.Rect(word[:4])
                            break
            rect = (rect + (-1.5, -1.2, 1.5, 1.2)) & page.rect
            if rect.is_empty:
                raise ValueError("MinerU returned an empty source bounding box")
            name = f"{serial:05d}_p{page_idx + 1}_{kind}.png"
            serial += 1
            path = assets / name
            page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=rect, alpha=False).save(path)
            url = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
            relpath = "source_assets/" + name
            width, height = round(rect.width * 1.2, 2), round(rect.height * 1.2, 2)
            klass = "formula-inline" if kind == "inline" else "source-block"
            fallback = (f'<img class="{klass}" src="{relpath}" width="{width}" height="{height}" '
                        f'alt="Original {kind}, page {page_idx + 1}">')
            if kind != "inline":
                fallback = f'<div class="source-block">{fallback}</div>'
            return {"kind": kind, "page": page_idx + 1, "image_url": url, "fallback": fallback}

        for info in layout.get("pdf_info", []):
            idx = int(info["page_idx"])
            size = info.get("page_size", [doc[idx].rect.width, doc[idx].rect.height])
            for block in info.get("preproc_blocks", []):
                # The table body is one protected unit; formulas inside it are not
                # processed again or incorrectly paired with subsequent occurrences.
                if block.get("type") == "table":
                    bodies = [b for b in block.get("blocks", []) if b.get("type") == "table_body"]
                    if bodies:
                        rect = fitz.Rect(bodies[0]["bbox"])
                        for body in bodies[1:]:
                            rect |= fitz.Rect(body["bbox"])
                        entry = crop(idx, list(rect), "table", size)
                        table_html = "".join(s.get("html", "") for b in bodies for s in walk_spans(b))
                        entry["original"] = table_html
                        tables.append(entry)
                    # Captions can contain math outside the table body.
                    spans = [s for b in block.get("blocks", []) if b.get("type") != "table_body" for s in walk_spans(b)]
                else:
                    spans = list(walk_spans(block))
                for span in spans:
                    if span.get("type") not in ("inline_equation", "interline_equation"):
                        continue
                    kind = "display" if span["type"] == "interline_equation" else "inline"
                    entry = crop(idx, span["bbox"], kind, size)
                    entry["original"] = span.get("content", "")
                    equations[(kind, normalize(entry["original"]))].append(entry)
                # Group multi-panel images belonging to one layout figure.
                if block.get("type") == "image":
                    image_spans = [s for b in block.get("blocks", []) if b.get("type") == "image_body"
                                   for s in walk_spans(b) if s.get("type") == "image"]
                    if len(image_spans) > 1:
                        rect = fitz.Rect(image_spans[0]["bbox"])
                        for s in image_spans[1:]:
                            rect |= fitz.Rect(s["bbox"])
                        entry = crop(idx, list(rect), "figure", size)
                        names = [Path(s["image_path"]).name for s in image_spans]
                        figures[names[0]] = (names, entry["fallback"])
    return {"equations": equations, "tables": tables, "figures": figures}


def formula_parts(value: str) -> tuple[str, str]:
    if value.startswith("$$"):
        return "display", value[2:-2].strip()
    if value.startswith(r"\["):
        return "display", value[2:-2].strip()
    if value.startswith(r"\("):
        return "inline", value[2:-2].strip()
    return "inline", value[1:-1].strip()


def pair_placeholders(placeholders: dict[str, str], evidence: dict) -> dict[str, dict]:
    paired = {}
    for token, value in placeholders.items():
        if re.fullmatch(TABLE_PATTERN, value, flags=re.I):
            # Match table HTML when possible; never blindly pair a missing table
            # by ordinal and silently use the wrong source data.
            found = next((e for e in evidence["tables"] if normalize(e["original"]) == normalize(value)), None)
            if found is None:
                raise ValueError("Could not pair an OCR table with the original PDF")
            evidence["tables"].remove(found)
        elif value.startswith(("$", r"\[", r"\(")):
            kind, tex = formula_parts(value)
            queue = evidence["equations"].get((kind, normalize(tex)))
            if not queue:
                raise ValueError("Could not pair an OCR formula with the original PDF; source comparison is required.")
            found = queue.popleft()
        else:
            continue
        paired[token] = dict(found, id=token, original=value)
    return paired


def group_figures(source: str, evidence: dict) -> str:
    pattern = r"!\[[^\]]*\]\(([^)\n]+)\)"
    matches = list(re.finditer(pattern, source))
    replacements = []
    for i, match in enumerate(matches):
        group = evidence["figures"].get(Path(match.group(1)).name)
        if not group:
            continue
        names, fallback = group
        selected = matches[i:i + len(names)]
        if [Path(m.group(1)).name for m in selected] != names:
            continue
        # Do not swallow captions or other text between the panels.
        if any(source[a.end():b.start()].strip() for a, b in zip(selected, selected[1:])):
            continue
        replacements.append((match.start(), selected[-1].end(), fallback))
    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def corrected_formula(entry: dict, value: str) -> str:
    tag_pattern = r"\\tag\s*\{([^{}]+)\}"
    if re.findall(tag_pattern, entry["original"]) != re.findall(tag_pattern, value):
        raise ValueError("Correction changed an equation number")
    kind, tex = formula_parts(value)
    if kind != entry["kind"] or not tex or re.search(r"@@PDF_TRANSLATE_|!\[", value):
        raise ValueError("Correction is not a safe formula of the original kind")
    if kind == "display" and not (value.startswith("$$") and value.endswith("$$") or value.startswith(r"\[") and value.endswith(r"\]")):
        raise ValueError("Display correction lacks math delimiters")
    if kind == "inline" and not (value.startswith("$") and value.endswith("$") or value.startswith(r"\(") and value.endswith(r"\)")):
        raise ValueError("Inline correction lacks math delimiters")
    # MathJax syntax failures can fall back to the source crop during rendering.
    fallback = html.escape(entry["fallback"], quote=True)
    tag = "div" if kind == "display" else "span"
    return f'<{tag} class="source-formula" data-source-fallback="{fallback}">{value}</{tag}>'


def safe_table(value: str) -> str:
    class TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.cells = 0

        def handle_starttag(self, tag, attrs):
            if tag not in {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "b", "strong", "i", "em", "sup", "sub", "br"}:
                raise ValueError("Unsafe tag in corrected table")
            for name, val in attrs:
                if name not in {"rowspan", "colspan"} or not val or not val.isdigit() or not 1 <= int(val) <= 200:
                    raise ValueError("Unsafe attribute in corrected table")
            if tag in {"td", "th"}:
                self.cells += 1
            if tag != "br":
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if not self.stack or self.stack.pop() != tag:
                raise ValueError("Unbalanced corrected table")

        def handle_startendtag(self, tag, attrs):
            if tag != "br" or attrs:
                raise ValueError("Unsafe element in corrected table")

        def handle_entityref(self, name):
            pass

    if not re.fullmatch(TABLE_PATTERN, value.strip(), flags=re.I) or "@@PDF_TRANSLATE_" in value:
        raise ValueError("Correction is not a complete table")
    parser = TableParser()
    parser.feed(value)
    if parser.stack or not parser.cells:
        raise ValueError("Incomplete corrected table")
    return value
