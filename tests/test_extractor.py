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


def test_three_line_table_keeps_its_rows_and_neighboring_text(tmp_path):
    import pymupdf

    path = tmp_path / "three-line.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((70, 120), "Table 1. Evaluation results.")
        for y, width in ((133, 1), (154, 0.4), (214, 1)):
            page.draw_line((70, y), (380, y), width=width)
        rows = (
            (147, ("Method", "Accuracy", "AUC")),
            (169, ("Baseline", "86.4%", "0.89")),
            (187, ("Variant", "89.2%", "0.91")),
            (205, ("Proposed", "92.1%", "0.95")),
        )
        for y, row in rows:
            for x, value in zip((76, 218, 310), row):
                page.insert_text((x, y), value, fontsize=10)
        page.insert_text((70, 230), "The differences were consistent across runs.")
        pdf.save(path)

    doc = extract_document(path, AppConfig(), asset_dir=tmp_path / "assets")
    table, = [block for block in doc.blocks if block.kind == "table"]
    assert "| Baseline | 86.4% | 0.89 |" in table.text
    assert "| Proposed | 92.1% | 0.95 |" in table.text
    assert table.bbox == [70, 133, 380, 214]
    assert (tmp_path / table.meta["asset"]).is_file()
    other = [block.text for block in doc.blocks if block is not table]
    assert any("consistent across runs" in text for text in other)
    assert not any("92.1%" in text for text in other)


def test_adjacent_formula_fragments_merge_without_the_next_paragraph():
    from app.models import Block
    from app.pdf.extractor import _merge_formula_fragments

    part_a = Block("", 1, 0, "paragraph", "x′ = σ", [70, 90, 106, 110], meta={"math_font_ratio": 0.8})
    operator = Block("", 1, 0, "paragraph", "∑︁", [105, 92, 112, 106], meta={"math_font_ratio": 1.0})
    part_b = Block("", 1, 0, "formula", "b + c", [100, 104, 122, 116])
    prose = Block("", 1, 0, "paragraph", "where c is a constant", [70, 115, 220, 128])
    page = PageData(number=1, width=612, height=792, blocks=[part_a, operator, part_b, prose])

    _merge_formula_fragments(page)

    assert page.blocks == [part_b, prose]
    assert part_b.bbox == [70, 90, 122, 116]
    assert part_b.text == "x′ = σ ∑︁ b + c"


def test_ocr_formula_keeps_native_crop_and_searchable_text(sample_pdf, tmp_path):
    from app.models import Block
    from app.ocr.service import OcrService

    config = AppConfig()
    assets = tmp_path / "assets"
    doc = extract_document(sample_pdf, config, asset_dir=assets)
    native = next(block for block in doc.blocks if block.kind == "formula")
    native_path = tmp_path / native.meta["asset"]
    original = native_path.read_bytes()
    page = doc.pages[native.page - 1]
    bbox = [80, 690, 130, 710]
    page.blocks.append(Block("", page.number, 0, "formula", "x = y", bbox))

    added = OcrService(config).apply_layout_formulas(
        doc, sample_pdf, {page.number: [{"bbox": bbox, "score": 0.99}]}, assets,
    )

    assert added == 1
    layout = next(block for block in page.blocks if block.source == "oxr_layout")
    assert layout.text == "x = y"
    assert layout.meta["asset"] != native.meta["asset"]
    assert native in page.blocks and native_path.read_bytes() == original


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


def test_native_margin_filter_keeps_notes_and_removes_running_publication_text():
    from app.models import Block
    from app.pdf.extractor import _remove_headers_and_footers

    running_head = "A long paper title on two lines followed by " + "conference information " * 6
    top_lines = ["First page introductory text stays.", "2 Authors et al.", running_head, "4 Authors et al.", running_head]
    pages = []
    for number, top in enumerate(top_lines, 1):
        # Different footnotes occupy the same location on successive pages.
        note = f"Data for {('dogs', 'cats', 'owls', 'bears', 'seals')[number - 1]} at https://example.org/data."
        blocks = [
            Block("", number, 0, "paragraph", top, [100, 91, 500, 105], meta={"font_size": 9.1}),
            Block("", number, 0, "paragraph", note, [100, 735, 500, 755], meta={"font_size": 8}),
        ]
        pages.append(PageData(number, 600, 800, blocks))
    title = Block("", 1, 0, "paragraph", "The Actual Paper Title", [100, 74, 510, 110], meta={"font_size": 18})
    license_note = Block("", 1, 0, "paragraph", "This work is licensed under a Creative Commons license.",
                         [100, 673, 500, 700], meta={"font_size": 7})
    stamp = Block("", 1, 0, "paragraph", "arXiv:2401.12345v2 [cs.CV]", [12, 190, 37, 560])
    pages[0].blocks.extend([title, license_note, stamp])

    _remove_headers_and_footers(pages, 10.0)

    assert [b.text for b in pages[0].blocks] == [
        "First page introductory text stays.",
        "Data for dogs at https://example.org/data.",
        title.text,
    ]
    assert all(len(page.blocks) == 1 and "https://example.org/data" in page.blocks[0].text for page in pages[1:])


