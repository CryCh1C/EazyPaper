from __future__ import annotations

import json
from pathlib import Path

from app.config import AppConfig
from app.models import PageData
from app.pdf.cache import DocumentCache
from app.pdf.extractor import extract_document, merge_ocr_pages, parse_ocr_text


def test_extract_structure(sample_pdf):
    config = AppConfig()
    config.ocr.enabled = False
    doc = extract_document(sample_pdf, config, known_hash="hash-1")
    assert doc.page_count == 3
    assert "EasyPaper" in doc.title
    kinds = [b.kind for b in doc.blocks]
    assert "title" in kinds
    assert "heading" in kinds
    assert "table" in kinds
    assert "formula" in kinds
    assert "caption" in kinds
    assert "figure" in kinds
    assert kinds.count("title") == 1

    first_page = doc.pages[0]
    assert first_page.width > 500
    assert first_page.words
    assert any("Abstract" in b.text for b in first_page.blocks)

    table = next(b for b in doc.blocks if b.kind == "table")
    assert "| Stage |" in table.text
    grid = table.meta.get("table")
    assert isinstance(grid, dict)
    assert grid["nrows"] == 4
    assert grid["ncols"] == 3
    assert len(grid["cells"]) == 12
    formula = next(b for b in doc.blocks if b.kind == "formula")
    assert "c^2" in formula.text


def test_figure_asset_extraction(sample_pdf, tmp_path):
    config = AppConfig()
    config.ocr.enabled = False
    asset_dir = tmp_path / "assets"
    doc = extract_document(sample_pdf, config, known_hash="figure", asset_dir=asset_dir)
    figure = next(b for b in doc.blocks if b.kind == "figure")
    asset = str(figure.meta.get("asset", ""))
    assert asset.startswith("assets/")
    assert (asset_dir.parent / asset).is_file()
    assert figure.meta.get("display_width", 0) >= 220
    table = next(b for b in doc.blocks if b.kind == "table")
    formula = next(b for b in doc.blocks if b.kind == "formula")
    for block in (table, formula):
        assert block.meta.get("region_image") is True
        assert (asset_dir.parent / str(block.meta["asset"])).is_file()
        assert block.translatable is False
    from app.ui.richtext import render_document

    html = render_document(doc, asset_base=asset_dir.parent)
    assert "<img" in html
    assert "file://" in html
    assert "table-image" in html
    assert "formula-image" in html
    zoomed = render_document(doc, asset_base=asset_dir.parent, zoom=1.5)
    assert "font-size:15.00pt" in zoomed
    assert f'width="{int(figure.meta["display_width"] * 1.5)}"' in zoomed


def test_headers_and_footers_removed_before_translation():
    from app.models import Block
    from app.pdf.extractor import _remove_headers_and_footers

    pages = []
    for number in (1, 2, 3):
        blocks = [
            Block(
                id="",
                page=number,
                order=1,
                kind="paragraph",
                text="Journal of Testing 2026" if number > 1 else "Journal of Testing",
                bbox=[50, 20, 500, 42],
                meta={"font_size": 8},
            ),
            Block(
                id="",
                page=number,
                order=2,
                kind="title" if number == 1 else "paragraph",
                text="A Paper Title" if number == 1 else "Body paragraph text.",
                bbox=[50, 100, 500, 130],
                meta={"font_size": 18 if number == 1 else 10},
            ),
            Block(
                id="",
                page=number,
                order=3,
                kind="paragraph",
                text=f"Body paragraph on page {number}.",
                bbox=[50, 240, 500, 280],
                meta={"font_size": 10},
            ),
            Block(
                id="",
                page=number,
                order=4,
                kind="paragraph",
                text=str(number),
                bbox=[280, 760, 320, 775],
                meta={"font_size": 8},
            ),
        ]
        pages.append(PageData(number, 595, 842, blocks=blocks))

    _remove_headers_and_footers(pages, 10.0)
    for page in pages:
        texts = [b.text for b in page.blocks]
        assert not any("Journal of Testing" in text for text in texts)
        assert not any(text.strip().isdigit() for text in texts)
        assert any("Body paragraph" in text for text in texts)
    assert any("A Paper Title" in b.text for b in pages[0].blocks)


