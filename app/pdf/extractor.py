"""Turn a PDF into reading-order blocks using PyMuPDF.

Digital papers are read from the embedded text layer (fast and exact).
Scanned pages are flagged for the local PaddleOCR-VL worker.  The extracted
JSON keeps page geometry, block type, bounding boxes, and word boxes used by
the PDF selection overlay.
"""

from __future__ import annotations

import hashlib
import html
import re
import statistics
import time
from bisect import bisect_right
from collections import Counter
from pathlib import Path
from typing import Callable

import pymupdf

from ..config import AppConfig
from ..logging_setup import get_logger
from ..models import (
    SCHEMA_VERSION,
    Block,
    DocumentStructure,
    PageData,
    Word,
    is_mostly_math,
    normalize_text,
)
from ..tables import parse_ocr_html_table, to_markdown

log = get_logger("pdf.extractor")

EXTRACTOR_VERSION = 9

ProgressFn = Callable[[str, int, int, str], None]

_CAPTION_RE = re.compile(
    r"^\s*(?:fig(?:ure)?\.?|tab(?:le)?\.?|图|表|scheme|algorithm)\s*[A-Za-z]?\d+"
    r"(?:[.\-]\d+)?(?:\s*[:：.\-–—|]\s*|\s+(?=(?-i:[A-Z\u4e00-\u9fff])))",
    re.IGNORECASE,
)
_TABLE_CAPTION_RE = re.compile(r"^\s*(?:tab(?:le)?\.?|表)\s*[A-Za-z]?\d+\s*[:：.\-–—]", re.I)
_OCR_HTML_TABLE_RE = re.compile(r"(<table\b[^>]*>.*?</table\s*>)", re.I | re.S)
_HEADING_NUM_RE = re.compile(r"^\s*(?:\d+(?:\.\d+){0,3}|[IVX]+\.)\s+[A-Z\u4e00-\u9fff]")
_KNOWN_HEADINGS = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "method",
    "methods",
    "methodology",
    "approach",
    "experiments",
    "experimental results",
    "results",
    "discussion",
    "limitations",
    "conclusion",
    "conclusions",
    "references",
    "bibliography",
    "acknowledgements",
    "acknowledgments",
    "appendix",
    "摘要",
    "引言",
    "相关工作",
    "方法",
    "实验",
    "结果",
    "讨论",
    "结论",
    "参考文献",
    "致谢",
}
_MATH_CHARS = set("=+-*/^_{}[]()<>∑∫√∞≈≠≤≥±×÷·αβγδεζηθλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ")
_EQUATION_OPERATORS = set("=+-−*/^_<>∑∏∫√≈≠≡≤≥±×÷∈∪∥⊙→⇒·")
_MATH_FONT_HINTS = (
    "cmmi",
    "cmsy",
    "cmex",
    "txex",
    "txmi",
    "txsy",
    "symbol",
    "math",
    "stix",
    "xits",
    "asana",
    "euler",
    "latinmodern-math",
    "cambria math",
)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def extract_document(
    path: Path,
    config: AppConfig,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    known_hash: str = "",
    asset_dir: Path | None = None,
) -> DocumentStructure:
    started = time.time()
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    digest = known_hash or hash_file(path)
    doc = pymupdf.open(str(path))
    try:
        if doc.needs_pass:
            raise RuntimeError("PDF 已加密，当前版本不支持带密码的 PDF。")
        page_count = doc.page_count
        if page_count <= 0:
            raise RuntimeError("PDF 没有可读取的页面。")
        _progress(progress, "extract", 0, page_count, "正在分析 PDF 页面…")

        raw_pages: list[PageData] = []
        raw_meta = doc.metadata or {}
        for index in range(page_count):
            if cancel and cancel():
                raise ExtractionCancelled()
            page = doc.load_page(index)
            page_data = _extract_page(page, index + 1, asset_dir)
            raw_pages.append(page_data)
            _progress(progress, "extract", index + 1, page_count, f"解析第 {index + 1}/{page_count} 页")

        body_size = _estimate_body_size(raw_pages)
        _remove_headers_and_footers(raw_pages, body_size)
        title_block = _pick_title(raw_pages, raw_meta)
        _classify_blocks(raw_pages, body_size, title_block)
        for page in raw_pages:
            _merge_formula_fragments(page)
            page.blocks = _layout_blocks(page.blocks, page.width)
            for order, block in enumerate(page.blocks, start=1):
                block.id = f"p{page.number}_b{order}"
                block.order = order
            page.words = _order_words(page.words, page.blocks)
        _mark_references(raw_pages)
        if asset_dir is not None:
            _extract_region_assets(doc, raw_pages, asset_dir)
        title = _document_title(raw_pages, raw_meta, path)
        authors = str(raw_meta.get("author") or "").strip()
        ocr_pages = _pages_requiring_ocr(raw_pages, config)
        for page in raw_pages:
            page.needs_ocr = page.number in ocr_pages

        structure = DocumentStructure(
            source_path=str(path),
            source_hash=digest,
            file_name=path.name,
            title=title,
            authors=authors,
            page_count=page_count,
            pages=raw_pages,
            metadata={str(k): str(v) for k, v in raw_meta.items() if v},
            extractor_version=EXTRACTOR_VERSION,
            ocr_summary={
                "required_pages": ocr_pages,
                "done_pages": [],
                "engine": "",
                "status": "pending" if ocr_pages else "not_needed",
                "elapsed": 0.0,
            },
        )
        structure.metadata["extract_seconds"] = f"{time.time() - started:.2f}"
        _progress(progress, "extract", page_count, page_count, "PDF 结构分析完成")
        log.info(
            "extracted %s: pages=%s blocks=%s ocr_pages=%s in %.2fs",
            path.name,
            page_count,
            len(structure.blocks),
            len(ocr_pages),
            time.time() - started,
        )
        return structure
    finally:
        doc.close()


class ExtractionCancelled(RuntimeError):
    pass


def _progress(fn: ProgressFn | None, phase: str, current: int, total: int, message: str) -> None:
    if fn:
        try:
            fn(phase, current, total, message)
        except Exception:
            log.debug("progress callback failed", exc_info=True)