def test_reading_order_keeps_left_column_before_right(sample_pdf):
    config = AppConfig()
    config.ocr.enabled = False
    doc = extract_document(sample_pdf, config, known_hash="hash-1")
    page1 = [b.text for b in doc.pages[0].blocks]
    intro = next(i for i, text in enumerate(page1) if "Reading a paper" in text)
    method = next(i for i, text in enumerate(page1) if "The PDF is parsed" in text)
    translation = next(i for i, text in enumerate(page1) if "Blocks are grouped" in text)
    assert intro < method < translation


def test_side_by_side_tables_and_staggered_text_keep_single_column_order():
    from app.models import Block
    from app.pdf.extractor import _layout_blocks

    caption = "Comparison across datasets with the same evaluation protocol and metrics."
    left_caption = Block("", 1, 0, "caption", caption, [108, 72, 287, 145])
    right_caption = Block("", 1, 0, "caption", caption, [320, 73, 505, 148])
    left_table = Block("", 1, 0, "table", caption, [108, 152, 287, 280])
    right_table = Block("", 1, 0, "table", caption, [320, 152, 505, 280])
    body = Block("", 1, 0, "paragraph", caption, [108, 296, 505, 351])
    left_text = Block("", 1, 0, "paragraph", caption, [108, 360, 287, 399])
    inset_note = Block("", 1, 0, "paragraph", caption, [320, 420, 505, 446])
    expected = [left_caption, right_caption, left_table, right_table, body, left_text, inset_note]

    ordered = _layout_blocks(list(reversed(expected)), 612)

    assert ordered == expected
    assert all(b.meta["columns"] == b.meta["column"] == 1 for b in ordered)


def test_two_column_layout_keeps_full_width_blocks_between_bands():
    from app.models import Block, DocumentStructure
    from app.pdf.extractor import _layout_blocks
    from app.ui.richtext import render_document

    text = "A paragraph in this column gives enough detail to establish the layout."
    left = Block("left", 1, 0, "paragraph", text, [60, 100, 291, 155])
    right = Block("right", 1, 0, "paragraph", text, [322, 90, 545, 155])
    spanning = Block("wide", 1, 0, "figure", "", [100, 160, 510, 230])
    right_only = Block("last", 1, 0, "paragraph", text, [320, 250, 540, 289])

    ordered = _layout_blocks([right_only, spanning, right, left], 612)
    assert ordered == [left, right, spanning, right_only]
    assert [b.meta["column"] for b in ordered] == [1, 2, 0, 2]
    doc = DocumentStructure("paper.pdf", "sample", "paper.pdf", "Paper", 1, [PageData(1, 612, 792, ordered)])
    html = render_document(doc)
    assert '<div class="page-full"><div class="block" data-block="wide">' in html
    assert '<div class="page-column" data-column="2"><div class="block" data-block="last">' in html


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


def test_ocr_html_preserves_table_spans_and_formula_operators():
    page = PageData(number=3, width=595, height=842)
    text = r"""Table 1. Evaluation results.
<table><tr><td rowspan="2">Method</td><td colspan="2">Accuracy</td></tr>
<tr><td>Set A</td><td>Set B</td></tr>
<tr><td>Model X</td><td>91%</td><td>92%</td></tr></table>
\[a<b>c\]
"""
    caption, table, formula = parse_ocr_text(text, page)
    assert caption.kind == "caption"
    assert table.kind == "table"
    assert "| Model X | 91% | 92% |" in table.text
    assert table.meta["table"]["ncols"] == 3
    cells = {cell["text"]: cell for cell in table.meta["table"]["cells"]}
    assert cells["Method"]["rs"] == 2
    assert cells["Accuracy"]["cs"] == 2
    assert cells["Set A"]["c"] == 1
    assert formula.kind == "formula" and formula.text == "a<b>c"


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


