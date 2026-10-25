"""Post-translation review that removes running headers/footers and boilerplate."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone

from .config import AppConfig
from .llm import LLMError, friendly_api_error, make_client, reasoning_kwargs
from .logging_setup import get_logger
from .models import Block, DocumentStructure, normalize_text

log = get_logger("review")

_PAGE_NUMBER = re.compile(
    r"^\s*(?:page\s*)?[-–—]?\s*\d{1,4}\s*(?:(?:/|of)\s*\d{1,4})?\s*[-–—]?\s*$",
    re.IGNORECASE,
)
_BOILERPLATE = re.compile(
    r"(arxiv|preprint|bioRxiv|medRxiv|downloaded from|copyright|all rights reserved|"
    r"journal homepage|proceedings of|issn|doi\s*:|https?://|www\.)",
    re.IGNORECASE,
)
_REVIEW_SYSTEM = """你是学术论文译文的终审编辑。你只负责识别页面级冗余信息。
允许删除：页眉、页脚、页码、running title、期刊名/会议名页眉、arXiv 编号、版权声明、下载水印、
“Downloaded from …”、重复出现的机构/日期/占位信息。
严禁删除：论文标题、作者、摘要、正文句子、章节标题、图注、表注、表格、公式、参考文献、任何有信息量的内容。
不确定时保留。只输出 JSON。"""

_REVIEW_PROMPT = """论文标题：{title}
以下是从各页提取的候选短文本块（不是全文）。请仅找出应删除的页面级冗余信息。
返回严格 JSON：
{{"remove":[{{"id":"块id","reason":"简短原因"}}]}}
没有可删除项时返回 {{"remove":[]}}。

候选块：
{payload}"""


def review_candidates(document: DocumentStructure, limit: int = 160) -> list[Block]:
    """Cheap local pre-filter so the review request stays small and safe."""

    pages = {p.number: p for p in document.pages}
    counts = Counter(
        normalize_text(b.text).lower()
        for b in document.blocks
        if not b.hidden and b.text.strip()
    )
    scored: list[tuple[int, Block]] = []
    allowed = {"paragraph", "other", "heading"}
    for block in document.blocks:
        if block.hidden or block.kind not in allowed:
            continue
        text = normalize_text(block.text)
        if not text or len(text) > 200:
            continue
        page = pages.get(block.page)
        if page is None:
            continue
        x0, y0, x1, y1 = block.bbox
        full_bbox = (x1 - x0) >= page.width * 0.85 and (y1 - y0) >= page.height * 0.85
        top = not full_bbox and y0 <= page.height * 0.085
        bottom = not full_bbox and y1 >= page.height * 0.915
        repeated = counts.get(text.lower(), 0) >= 2 and len(text) >= 4
        page_number = bool(_PAGE_NUMBER.fullmatch(text))
        boiler = bool(_BOILERPLATE.search(text)) and (top or bottom or len(text) < 100)
        heading_edge = block.kind == "heading" and (top or bottom) and len(text) < 80
        if not (top or bottom or repeated or page_number or boiler or heading_edge):
            continue
        if len(text) <= 2 and not page_number:
            continue
        score = 0
        score += 4 if page_number else 0
        score += 3 if repeated else 0
        score += 2 if top or bottom else 0
        score += 3 if boiler else 0
        score += 2 if heading_edge else 0
        scored.append((score, block))
    scored.sort(key=lambda pair: (-pair[0], pair[1].page, pair[1].order))
    return [block for _, block in scored[:limit]]


def parse_review_response(content: str, candidates: list[Block]) -> list[str]:
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("审查响应不是 JSON")
        data = json.loads(text[start : end + 1])
    items = data.get("remove") if isinstance(data, dict) else None
    if items is None:
        return []
    valid = {b.id for b in candidates}
    removed: list[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                block_id = item
            elif isinstance(item, dict):
                block_id = str(item.get("id", ""))
            else:
                continue
            if block_id in valid and block_id not in removed:
                removed.append(block_id)
    elif isinstance(items, dict):
        for block_id in items:
            if block_id in valid and block_id not in removed:
                removed.append(block_id)
    return removed


def review_translation(
    config: AppConfig,
    document: DocumentStructure,
    candidates: list[Block],
) -> list[str]:
    if not candidates:
        return []
    client = make_client(config, timeout=config.llm.timeout)
    payload = json.dumps(
        [
            {
                "id": b.id,
                "page": b.page,
                "type": b.kind,
                "text": b.text[:800],
            }
            for b in candidates
        ],
        ensure_ascii=False,
    )
    messages = [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {
            "role": "user",
            "content": _REVIEW_PROMPT.format(title=document.title, payload=payload),
        },
    ]
    last_error: Exception | None = None
    for json_mode in (True, False):
        try:
            kwargs = {
                "model": config.llm.model,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 1800,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            extra = reasoning_kwargs(
                config.translation.reasoning_effort, config.llm.model, "low"
            )
            kwargs.update(extra)
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as exc:
                if not extra or getattr(exc, "status_code", None) not in {400, 422}:
                    raise
                kwargs.pop("reasoning_effort", None)
                response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            if isinstance(content, list):
                content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            return parse_review_response(str(content or ""), candidates)
        except Exception as exc:
            last_error = exc
            log.warning("review attempt failed (json=%s): %s", json_mode, exc)
    raise LLMError(friendly_api_error(last_error) if last_error else "审查请求失败")


def apply_review_removals(document: DocumentStructure, removed_ids: list[str]) -> int:
    wanted = set(removed_ids)
    count = 0
    for block in document.blocks:
        if block.id in wanted:
            block.hidden = True
            # Keep the translation so a forced re-review can restore it.
            count += 1
    document.review_summary = {
        "status": "done",
        "removed": count,
        "removed_ids": sorted(wanted),
        "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    return count