def test_reading_order_keeps_left_column_before_right(sample_pdf):
    config = AppConfig()
    config.ocr.enabled = False
    doc = extract_document(sample_pdf, config, known_hash="hash-1")
    page1 = [b.text for b in doc.pages[0].blocks]
    intro = next(i for i, text in enumerate(page1) if "Reading a paper" in text)
    method = next(i for i, text in enumerate(page1) if "The PDF is parsed" in text)
    translation = next(i for i, text in enumerate(page1) if "Blocks are grouped" in text)
    assert intro < method < translation


def test_ocr_target_detection(sample_pdf):
    from app.pdf.extractor import ocr_target_pages

    config = AppConfig()
    doc = extract_document(sample_pdf, config, known_hash="hash-1")
    pages = ocr_target_pages(doc, config)
    assert pages == [3]
    assert doc.pages[2].image_ratio > 0.8


def test_merged_table_grid_preserves_spans(tmp_path):
    import pymupdf

    pdf = tmp_path / "merged.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    x0, y0, x1, y1 = 80, 100, 500, 400
    page.draw_rect(pymupdf.Rect(x0, y0, x1, y1))
    for x in (220, 360):
        page.draw_line((x, y0), (x, y1))
    for y in (175, 250, 325):
        page.draw_line((220 if y == 175 else 80, y), (x1, y))
    for x, y, text in [
        (90, 145, "Merged"),
        (230, 145, "R1C2"),
        (370, 145, "R1C3"),
        (230, 220, "R2C2"),
        (370, 220, "R2C3"),
        (230, 295, "R3C2"),
        (370, 295, "R3C3"),
        (230, 370, "R4C2"),
        (370, 370, "R4C3"),
    ]:
        page.insert_text((x, y), text, fontsize=10)
    doc.save(pdf)
    doc.close()

    config = AppConfig()
    config.ocr.enabled = False
    extracted = extract_document(pdf, config, known_hash="merged")
    table = next(b for b in extracted.blocks if b.kind == "table")
    grid = table.meta["table"]
    first = next(c for c in grid["cells"] if c["text"] == "Merged")
    assert first["rs"] == 2
    assert grid["ncols"] == 3


def test_parse_ocr_markdown():
    page = PageData(number=3, width=595, height=842)
    text = """6 Scanned Appendix

This page is a raster image.

Step | Action | Output
1 | Render page | RGB image
2 | OCR | Markdown

Table 1: Results.
"""
    blocks = parse_ocr_text(text, page)
    assert blocks[0].kind == "heading"
    table = next(b for b in blocks if b.kind == "table")
    assert "| Step | Action | Output |" in table.text
    assert any(b.kind == "caption" for b in blocks)


def test_merge_ocr_and_cache(sample_pdf, config, tmp_path, monkeypatch):
    import app.pdf.cache as cache_module

    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")
    config.ocr.enabled = True
    config.ocr.mode = "auto"
    doc = extract_document(sample_pdf, config, known_hash="hash-2")
    ocr_text = """6 Scanned Appendix

This page intentionally contains only a raster image.

Step | Action | Output
1 | Render page | RGB image
"""
    merge_ocr_pages(doc, {3: ocr_text}, "unit-test", 1.0)
    third = doc.pages[2]
    assert third.ocr_done
    assert any(b.kind == "heading" and "Scanned" in b.text for b in third.blocks)
    cache = DocumentCache(sample_pdf, "hash-2", config)
    cache.save_structure(doc)
    loaded = cache.load_structure()
    assert loaded is not None
    assert loaded.pages[2].ocr_done
    assert any(b.kind == "table" for b in loaded.pages[2].blocks)


def test_translation_cache_roundtrip(sample_pdf, config, tmp_path, monkeypatch):
    import app.pdf.cache as cache_module

    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")
    doc = extract_document(sample_pdf, config, known_hash="hash-3")
    cache = DocumentCache(sample_pdf, "hash-3", config)
    cache.save_structure(doc)
    assert cache.load_translation(doc) is None
    data = {"p1_b1": "标题", "p1_b2": "正文"}
    cache.save_translation(doc, data, complete=True)
    loaded = cache.load_translation(doc)
    assert loaded == data
    assert cache.translation_meta(doc)["complete"] is True
    config.llm.model = "another-model"
    cache.invalidate_signature()
    assert cache.load_translation(doc) is None
