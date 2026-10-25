from __future__ import annotations

from app.config import AppConfig
from app.llm import (
    TranslationParseError,
    TranslationService,
    build_chat_context,
    build_chat_messages,
    make_chunks,
    stream_chat,
    test_connection as check_connection,
)
from app.models import Block
from app.pdf.extractor import extract_document


def _blocks():
    return [
        Block("b1", 1, 1, "title", "A Paper"),
        Block("b2", 1, 2, "paragraph", "Hello world."),
        Block("b3", 1, 3, "table", "| A | B |\n| --- | --- |\n| 1 | 2 |"),
    ]


def test_chunking_preserves_order():
    blocks = [Block(f"b{i}", 1, i, "paragraph", "x" * 800) for i in range(1, 7)]
    chunks = make_chunks(blocks, 2000)
    assert [b.id for chunk in chunks for b in chunk] == [b.id for b in blocks]


def test_parse_json_requires_all_ids():
    blocks = _blocks()
    result = TranslationService._parse_json(
        '{"translations":[{"id":"b1","text":"标题"},{"id":"b2","text":"正文"},{"id":"b3","text":"表"}]}',
        blocks,
    )
    assert result["b1"] == "标题"
    try:
        TranslationService._parse_json('{"translations":[{"id":"b1","text":"标题"}]}', blocks)
    except TranslationParseError as exc:
        assert "缺失" in str(exc) or "缺少" in str(exc)
    else:
        raise AssertionError("missing ids should fail")


def test_marker_parser():
    blocks = _blocks()
    content = (
        "@@BLOCK id=b1@@\n标题\n@@END@@\n"
        "@@BLOCK id=b2@@\n正文\n@@END@@\n"
        "@@BLOCK id=b3@@\n表\n@@END@@"
    )
    result = TranslationService._parse_markers(content, blocks)
    assert set(result) == {"b1", "b2", "b3"}


def test_reasoning_effort_resolution():
    from app.llm import resolve_reasoning_effort

    assert resolve_reasoning_effort("auto", "gpt-5-mini", "low") == "low"
    assert resolve_reasoning_effort("auto", "o3", "low") == "low"
    assert resolve_reasoning_effort("auto", "gpt-4o-mini", "low") == ""
    assert resolve_reasoning_effort("off", "o3", "low") == ""
    assert resolve_reasoning_effort("high", "gpt-4o-mini", "low") == "high"


def test_geometry_table_translation_returns_html(mock_api, sample_pdf):
    config = AppConfig()
    config.llm.base_url = mock_api
    config.llm.api_key = "test-key"
    config.llm.model = "mock-model"
    config.ocr.enabled = False
    doc = extract_document(sample_pdf, config, known_hash="grid-html")
    table = next(b for b in doc.blocks if b.kind == "table")
    mapping = TranslationService(config).translate_blocks([table], doc.title)
    html = mapping[table.id]
    assert html.startswith('<div class="table-grid"')
    assert "grid-template-columns:" in html
    assert html.count("grid-row:") >= 12
    assert "【译文】Stage" in html


def test_table_translation_preserves_grid(mock_api):
    from app.tables import parse_markdown_table

    config = AppConfig()
    config.llm.base_url = mock_api
    config.llm.api_key = "test-key"
    config.llm.model = "mock-model"
    table = Block(
        "t1",
        1,
        1,
        "table",
        "| A | B | C |\n| --- | --- | --- |\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |",
    )
    mapping = TranslationService(config).translate_blocks([table], "Table paper")
    assert set(mapping) == {"t1"}
    parsed = parse_markdown_table(mapping["t1"])
    assert parsed is not None
    assert len(parsed.rows) == 3
    assert all(len(row) == 3 for row in parsed.rows)
    assert "【译文】A" in parsed.rows[0][0]


def test_connection_and_translation(sample_pdf, mock_api):
    config = AppConfig()
    config.llm.base_url = mock_api
    config.llm.api_key = "test-key"
    config.llm.model = "mock-model"
    result = check_connection(config)
    assert result["ok"], result
    assert result["model_found"] is True
    assert "mock-model" in result["models"]

    config.ocr.enabled = False
    doc = extract_document(sample_pdf, config, known_hash="llm-hash")
    service = TranslationService(config)
    first_chunk = doc.translatable_blocks()[:4]
    mapping = service.translate_blocks(first_chunk, doc.title)
    assert set(mapping) == {b.id for b in first_chunk}
    assert all(text.strip() for text in mapping.values())


def test_chat_context_and_stream(sample_pdf, mock_api):
    config = AppConfig()
    config.llm.base_url = mock_api
    config.llm.api_key = "test-key"
    config.llm.model = "mock-model"
    config.ocr.enabled = False
    config.chat.context_chars = 9000
    doc = extract_document(sample_pdf, config, known_hash="chat-hash")
    context, meta = build_chat_context(doc, config, "OCR 是如何工作的？", "scanned page", 3)
    assert "scanned page" in context
    assert meta["chars"] > 0
    messages = build_chat_messages(config, context, "OCR 是如何工作的？")
    pieces = []
    answer = stream_chat(config, messages, pieces.append)
    assert answer
    assert "".join(pieces) == answer


def test_context_budget_triggers_retrieval(sample_pdf):
    from app.models import Block, PageData

    config = AppConfig()
    config.ocr.enabled = False
    config.chat.context_chars = 8000
    doc = extract_document(sample_pdf, config, known_hash="ctx-hash")
    filler = [
        Block(
            id=f"long_{i}",
            page=1,
            order=100 + i,
            kind="paragraph",
            text=(f"Section {i}. " + "This is a long paragraph about retrieval and local OCR. " * 20),
        )
        for i in range(12)
    ]
    doc.pages[0].blocks.extend(filler)
    doc.reindex()
    context, meta = build_chat_context(doc, config, "energy equation formula", "", 0)
    assert context
    assert meta["retrieval"] is True
    assert meta["used"] < meta["chunks"]