def _extract_page(page: pymupdf.Page, page_number: int, asset_dir: Path | None = None) -> PageData:
    width = float(page.rect.width)
    height = float(page.rect.height)
    native_text = page.get_text("text") or ""
    image_infos = []
    try:
        image_infos = page.get_image_info()
    except Exception:
        image_infos = []

    image_area = 0.0
    figures: list[Block] = []
    page_area = max(width * height, 1.0)
    if asset_dir is not None:
        try:
            asset_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            asset_dir = None
    for info in image_infos:
        bbox = [float(v) for v in info.get("bbox", (0, 0, 0, 0))]
        area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
        image_area += area
        if area / page_area >= 0.02 and area / page_area <= 0.9:
            if (bbox[2] - bbox[0]) < 24 or (bbox[3] - bbox[1]) < 24:
                continue
            meta: dict[str, object] = {
                "image": True,
                "area_ratio": round(area / page_area, 3),
                "display_width": int(max(220, min(720, (bbox[2] - bbox[0]) * 1.25))),
            }
            if asset_dir is not None:
                relative = f"assets/p{page_number:03d}_fig{len(figures) + 1}.png"
                target = asset_dir.parent / relative
                try:
                    rect = pymupdf.Rect(bbox)
                    scale = min(2.2, 1800.0 / max(rect.width, rect.height, 1.0))
                    pix = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale),
                        clip=rect,
                        alpha=False,
                        colorspace=pymupdf.csRGB,
                    )
                    pix.save(str(target))
                    meta.update(
                        {
                            "asset": relative,
                            "asset_width": pix.width,
                            "asset_height": pix.height,
                        }
                    )
                except Exception:
                    log.debug("could not extract figure on page %s", page_number, exc_info=True)
            figures.append(
                Block(
                    id="",
                    page=page_number,
                    order=0,
                    kind="figure",
                    text="",
                    bbox=bbox,
                    meta=meta,
                )
            )
    image_ratio = min(1.0, image_area / page_area)

    blocks: list[Block] = []
    try:
        raw_blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT).get("blocks", [])
    except Exception:
        raw_blocks = []
    tables = _extract_tables(page, page_number, raw_blocks)
    table_boxes = [block.bbox for block in tables]

    for raw_block in raw_blocks:
        if raw_block.get("type") != 0:
            continue
        bounds = pymupdf.Rect()
        lines: list[str] = []
        sizes: list[tuple[float, int]] = []
        bold_chars = 0
        total_chars = 0
        font_names: Counter[str] = Counter()
        span_meta: list[dict[str, object]] = []
        for line_index, line in enumerate(raw_block.get("lines", [])):
            line_box = line["bbox"]
            if _center_inside_any(line_box, table_boxes):
                continue
            spans = line.get("spans", [])
            if not spans:
                continue
            pieces: list[str] = []
            for span in spans:
                text = str(span.get("text", ""))
                if not text:
                    continue
                pieces.append(text)
                size = float(span.get("size", 0.0))
                count = max(len(text.strip()), 1)
                sizes.append((size, count))
                total_chars += count
                if int(span.get("flags", 0)) & 16:
                    bold_chars += count
                font_name = str(span.get("font", "")).lower()
                font_names[font_name] += count
                is_math_font = any(hint in font_name for hint in _MATH_FONT_HINTS)
                if is_math_font and any(ch in _MATH_CHARS for ch in text):
                    span_meta.append(
                        {
                            "text": text,
                            "bbox": [round(float(v), 2) for v in span.get("bbox", (0, 0, 0, 0))],
                            "math": True,
                            "line": line_index,
                        }
                    )
            line_text = "".join(pieces).strip()
            if line_text:
                bounds |= pymupdf.Rect(line_box)
                lines.append(line_text)
        if not lines:
            continue
        text = _join_lines(lines)
        if not text:
            continue
        font_size = 0.0
        weight = sum(count for _, count in sizes) or 1
        if sizes:
            font_size = sum(size * count for size, count in sizes) / weight
        bold_ratio = bold_chars / max(total_chars, 1)
        math_font_chars = sum(
            count for name, count in font_names.items() if any(hint in name for hint in _MATH_FONT_HINTS)
        )
        block = Block(
            id="",
            page=page_number,
            order=0,
            kind="paragraph",
            text=text,
            bbox=list(bounds),
            meta={
                "font_size": round(font_size, 2),
                "bold_ratio": round(bold_ratio, 3),
                "math_font_ratio": round(math_font_chars / max(total_chars, 1), 3),
                "lines": len(lines),
                **({"spans": span_meta} if span_meta else {}),
            },
        )
        blocks.append(block)

    blocks.extend(tables)
    blocks.extend(figures)
    words = _extract_words(page, page_number)

    return PageData(
        number=page_number,
        width=width,
        height=height,
        blocks=blocks,
        words=words,
        text_chars=len(native_text.strip()),
        image_ratio=image_ratio,
    )