def test_ocr_margins_are_filtered_before_joining_paragraph_lines():
    from app.models import DocumentStructure
    from app.ui.richtext import render_document

    pages = [PageData(number, 600, 800) for number in range(1, 4)]
    doc = DocumentStructure("paper.pdf", "sample", "paper.pdf", "Paper", 3, pages)
    merge_ocr_pages(doc, {
        1: "arXiv:2401.12345v2\nThe Actual Paper Title\n\nOpening body paragraph.\nPage 1",
        2: "# Journal of Testing 2\nThe body begins here.\n\n$$\nx = y\n$$\nPage 2",
        3: "# Journal of Testing 3\nThe results stay here.\n\nTable 1: Results\n"
           "<table><tr><td>Sample</td><td>Value</td></tr><tr><td>A</td><td>9</td></tr></table>\n"
           "\n1 Detailed note with a link to https://example.org/data.\n第 3 页",
    }, "unit-test")

    assert pages[0].blocks[0].kind == "title" and pages[0].blocks[0].text == "The Actual Paper Title"
    assert [b.text for b in pages[1].blocks] == ["The body begins here.", "x = y"]
    assert [b.kind for b in pages[2].blocks] == ["paragraph", "caption", "table", "paragraph"]
    assert pages[2].blocks[-1].text == "1 Detailed note with a link to https://example.org/data."
    assert all("Journal of Testing" not in b.text for b in doc.translatable_blocks())
    html = render_document(doc)
    assert "Journal of Testing" not in html and "arXiv:" not in html


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


def test_test_samples_keep_single_and_double_column_layout(tmp_path):
    from app.ui.richtext import render_document

    root = Path(__file__).resolve().parents[1]
    single = root / "test_sample1.pdf"
    double = root / "test_sample2.pdf"
    if not single.is_file() or not double.is_file():
        return
    config = AppConfig()
    config.ocr.enabled = False
    one = extract_document(
        single, config, known_hash="sample-one", asset_dir=tmp_path / "one" / "assets"
    )
    two = extract_document(
        double, config, known_hash="sample-two", asset_dir=tmp_path / "two" / "assets"
    )
    assert all(max(int(b.meta.get("columns", 1)) for b in p.blocks) == 1 for p in one.pages)
    assert all(max(int(b.meta.get("columns", 1)) for b in p.blocks) == 2 for p in two.pages)
    html = render_document(two, asset_base=tmp_path / "two" / "assets")
    assert "page-columns" in html
    assert "page-column" in html
    assert '<div id="camera-shell">' in html
    assert 'id="camera"' in html
    assert "body.style.zoom" not in html
    assert "scale(" in html


def test_sample2_aggregation_formula_is_one_complete_image(tmp_path):
    import pytest

    from app.ocr.service import OcrService
    from app.ui.richtext import render_block

    path = Path(__file__).resolve().parents[1] / "test_sample2.pdf"
    if not path.is_file():
        pytest.skip("optional test_sample2.pdf is unavailable")
    config = AppConfig()
    assets = tmp_path / "assets"
    doc = extract_document(path, config, asset_dir=assets)
    page = doc.pages[4]
    intro = next(b for b in page.blocks if b.text.startswith("Each node then aggregates"))
    following = next(b for b in page.blocks if b.text.startswith("We then employ two GAT layers"))

    def aggregation_formula():
        formula, = [
            b for b in page.blocks
            if b.bbox[0] > 310 and intro.bbox[3] < b.bbox[1] < following.bbox[1]
        ]
        assert formula.kind == "formula" and not formula.translatable
        assert all(symbol in formula.text for symbol in ("h′", "𝜎", "∑", "𝛼"))
        assert (tmp_path / formula.meta["asset"]).is_file()
        html = render_block(formula, asset_base=tmp_path)
        assert html.count("<img") == 1 and "data-tex" not in html
        return formula

    native = aggregation_formula()
    crop = native.meta["asset_bbox"]
    assert crop[0] < 397.85 and crop[1] < 548.1
    assert 579.4 < crop[3] < following.bbox[1]

    # A layout box that starts inside the brackets must not crop out the left side.
    boxes = {5: [{"bbox": [425, 548, 478, 580], "score": 0.99}]}
    assert OcrService(config).apply_layout_formulas(doc, path, boxes, assets) == 1
    refined = aggregation_formula()
    assert refined.bbox[0] <= crop[0] and refined.bbox[1] <= crop[1]
    assert refined.bbox[2] >= crop[2] and refined.bbox[3] >= crop[3]
