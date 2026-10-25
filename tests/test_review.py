from __future__ import annotations

from app.config import AppConfig
from app.models import Block, DocumentStructure, PageData
from app.review import (
    apply_review_removals,
    parse_review_response,
    review_candidates,
    review_translation,
)
from app.ui.richtext import render_document


def _document():
    p1_blocks = [
        Block("p1_h", 1, 1, "paragraph", "Journal Header", bbox=[50, 20, 400, 34]),
        Block("p1_t", 1, 2, "paragraph", "This is the real body text.", bbox=[50, 100, 500, 180]),
        Block("p1_n", 1, 3, "paragraph", "12", bbox=[280, 810, 310, 825]),
    ]
    p2_blocks = [
        Block("p2_h", 2, 1, "paragraph", "Journal Header", bbox=[50, 20, 400, 34]),
        Block("p2_t", 2, 2, "paragraph", "More body text for page two.", bbox=[50, 100, 500, 180]),
    ]
    return DocumentStructure(
        source_path="x.pdf",
        source_hash="x",
        file_name="x.pdf",
        title="A Paper",
        page_count=2,
        pages=[
            PageData(1, 595, 842, blocks=p1_blocks),
            PageData(2, 595, 842, blocks=p2_blocks),
        ],
    )


def test_candidate_detection_keeps_body():
    doc = _document()
    ids = {b.id for b in review_candidates(doc)}
    assert "p1_h" in ids
    assert "p1_n" in ids
    assert "p2_h" in ids
    assert "p1_t" not in ids
    assert "p2_t" not in ids


def test_parse_and_apply_review():
    doc = _document()
    candidates = review_candidates(doc)
    removed = parse_review_response(
        '{"remove":[{"id":"p1_h"},{"id":"p1_n"},{"id":"p2_h"},{"id":"bogus"}]}',
        candidates,
    )
    assert removed == ["p1_h", "p1_n", "p2_h"]
    count = apply_review_removals(doc, removed)
    assert count == 3
    assert doc.block("p1_h").hidden
    assert doc.block("p1_t").hidden is False
    html = render_document(doc)
    assert "Journal Header" not in html
    assert "real body text" in html


def test_grid_html_supports_rowspan():
    from app.tables import grid_to_html

    grid = {
        "nrows": 4,
        "ncols": 3,
        "has_header": True,
        "cells": [
            {"r": 0, "c": 0, "rs": 2, "cs": 1, "text": "Merged", "cell_index": 0},
            {"r": 0, "c": 1, "rs": 1, "cs": 1, "text": "R1C2", "cell_index": 1},
            {"r": 0, "c": 2, "rs": 1, "cs": 1, "text": "R1C3", "cell_index": 2},
            {"r": 1, "c": 1, "rs": 1, "cs": 1, "text": "R2C2", "cell_index": 3},
            {"r": 1, "c": 2, "rs": 1, "cs": 1, "text": "R2C3", "cell_index": 4},
        ],
    }
    html = grid_to_html(grid, {0: "合并"})
    assert "grid-row:1 / span 2" in html
    assert "合并" in html


def test_grid_html_fills_missing_cells_to_keep_columns():
    from app.tables import grid_to_html

    grid = {
        "nrows": 2,
        "ncols": 3,
        "has_header": True,
        "cells": [
            {"r": 0, "c": 0, "rs": 1, "cs": 1, "text": "A", "cell_index": 0},
            {"r": 0, "c": 1, "rs": 1, "cs": 1, "text": "B", "cell_index": 1},
            {"r": 0, "c": 2, "rs": 1, "cs": 1, "text": "C", "cell_index": 2},
            {"r": 1, "c": 0, "rs": 1, "cs": 1, "text": "1", "cell_index": 3},
            {"r": 1, "c": 2, "rs": 1, "cs": 1, "text": "3", "cell_index": 4},
        ],
    }
    html = grid_to_html(grid)
    assert '<div class="grid-cell empty" style="grid-row:2;grid-column:2;"></div>' in html
    assert "grid-row:2 / span 1;grid-column:3 / span 1;" in html
    assert ">3</div>" in html


def test_review_request(mock_api):
    config = AppConfig()
    config.llm.base_url = mock_api
    config.llm.api_key = "test"
    config.llm.model = "mock-model"
    doc = _document()
    candidates = review_candidates(doc)
    removed = review_translation(config, doc, candidates)
    assert "p1_h" in removed
    assert "p1_n" in removed
    assert "p1_t" not in removed