def _extract_region_assets(
    doc: pymupdf.Document,
    pages: list[PageData],
    asset_dir: Path,
) -> None:
    """Crop tables/formulas as lossless page images.

    Displaying the original region avoids wrong row/column reconstruction and
    wrong LaTeX conversion.  The extracted text is still retained for Q&A.
    """

    try:
        asset_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for page_data in pages:
        targets = [b for b in page_data.blocks if b.kind in {"table", "formula"}]
        if not targets:
            continue
        try:
            page = doc.load_page(page_data.number - 1)
        except Exception:
            continue
        counters = {"table": 0, "formula": 0}
        for block in targets:
            if block.meta.get("asset"):
                continue
            x0, y0, x1, y1 = block.bbox
            if x1 - x0 < 1 or y1 - y0 < 1:
                continue
            # Small single-line formulas are often only a few points high;
            # pad them so they still become an image instead of falling back
            # to KaTeX.
            pad_x = max(3.0, (12.0 - (x1 - x0)) / 2.0)
            pad_y = max(3.0, (10.0 - (y1 - y0)) / 2.0)
            merged = bool(block.meta.get("merged_formula"))
            if merged:
                # Stretched TeX glyphs can extend outside their text-layer boxes.
                pad_y = max(pad_y, 1.25 * float(block.meta.get("font_size", 0)))
            counters[block.kind] += 1
            relative = (
                f"assets/p{page_data.number:03d}_{block.kind}{counters[block.kind]}.png"
            )
            target = asset_dir.parent / relative
            try:
                top, bottom = y0 - pad_y, y1 + pad_y
                if block.kind == "formula":
                    for neighbor in page_data.blocks:
                        left, upper, right, lower = neighbor.bbox
                        if neighbor is block or min(x1, right) <= max(x0, left):
                            continue
                        if merged and neighbor.kind in {"paragraph", "heading", "title", "caption", "reference"}:
                            if lower <= y0 + pad_y / 2:
                                top = max(top, lower + 1)
                            if upper >= y1 - pad_y / 2:
                                bottom = min(bottom, upper - 1)
                            continue
                        if lower <= y0 + 4:
                            top = max(top, min(y0, lower + 1))
                        if upper >= y1 - 4:
                            bottom = min(bottom, max(y1, upper - 1))
                if bottom <= top:
                    top, bottom = y0, y1
                rect = pymupdf.Rect(x0 - pad_x, top, x1 + pad_x, bottom) & page.rect
                if rect.is_empty:
                    continue
                scale = min(3.0, 1800.0 / max(rect.width, rect.height, 1.0))
                pix = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale),
                    clip=rect,
                    alpha=False,
                    colorspace=pymupdf.csRGB,
                )
                pix.save(str(target))
                if block.kind == "table":
                    display = int(max(280, min(720, (x1 - x0) * 1.2)))
                else:
                    display = int(max(180, min(700, (x1 - x0) * 1.1)))
                block.meta.update(
                    {
                        "region_image": True,
                        "asset": relative,
                        "asset_width": pix.width,
                        "asset_height": pix.height,
                        "display_width": display,
                    }
                )
                if merged:
                    block.meta["asset_bbox"] = list(rect)
            except Exception:
                log.debug(
                    "could not extract %s region on page %s",
                    block.kind,
                    page_data.number,
                    exc_info=True,
                )


def _extract_words(page: pymupdf.Page, page_number: int) -> list[Word]:
    words: list[Word] = []
    try:
        raw_words = page.get_text("words")
    except Exception:
        return words
    for raw in raw_words:
        if len(raw) < 8:
            continue
        text = str(raw[4]).strip()
        if not text:
            continue
        bbox = [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
        line = f"{int(raw[5])}:{int(raw[6])}"
        words.append(Word(text=text, bbox=bbox, line=line, order=int(raw[7])))
    return words


def _order_words(words: list[Word], blocks: list[Block]) -> list[Word]:
    def block_for(word: Word) -> int:
        cx = (word.bbox[0] + word.bbox[2]) / 2
        cy = (word.bbox[1] + word.bbox[3]) / 2
        best_index = len(blocks) + 1
        best_area = float("inf")
        for index, block in enumerate(blocks):
            x0, y0, x1, y1 = block.bbox
            if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
                area = max(1.0, (x1 - x0) * (y1 - y0))
                if area < best_area:
                    best_area = area
                    best_index = index
        return best_index

    indexed = [(block_for(word), word) for word in words]
    indexed.sort(key=lambda item: (item[0], item[1].line, item[1].order, item[1].bbox[1], item[1].bbox[0]))
    for position, (_, word) in enumerate(indexed):
        word.order = position
    return [word for _, word in indexed]


def _join_lines(lines: list[str]) -> str:
    cleaned: list[str] = []
    for line in lines:
        value = normalize_text(line)
        if not value:
            continue
        if cleaned:
            previous = cleaned[-1]
            if previous.endswith("-") and len(previous) > 2 and value[:1].islower():
                cleaned[-1] = previous[:-1] + value
                continue
            if _is_cjk(previous[-1:]) and _is_cjk(value[:1]):
                cleaned[-1] = previous + value
                continue
        cleaned.append(value)
    text = " ".join(cleaned)
    text = re.sub(r"\s+([,.;:!?%)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return text.strip()


def _is_cjk(value: str) -> bool:
    return bool(value) and "\u3400" <= value[0] <= "\u9fff"


def _overlaps_any(bbox: list[float], boxes: list[list[float]], threshold: float = 0.3) -> bool:
    area = max(1.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
    for other in boxes:
        x0 = max(bbox[0], other[0])
        y0 = max(bbox[1], other[1])
        x1 = min(bbox[2], other[2])
        y1 = min(bbox[3], other[3])
        if x1 > x0 and y1 > y0:
            overlap = (x1 - x0) * (y1 - y0)
            if overlap / area >= threshold:
                return True
    return False


def _extract_tables(page: pymupdf.Page, number: int, raw_blocks: list[dict]) -> list[Block]:
    tables: list[Block] = []

    def collect(finder, region: pymupdf.Rect | None = None) -> bool:
        found = False
        for table in finder.tables:
            bounds = pymupdf.Rect(table.bbox)
            if region is not None:
                if bounds.width < region.width * 0.75 or bounds.height < region.height * 0.65:
                    continue
                data = _table_rows_from_words(page, table, region)
                bounds = region
            else:
                data = table.extract()
            rows = [row for row in data if any(str(cell or "").strip() for cell in row)]
            cols = max((len(row) for row in rows), default=0)
            if not any(ch.isalnum() for row in rows for cell in row for ch in str(cell or "")):
                continue
            if region is not None and (len(rows) < 2 or cols < 2):
                continue
            found = True
            bbox = list(bounds)
            existing = [old for old in tables if _overlaps_any(old.bbox, [bbox], 0.65)]
            if any(
                region is None
                or cols < old.meta["cols"]
                or (cols == old.meta["cols"] and len(rows) <= old.meta["rows"])
                for old in existing
            ):
                continue
            meta = {"rows": len(rows), "cols": cols}
            grid = _table_grid(table, data)
            if grid is not None:
                meta["table"] = grid
            for old in existing:
                tables.remove(old)
            tables.append(Block("", number, 0, "table", _table_to_markdown(rows), bbox, meta=meta))
        return found

    try:
        paths = page.get_drawings()
        collect(page.find_tables(paths=paths))
        for region in _captioned_table_regions(paths, raw_blocks):
            clip = (region + (-2, -1, 2, 1)) & page.rect
            if not collect(page.find_tables(clip=clip, strategy="text", paths=paths), region):
                collect(page.find_tables(clip=clip, strategy="text", min_words_vertical=2, paths=paths), region)
    except Exception:
        log.debug("table detection failed on page %s", number, exc_info=True)
    return tables


def _table_rows_from_words(page: pymupdf.Page, table, region: pymupdf.Rect) -> list[list[str]]:
    """Assign whole words to the inferred grid, including wide header labels."""
    x_edges = sorted({round(cell[0], 2) for cell in table.cells if cell})[1:]
    y_edges = [row.bbox[1] for row in table.rows][1:]
    rows = [[[] for _ in range(len(x_edges) + 1)] for _ in range(len(y_edges) + 1)]
    for x0, y0, x1, y1, word, *_ in page.get_text("words", clip=region, sort=True):
        row = bisect_right(y_edges, (y0 + y1) / 2)
        col = bisect_right(x_edges, (x0 + x1) / 2)
        rows[row][col].append(word)
    return [[" ".join(cell) for cell in row] for row in rows]


def _captioned_table_regions(paths: list[dict], raw_blocks: list[dict]) -> list[pymupdf.Rect]:
    """Bound three-line tables using collinear rules near a numbered caption."""
    captions = [
        pymupdf.Rect(block["bbox"])
        for block in raw_blocks
        if _TABLE_CAPTION_RE.match(" ".join(
            span["text"] for line in block.get("lines", []) for span in line.get("spans", [])
        ))
    ]
    if not captions:
        return []
    rules: list[tuple[float, float, float, float]] = []
    for path in paths:
        for item in path["items"]:
            if item[0] == "l" and abs(item[1].y - item[2].y) < 0.5:
                first, last = sorted((item[1], item[2]), key=lambda point: point.x)
                if last.x - first.x >= 60:
                    rules.append((first.x, first.y, last.x, float(path["width"] or 0)))
            elif item[0] == "re" and 0 < item[1].height <= 2 and item[1].width >= 60:
                rect = item[1]
                rules.append((rect.x0, (rect.y0 + rect.y1) / 2, rect.x1, rect.height))
    rules.sort(key=lambda rule: rule[1])

    regions: list[pymupdf.Rect] = []
    for caption in captions:
        matches = []
        for index, (x0, top, x1, thickness) in enumerate(rules):
            centered = abs((x0 + x1) / 2 - (caption.x0 + caption.x1) / 2) <= caption.width * 0.45
            if not centered and abs(x0 - caption.x0) > 15:
                continue
            below_caption = -1 <= top - caption.y1 <= 26
            if not below_caption and not 0 < caption.y0 - top <= 380:
                continue
            has_inner_rule = False
            for left, bottom, right, weight in rules[index + 1:]:
                if bottom - top > 355:
                    break
                if bottom - top < 5 or abs(left - x0) > 3 or abs(right - x1) > 3:
                    continue
                if not has_inner_rule:
                    has_inner_rule = True
                    continue
                if weight < thickness * 0.9 or bottom - top < 15:
                    continue
                gap = top - caption.y1 if below_caption else caption.y0 - bottom
                if -1 <= gap <= 26:
                    matches.append((not below_caption, abs(gap), pymupdf.Rect(x0, top, x1, bottom)))
                break
        if matches:
            region = min(matches, key=lambda match: match[:2])[2]
            if region not in regions:
                regions.append(region)
    return regions


def _table_grid(table, data: list[list[object]]) -> dict[str, object] | None:
    """Derive a real cell grid (with rowspan/colspan) from PyMuPDF geometry."""

    raw_cells = [b for b in getattr(table, "cells", []) if b]
    if not raw_cells:
        return None
    xs = sorted({round(float(b[0]), 2) for b in raw_cells} | {round(float(b[2]), 2) for b in raw_cells})
    ys = sorted({round(float(b[1]), 2) for b in raw_cells} | {round(float(b[3]), 2) for b in raw_cells})
    if len(xs) < 2 or len(ys) < 2:
        return None
    ncols = len(xs) - 1
    nrows = len(ys) - 1
    if ncols > 40 or nrows > 100:
        return None

    def nearest(value: float, values: list[float]) -> int | None:
        rounded = round(float(value), 2)
        if rounded in values:
            return values.index(rounded)
        distances = [(abs(item - rounded), index) for index, item in enumerate(values)]
        distance, index = min(distances)
        return index if distance <= 1.5 else None

    cells: list[dict[str, object]] = []
    occupied = [[False] * ncols for _ in range(nrows)]
    for cell_index, cell_bbox in enumerate(raw_cells):
        c0 = nearest(cell_bbox[0], xs)
        c1 = nearest(cell_bbox[2], xs)
        r0 = nearest(cell_bbox[1], ys)
        r1 = nearest(cell_bbox[3], ys)
        if None in {c0, c1, r0, r1} or c1 <= c0 or r1 <= r0:
            continue
        rowspan = r1 - r0
        colspan = c1 - c0
        text = ""
        try:
            text = str(data[r0][c0] or "").strip()
        except (IndexError, TypeError):
            text = ""
        if not text:
            for rr in range(r0, min(r1, len(data))):
                for cc in range(c0, min(c1, len(data[rr]))):
                    if data[rr][cc]:
                        text = str(data[rr][cc]).strip()
                        break
                if text:
                    break
        for rr in range(r0, r1):
            for cc in range(c0, c1):
                if occupied[rr][cc]:
                    return None
                occupied[rr][cc] = True
        cells.append(
            {
                "r": r0,
                "c": c0,
                "rs": rowspan,
                "cs": colspan,
                "text": text,
                "cell_index": cell_index,
            }
        )
    if not cells:
        return None
    return {"nrows": nrows, "ncols": ncols, "has_header": True, "cells": cells}


def _center_inside_any(bbox: tuple[float, float, float, float], boxes: list[list[float]]) -> bool:
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return any(box[0] <= cx <= box[2] and box[1] <= cy <= box[3] for box in boxes)


def _table_to_markdown(rows: list[list[object]]) -> str:
    cleaned: list[list[str]] = []
    for row in rows:
        values = [normalize_text(str(cell or "")).replace("|", "\\|") for cell in row]
        if any(values):
            cleaned.append(values)
    if not cleaned:
        return ""
    cols = max(len(row) for row in cleaned)
    for row in cleaned:
        row.extend([""] * (cols - len(row)))
    header = cleaned[0]
    separator = ["---"] * cols
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


_PAGE_NUMBER_RE = re.compile(
    r"(?:page\s*)?[-–—]?\s*\d{1,4}\s*(?:(?:/|of)\s*\d{1,4})?\s*[-–—]?|第\s*\d{1,4}\s*页",
    re.IGNORECASE,
)
_PREPRINT_STAMP_RE = re.compile(r"^(?:arxiv|biorxiv|medrxiv)\s*:\s*\d{4}\.\d{4,6}(?:v\d+)?\b", re.I)
_PUBLICATION_NOTE_RE = re.compile(
    r"^(?:(?:this (?:work|article) is )?licensed under (?:a |the )?creative commons\b|"
    r"(?:©\s*\d{4}\s+)?copyright (?:©|\d{4}|held by)|all rights reserved\b|"
    r"(?:\d+(?:st|nd|rd|th)\s+)?(?:international\s+)?(?:conference|symposium|workshop) on\b|"
    r"(?:acm\s+)?(?:isbn|issn)\s+[\d-]+\b|downloaded from\s+https?://)",
    re.IGNORECASE,
)
_REFERENCE_HINT_RE = re.compile(r"^\s*(?:\[\d+\]|\d+\.\s+[A-Z])")


def _header_key(text: str) -> str:
    value = normalize_text(text).lower()
    value = re.sub(r"\d+", "#", value)
    value = re.sub(r"[^0-9a-z\u3400-\u9fff#]+", " ", value)
    return " ".join(value.split())


def _margin_text(text: str, limit: int) -> bool:
    return bool(
        text and len(text) <= limit
        and text.lower() not in _KNOWN_HEADINGS
        and not _CAPTION_RE.match(text)
        and not _REFERENCE_HINT_RE.match(text)
        and not is_mostly_math(text)
    )


def _remove_headers_and_footers(pages: list[PageData], body_size: float = 10.0) -> None:
    """Filter recurring running heads and identifiable publication marks."""
    body_size = body_size or 10.0
    occurrences: dict[tuple[str, str], set[int]] = {}
    candidates: list[tuple[PageData, Block, str, str]] = []
    removed: set[int] = set()
    for page in pages:
        for block in page.blocks:
            if block.source == "ocr" or block.kind in {"figure", "table", "formula", "caption", "title", "reference"}:
                continue
            text = normalize_text(block.text)
            if not _margin_text(text, 320) or float(block.meta.get("math_font_ratio", 0) or 0) >= 0.35:
                continue
            x0, y0, x1, y1 = block.bbox
            if (x1 <= page.width * 0.08 or x0 >= page.width * 0.92) and _PREPRINT_STAMP_RE.match(text):
                removed.add(id(block))
                continue
            if y1 <= page.height * 0.15:
                side = "top"
            elif y0 >= page.height * 0.80:
                side = "bottom"
            else:
                continue
            candidates.append((page, block, text, side))
            if len(text) <= 240 and (side == "top" or y0 >= page.height * 0.90):
                occurrences.setdefault((side, _header_key(text)), set()).add(page.number)

    repeated = {
        key
        for key, page_numbers in occurrences.items()
        if len(page_numbers) >= max(2, int(len(pages) * 0.30))
    }
    for page, block, text, side in candidates:
        y0, y1 = block.bbox[1], block.bbox[3]
        top = side == "top"
        top_limit = 0.07 if page.number == 1 else 0.10
        near_edge = y1 <= page.height * top_limit if top else y0 >= page.height * 0.90
        font_size = float(block.meta.get("font_size", 0) or 0)
        bold = float(block.meta.get("bold_ratio", 0) or 0)
        small = 0 < font_size < body_size * 0.90
        protected = (
            (page.number == 1 and top and (font_size >= body_size * 0.95 or bold >= 0.45 or len(text) >= 60))
            or (block.kind == "heading" and (font_size >= body_size * 0.90 or bold >= 0.45))
        )
        tiny_first_edge = page.number == 1 and 0 < font_size < body_size * 0.85 and (
            y1 <= page.height * 0.05 if top else y0 >= page.height * 0.97
        )
        if not protected and (
            (len(text) <= 240 and (side, _header_key(text)) in repeated)
            or (near_edge and (_PAGE_NUMBER_RE.fullmatch(text) or _PREPRINT_STAMP_RE.match(text)))
            or (not top and (near_edge or small) and _PUBLICATION_NOTE_RE.match(text))
            or (near_edge and small and top and len(text) <= 100 and page.number > 1)
            or tiny_first_edge
        ):
            removed.add(id(block))
    for page in pages:
        page.blocks = [block for block in page.blocks if id(block) not in removed]


def _estimate_body_size(pages: list[PageData]) -> float:
    per_page: list[float] = []
    for page in pages:
        sizes = [
            (float(b.meta.get("font_size", 0)), max(len(b.text), 1))
            for b in page.blocks
            if b.kind != "figure" and b.meta.get("font_size")
        ]
        if not sizes:
            continue
        weighted = []
        for size, count in sizes:
            weighted.extend([size] * min(count, 200))
        try:
            per_page.append(statistics.median(weighted))
        except statistics.StatisticsError:
            continue
    if not per_page:
        return 10.0
    return float(statistics.median(per_page)) or 10.0


def _pick_title(pages: list[PageData], meta: dict) -> Block | None:
    if not pages:
        return None
    first = pages[0]
    text_blocks = [
        b
        for b in first.blocks
        if b.kind not in {"figure", "table"} and b.text.strip() and b.bbox[1] < first.height * 0.6
    ]
    if not text_blocks:
        return None
    meta_title = normalize_text(str(meta.get("title") or "")).lower()
    if meta_title:
        for block in text_blocks:
            if normalize_text(block.text).lower() == meta_title:
                return block
    sized = [b for b in text_blocks if float(b.meta.get("font_size", 0)) > 0]
    if not sized:
        return text_blocks[0]
    largest = max(float(b.meta.get("font_size", 0)) for b in sized)
    candidates = [b for b in sized if abs(float(b.meta.get("font_size", 0)) - largest) < 0.1]
    candidates.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
    return candidates[0]


def _classify_blocks(pages: list[PageData], body_size: float, title_block: Block | None) -> None:
    title_seen = False
    for page in pages:
        for block in page.blocks:
            if block.kind in {"table", "figure"}:
                continue
            text = block.text.strip()
            size = float(block.meta.get("font_size", 0) or 0)
            bold = float(block.meta.get("bold_ratio", 0) or 0)
            math_font = float(block.meta.get("math_font_ratio", 0) or 0)

            if block is title_block and not title_seen:
                block.kind = "title"
                block.level = 1
                title_seen = True
                continue
            if _CAPTION_RE.match(text):
                block.kind = "caption"
                continue
            if (
                _EQUATION_OPERATORS.intersection(text)
                and any(ch.isalnum() for ch in text)
                and (math_font >= 0.45 or len(re.findall(r"[A-Za-z]{3,}", text)) <= 3)
                and (is_mostly_math(text) or (math_font >= 0.45 and len(text) <= 500))
            ):
                block.kind = "formula"
                continue
            lower = text.lower().strip(" .:：")
            heading_like = (
                len(text) <= 180
                and (size >= body_size * 1.06 or bold >= 0.55)
                and not text.endswith((".", "。", ";", "；"))
                and (lower in _KNOWN_HEADINGS or _HEADING_NUM_RE.match(text) or size >= body_size * 1.12)
            )
            if heading_like:
                block.kind = "heading"
                if size >= body_size * 1.3:
                    block.level = 1
                elif size >= body_size * 1.13:
                    block.level = 2
                else:
                    block.level = 3
                if _HEADING_NUM_RE.match(text):
                    dots = text.split()[0].count(".")
                    block.level = max(1, min(3, dots + 1))
                continue

    # Keep one title per document.  If the heuristic missed it, use metadata or
    # the first page's largest text block.
    if not title_seen and pages:
        first = pages[0]
        candidates = [
            b
            for b in first.blocks
            if b.kind not in {"figure", "table"} and b.text.strip() and b.bbox[1] < first.height * 0.5
        ]
        if candidates:
            candidates.sort(key=lambda b: (-float(b.meta.get("font_size", 0) or 0), b.bbox[1]))
            candidates[0].kind = "title"
            candidates[0].level = 1


def _merge_formula_fragments(page: PageData) -> None:
    """Keep adjacent fraction or matrix pieces in a single source crop."""
    candidates = [
        block for block in page.blocks
        if block.kind == "formula" or (
            block.kind == "paragraph"
            and (
                block.text in {",", ".", "→", "⇒"}
                or all("\ue000" <= ch <= "\uf8ff" for ch in block.text)
                or (
                    len(block.text) <= 32
                    and block.meta.get("math_font_ratio", 0) >= 0.45
                    and not re.search(r"[A-Za-z]{3,}", block.text)
                )
            )
        )
    ]
    source_order = {id(block): index for index, block in enumerate(candidates)}
    removed: set[int] = set()
    for block in candidates:
        if block.kind != "formula" or id(block) in removed:
            continue
        bounds = pymupdf.Rect(block.bbox)
        parts = [block]
        while True:
            neighbors = []
            for other in candidates:
                if other is block or id(other) in removed:
                    continue
                x0, y0, x1, y1 = other.bbox
                overlap = min(bounds.y1, y1) - max(bounds.y0, y0)
                gap = max(x0 - bounds.x1, bounds.x0 - x1)
                if overlap >= min(bounds.height, y1 - y0) * 0.35 and gap <= 9:
                    neighbors.append(other)
            if not neighbors:
                break
            for other in neighbors:
                bounds |= pymupdf.Rect(other.bbox)
                removed.add(id(other))
            parts.extend(neighbors)
        if len(parts) > 1:
            block.bbox = list(bounds)
            block.text = _join_lines([part.text for part in sorted(parts, key=lambda part: source_order[id(part)])])
            block.meta["merged_formula"] = True
    if removed:
        page.blocks = [block for block in page.blocks if id(block) not in removed]


def _document_title(pages: list[PageData], meta: dict, source: Path) -> str:
    for block in _all_blocks(pages):
        if block.kind == "title" and block.text.strip():
            return normalize_text(block.text)[:300]
    value = normalize_text(str(meta.get("title") or ""))
    if value and value.lower() not in {"untitled", "microsoft word", "document"}:
        return value[:300]
    return source.stem


def _mark_references(pages: list[PageData]) -> None:
    in_refs = False
    saw_reference_entry = False
    for page in pages:
        for block in page.blocks:
            normalized = normalize_text(block.text).lower().strip(" .:：")
            if block.kind == "heading" and normalized in {
                "references",
                "bibliography",
                "参考文献",
            }:
                in_refs = True
                saw_reference_entry = False
                continue
            if in_refs and block.kind == "heading" and normalized:
                # A new section (for example an Appendix after References)
                # ends the reference run, even on the same page.
                if saw_reference_entry:
                    in_refs = False
            if in_refs and block.kind in {"paragraph", "formula", "caption"}:
                block.kind = "reference"
                saw_reference_entry = True


def _pages_requiring_ocr(pages: list[PageData], config: AppConfig) -> list[int]:
    if not config.ocr.enabled or config.ocr.mode == "off":
        return []
    result: list[int] = []
    for page in pages:
        if page.ocr_done:
            continue
        if config.ocr.mode == "always":
            if page.text_chars > 0 or page.image_ratio > 0.02:
                result.append(page.number)
        elif page.text_chars < 60 and page.image_ratio > 0.08:
            result.append(page.number)
        elif page.text_chars < 20 and page.image_ratio > 0.02:
            result.append(page.number)
    return result


def _column_gutter(blocks: list[Block], page_width: float) -> float | None:
    """Find a central gutter supported by sustained, parallel body text."""
    body = [
        b
        for b in blocks
        if not b.hidden
        and b.kind in {"paragraph", "reference"}
        and page_width * 0.18 <= b.bbox[2] - b.bbox[0] <= page_width * 0.56
        and len(b.text.strip()) >= 65
    ]
    left = [b for b in body if b.bbox[0] < page_width * 0.36 and b.bbox[2] <= page_width * 0.525]
    right = [b for b in body if b.bbox[0] >= page_width * 0.475 and b.bbox[2] > page_width * 0.64]
    best_overlap = page_width * 0.03
    gutter = None
    for a in left:
        for b in right:
            gap = b.bbox[0] - a.bbox[2]
            overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
            if page_width * 0.008 <= gap <= page_width * 0.12 and overlap > best_overlap:
                best_overlap = overlap
                gutter = (a.bbox[2] + b.bbox[0]) / 2
    return gutter


def _layout_blocks(blocks: list[Block], page_width: float) -> list[Block]:
    """Use the same column assignments for reading order and rendering."""
    gutter = _column_gutter(blocks, page_width)
    if gutter is None:
        for block in blocks:
            block.meta.update(columns=1, column=1)
        return sorted(blocks, key=lambda b: (round(b.bbox[1], 1), b.bbox[0]))

    full: list[Block] = []
    regular: list[Block] = []
    margin = page_width * 0.008
    for block in blocks:
        x0, _, x1, _ = block.bbox
        if x0 < gutter - margin and x1 > gutter + margin:
            block.meta.update(columns=2, column=0)
            full.append(block)
        else:
            block.meta.update(columns=2, column=1 if (x0 + x1) / 2 <= gutter else 2)
            regular.append(block)

    full.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
    result: list[Block] = []
    remaining = list(regular)
    for separator in full:
        limit = separator.bbox[1] + (separator.bbox[3] - separator.bbox[1]) * 0.5
        band = [b for b in remaining if (b.bbox[1] + b.bbox[3]) / 2 < limit]
        if band:
            band_set = set(id(b) for b in band)
            result.extend(_sort_columns(band))
            remaining = [b for b in remaining if id(b) not in band_set]
        result.append(separator)
    if remaining:
        result.extend(_sort_columns(remaining))
    return result


def _sort_columns(blocks: list[Block]) -> list[Block]:
    return sorted(blocks, key=lambda b: (b.meta["column"], round(b.bbox[1], 1), b.bbox[0]))


def _all_blocks(pages: list[PageData]):
    for page in pages:
        yield from page.blocks


def parse_ocr_text(text: str, page: PageData) -> list[Block]:
    """Convert page OCR text and HTML tables into structured blocks."""

    return _parse_ocr_lines(_ocr_lines(text), page)


def _ocr_lines(text: str) -> list[str]:
    lines: list[str] = []
    for index, part in enumerate(_OCR_HTML_TABLE_RE.split(text or "")):
        if index % 2:
            lines.append(part)
        else:
            lines.extend(_clean_ocr_text(part).splitlines())
    return lines


def _ocr_margin_lines(lines: list[str]) -> list[tuple[str, list[tuple[int, str]]]]:
    # Whole-page OCR returns Markdown without coordinates. Only inspect the
    # first and last few lines, stopping at captions, formulas and tables.
    nonempty = [(index, line.strip()) for index, line in enumerate(lines) if line.strip()]
    limit = min(2, len(nonempty) // 2)
    margins: list[tuple[str, list[tuple[int, str]]]] = []
    for side, edge in (("top", nonempty[:limit]), ("bottom", list(reversed(nonempty))[:limit])):
        candidates = []
        for index, line in edge:
            if line.startswith(("<", "$", "\\", "```")) or "|" in line:
                break
            text = normalize_text(re.sub(r"^#{1,6}\s+", "", line).strip("*"))
            if not _margin_text(text, 240):
                break
            candidates.append((index, text))
        margins.append((side, candidates))
    return margins


def _filter_ocr_margins(page_texts: dict[int, str]) -> dict[int, list[str]]:
    lines_by_page = {number: _ocr_lines(text) for number, text in page_texts.items()}
    margins = {number: _ocr_margin_lines(lines) for number, lines in lines_by_page.items()}
    occurrences: dict[tuple[str, str], set[int]] = {}
    for number, edges in margins.items():
        for side, candidates in edges:
            for _, text in candidates:
                occurrences.setdefault((side, _header_key(text)), set()).add(number)

    min_pages = max(2, int(len(page_texts) * 0.30))
    for number, edges in margins.items():
        removed: set[int] = set()
        for side, candidates in edges:
            for index, text in candidates:
                running_text = len(occurrences[(side, _header_key(text))]) >= min_pages
                if (
                    _PAGE_NUMBER_RE.fullmatch(text)
                    or _PREPRINT_STAMP_RE.match(text)
                    or (side == "bottom" and _PUBLICATION_NOTE_RE.match(text))
                    or (running_text and (number != 1 or side == "bottom"))
                ):
                    removed.add(index)
                else:
                    break
        lines_by_page[number] = [line for index, line in enumerate(lines_by_page[number]) if index not in removed]
    return lines_by_page


def _parse_ocr_lines(lines: list[str], page: PageData) -> list[Block]:
    blocks: list[Block] = []
    index = 0
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        value = _join_lines([line for line in paragraph if line.strip()])
        paragraph = []
        if not value:
            return
        kind = "paragraph"
        level = 0
        if _CAPTION_RE.match(value):
            kind = "caption"
        elif is_mostly_math(value):
            kind = "formula"
        elif _HEADING_NUM_RE.match(value) and len(value) <= 120 and not value.endswith((".", "。")):
            kind = "heading"
            level = min(3, value.split()[0].count(".") + 1)
        blocks.append(
            Block(
                id="",
                page=page.number,
                order=0,
                kind=kind,
                text=value,
                bbox=[0, 0, page.width, page.height],
                level=level,
                source="ocr",
            )
        )

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if _OCR_HTML_TABLE_RE.fullmatch(stripped):
            flush_paragraph()
            parsed = parse_ocr_html_table(stripped)
            if parsed:
                rows, grid = parsed
                block = _ocr_block(page, "table", to_markdown(rows))
                block.meta.update({"rows": grid["nrows"], "cols": grid["ncols"], "table": grid})
                blocks.append(block)
            index += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            index += 1
            fence: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                fence.append(lines[index])
                index += 1
            index += 1
            value = "\n".join(fence).strip()
            if value:
                blocks.append(_ocr_block(page, "formula" if _looks_like_formula(value) else "paragraph", value))
            continue

        if re.fullmatch(r"\$\$.+\$\$|\\\[.+\\\]", stripped):
            flush_paragraph()
            blocks.append(_ocr_block(page, "formula", stripped[2:-2]))
            index += 1
            continue
        if stripped in {"$$", "\\["}:
            flush_paragraph()
            index += 1
            formula: list[str] = []
            closing = "$$" if stripped == "$$" else "\\]"
            while index < len(lines) and lines[index].strip() != closing:
                formula.append(lines[index])
                index += 1
            index += 1
            blocks.append(_ocr_block(page, "formula", "\n".join(formula)))
            continue

        if stripped.startswith("|") and "|" in stripped[1:]:
            flush_paragraph()
            table_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            blocks.append(_ocr_block(page, "table", "\n".join(table_lines)))
            continue

        # PaddleOCR-VL sometimes emits a plain pipe table without leading pipes
        # or a separator row.  Recognize two or more aligned rows.
        if stripped.count("|") >= 2 and index + 1 < len(lines) and lines[index + 1].strip().count("|") >= 2:
            flush_paragraph()
            pipe_lines: list[str] = []
            while index < len(lines) and lines[index].strip().count("|") >= 2:
                pipe_lines.append(lines[index].strip())
                index += 1
            blocks.append(_ocr_block(page, "table", _pipe_table_to_markdown(pipe_lines)))
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            value = heading_match.group(2).strip()
            blocks.append(_ocr_block(page, "heading", value, level=min(level, 3)))
            index += 1
            continue

        paragraph.append(stripped)
        index += 1

    flush_paragraph()

    # First page first short block is usually the title.
    if page.number == 1 and blocks and len(blocks[0].text) < 300 and blocks[0].kind == "paragraph":
        blocks[0].kind = "title"
        blocks[0].level = 1

    for order, block in enumerate(blocks, start=1):
        block.id = f"p{page.number}_o{order}"
        block.order = order
    return blocks


def _pipe_table_to_markdown(lines: list[str]) -> str:
    rows: list[list[str]] = []
    for line in lines:
        value = line.strip()
        if value.startswith("|"):
            value = value[1:]
        if value.endswith("|"):
            value = value[:-1]
        cells = [cell.strip() for cell in value.split("|")]
        if any(cells):
            rows.append(cells)
    if not rows:
        return "\n".join(lines)
    width = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (width - len(row)))
    separator = re.compile(r"^:?-{2,}:?$")
    if len(rows) > 1 and all(separator.match(cell.replace(" ", "")) for cell in rows[1] if cell):
        header = rows[0]
        body = rows[2:]
    else:
        header = rows[0]
        body = rows[1:]
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(out)


def _ocr_block(page: PageData, kind: str, text: str, level: int = 0) -> Block:
    return Block(
        id="",
        page=page.number,
        order=0,
        kind=kind,
        text=text.strip(),
        bbox=[0, 0, page.width, page.height],
        level=level,
        source="ocr",
    )


def _clean_ocr_text(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"<\|[^|>]*\|>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "<" in text and ">" in text:
        text = re.sub(
            r"(\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|\$[^$\n]+\$)|(<br\s*/?>)|</?[A-Za-z][^>\n]{0,79}>",
            lambda match: match[1] or ("\n" if match[2] else ""),
            text,
            flags=re.S | re.I,
        )
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^```(?:markdown|md|text)?\s*\n", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _looks_like_formula(text: str) -> bool:
    return is_mostly_math(text) or bool(re.search(r"\\(frac|sum|int|alpha|beta|sqrt|begin\{)", text))


def merge_ocr_pages(
    structure: DocumentStructure,
    page_texts: dict[int, str],
    engine: str,
    elapsed: float = 0.0,
) -> DocumentStructure:
    """Replace OCR-target pages with structured OCR blocks and re-index."""

    cleaned_pages = _filter_ocr_margins(page_texts)
    for page in structure.pages:
        if page.number not in cleaned_pages:
            continue
        blocks = _parse_ocr_lines(cleaned_pages[page.number], page)
        if not blocks:
            page.ocr_done = True
            page.ocr_engine = engine
            page.needs_ocr = False
            continue
        page.blocks = _layout_blocks(blocks, page.width)
        # Keep any native word boxes so a force-OCR run does not remove the
        # PDF selection affordance; scanned pages simply have none.
        page.ocr_done = True
        page.ocr_engine = engine
        page.needs_ocr = False
        page.text_chars = len(page_texts[page.number].strip())

    done_pages = [p.number for p in structure.pages if p.ocr_done]
    required = sorted(set(structure.ocr_summary.get("required_pages") or []) | set(page_texts))
    structure.ocr_summary.update(
        {
            "engine": engine,
            "required_pages": required,
            "done_pages": done_pages,
            "status": "done",
            "elapsed": round(elapsed, 2),
        }
    )
    structure.ocr_summary.pop("error", None)
    structure.reindex()
    _mark_references(structure.pages)
    return structure


def ocr_target_pages(structure: DocumentStructure, config: AppConfig) -> list[int]:
    if not config.ocr.enabled or config.ocr.mode == "off":
        return []
    if config.ocr.force_refresh:
        return [p.number for p in structure.pages if p.text_chars > 0 or p.image_ratio > 0.02]
    result: list[int] = []
    for page in structure.pages:
        if page.ocr_done:
            continue
        if config.ocr.mode == "always":
            if page.text_chars > 0 or page.image_ratio > 0:
                result.append(page.number)
        elif page.text_chars < 60 and page.image_ratio > 0.08:
            result.append(page.number)
        elif page.text_chars < 20 and page.image_ratio > 0.02:
            result.append(page.number)
    return result


def document_title_fallback(path: Path) -> str:
    return path.stem
