"""OpenAI-compatible translation, retrieval context and streaming chat."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from .config import AppConfig
from .logging_setup import get_logger
from .models import Block, DocumentStructure, TRANSLATABLE_KINDS

log = get_logger("llm")

_REASONING_MODEL = re.compile(
    r"(?:^|[/:_-])(o1|o3|o4|gpt-5|gpt5)(?:$|[/:_.-])", re.IGNORECASE
)


def resolve_reasoning_effort(effort: str, model: str, auto_level: str) -> str:
    value = (effort or "auto").strip().lower()
    if value in {"off", "default", "none"}:
        return ""
    if value == "auto":
        return auto_level if _REASONING_MODEL.search(model or "") else ""
    if value in {"minimal", "low", "medium", "high"}:
        return value
    return ""


def reasoning_kwargs(effort: str, model: str, auto_level: str) -> dict[str, str]:
    level = resolve_reasoning_effort(effort, model, auto_level)
    return {"reasoning_effort": level} if level else {}


class LLMError(RuntimeError):
    pass


class TranslationParseError(LLMError):
    pass


def normalize_base_url(value: str) -> str:
    """Accept the common bases people paste, without over-guessing paths."""

    url = (value or "").strip().rstrip("/")
    if not url:
        return "https://api.openai.com/v1"
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url


def make_client(config: AppConfig, timeout: int | None = None) -> OpenAI:
    if not config.llm.api_key.strip():
        raise LLMError("尚未配置 API Key。请点击左下角“设置”填写 OpenAI 兼容接口信息。")
    if not config.llm.model.strip():
        raise LLMError("尚未配置模型名称。")
    return OpenAI(
        api_key=config.llm.api_key.strip(),
        base_url=normalize_base_url(config.llm.base_url),
        timeout=float(timeout or config.llm.timeout),
        max_retries=2,
    )


def friendly_api_error(exc: Exception) -> str:
    name = type(exc).__name__
    if isinstance(exc, APITimeoutError):
        return "请求超时：接口没有在设定时间内响应。"
    if isinstance(exc, APIConnectionError):
        return "无法连接 API：请检查 base_url、网络、代理或本地服务是否启动。"
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", "?")
        detail = ""
        try:
            body = exc.response.json()
            detail = str(body.get("error", {}).get("message") or body.get("message") or "")
        except Exception:
            detail = str(getattr(exc, "message", "") or "")
        hints = {
            400: "请求格式不被服务端接受（部分兼容服务不支持 JSON 模式）。",
            401: "鉴权失败：API Key 无效。",
            403: "没有访问权限（403）。",
            404: "接口路径或模型不存在（404）：base_url 通常应以 /v1 结尾。",
            429: "请求过于频繁或额度不足（429）。",
            500: "服务端内部错误（500）。",
            502: "上游网关错误（502）。",
            503: "服务暂不可用（503）。",
        }
        hint = hints.get(int(status) if str(status).isdigit() else 0, "")
        return f"API 返回 {status}。{hint} {detail}".strip()
    return f"{name}: {exc}"


def test_connection(config: AppConfig) -> dict:
    """Blocking reachability / auth / model check.  Run in a worker thread."""

    started = time.perf_counter()
    result = {
        "ok": False,
        "message": "",
        "latency_ms": 0,
        "models": [],
        "model_found": False,
        "detail": "",
    }
    try:
        client = make_client(config, timeout=20)
        models: list[str] = []
        try:
            page = client.models.list()
            models = [item.id for item in getattr(page, "data", [])]
            result["models"] = models[:200]
            result["model_found"] = config.llm.model in models if models else False
        except Exception as list_error:
            # Some compatible services do not implement /models.  Try a
            # one-token chat completion as the authoritative probe.
            log.info("models.list failed, falling back to chat probe: %s", list_error)
            client.chat.completions.create(
                model=config.llm.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
        latency = int((time.perf_counter() - started) * 1000)
        result["ok"] = True
        result["latency_ms"] = latency
        if result["models"] and not result["model_found"]:
            result["message"] = f"连接成功（{latency} ms），但模型列表中未找到 “{config.llm.model}”；仍可尝试调用。"
        else:
            result["message"] = f"连接成功，鉴权通过（{latency} ms）。"
        return result
    except Exception as exc:
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        result["message"] = friendly_api_error(exc)
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------

TRANSLATION_SYSTEM = """你是一名严谨、专业的学术论文翻译引擎。你的任务是把论文结构块翻译成目标语言。
必须遵守：
1. 只输出用户要求的 JSON，不要输出解释、前言或 Markdown 围栏。
2. 严格保留每个块的 id，不合并、不拆分、不遗漏、不改变顺序。
3. 保持学术语体、术语准确；首次出现的重要术语可在译文中保留英文原词。
4. 公式、变量、引用编号、URL、代码、LaTeX 必须原样保留；不要翻译作者姓名和参考文献条目。
5. 表格必须继续使用 Markdown 表格语法，保留行列、分隔线和数字；只翻译自然语言单元格。
6. 图注/表注保留 Figure/Table/图/表的编号与结构。
7. 译文只翻译该块自身内容，不要补充原文没有的信息。"""


TRANSLATION_JSON_PROMPT = """目标语言：{target}
论文标题：{title}

待翻译块（JSON 数组，每项含 id/type/text）：
{payload}

请返回严格 JSON：
{{"translations":[{{"id":"原 id","text":"译文"}}]}}
不得遗漏任何 id。"""


TRANSLATION_MARKER_PROMPT = """目标语言：{target}
论文标题：{title}

请逐块翻译下面的内容。输出格式必须严格为：
@@BLOCK id=块id@@
译文
@@END@@
每个块一段，顺序与输入一致，不要输出任何其他文字。

输入：
{payload}"""


def make_chunks(blocks: Iterable[Block], max_chars: int) -> list[list[Block]]:
    chunks: list[list[Block]] = []
    current: list[Block] = []
    size = 0
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        block_size = len(text) + len(block.id) + 40
        if current and size + block_size > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(block)
        size += block_size
    if current:
        chunks.append(current)
    return chunks


class TranslationService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.client = make_client(config)

    def translate_blocks(self, blocks: list[Block], title: str) -> dict[str, str]:
        """Translate blocks while keeping table grids mechanically stable.

        A Markdown table is expanded into synthetic cell blocks, translated,
        and rebuilt from the original row/column grid.  This prevents a model
        from dropping pipes, merging cells, or changing the number of rows.
        """

        from .tables import grid_to_html, parse_markdown_table, to_markdown

        original_ids = {b.id for b in blocks}
        request_blocks: list[Block] = []
        grid_specs: list[tuple[Block, dict, list[tuple[int, str]]]] = []
        table_specs: list[tuple[Block, object, list[tuple[int, int, str]]]] = []
        for block in blocks:
            if block.kind != "table":
                request_blocks.append(block)
                continue
            grid = block.meta.get("table")
            if isinstance(grid, dict) and grid.get("cells"):
                cell_ids: list[tuple[int, str]] = []
                for cell in grid["cells"]:
                    text = str(cell.get("text", ""))
                    cell_index = int(cell.get("cell_index", 0))
                    if not text.strip():
                        continue
                    cell_id = f"{block.id}::grid_{cell_index}"
                    request_blocks.append(
                        Block(
                            id=cell_id,
                            page=block.page,
                            order=block.order,
                            kind="table_cell",
                            text=text,
                        )
                    )
                    cell_ids.append((cell_index, cell_id))
                grid_specs.append((block, grid, cell_ids))
                continue
            parsed = parse_markdown_table(block.text)
            if parsed is None or len(parsed.rows) < 1:
                request_blocks.append(block)
                continue
            cells: list[tuple[int, int, str]] = []
            for row_index, row in enumerate(parsed.rows):
                for column_index, cell in enumerate(row):
                    if not cell.strip():
                        continue
                    cell_id = f"{block.id}::cell_{row_index}_{column_index}"
                    request_blocks.append(
                        Block(
                            id=cell_id,
                            page=block.page,
                            order=block.order,
                            kind="table_cell",
                            text=cell,
                        )
                    )
                    cells.append((row_index, column_index, cell_id))
            table_specs.append((block, parsed, cells))

        mapping = self._translate_request(request_blocks, title)
        for block, grid, cells in grid_specs:
            translated_cells = {
                cell_index: mapping[cell_id]
                for cell_index, cell_id in cells
                if cell_id in mapping
            }
            mapping[block.id] = grid_to_html(grid, translated_cells)
        for block, parsed, cells in table_specs:
            rebuilt = [list(row) for row in parsed.rows]
            for row_index, column_index, cell_id in cells:
                if cell_id in mapping:
                    rebuilt[row_index][column_index] = mapping[cell_id]
            mapping[block.id] = to_markdown(rebuilt, parsed.has_header)
        return {key: value for key, value in mapping.items() if key in original_ids}

    def _translate_request(self, blocks: list[Block], title: str) -> dict[str, str]:
        if not blocks:
            return {}
        payload = json.dumps(
            [{"id": b.id, "type": b.kind, "text": b.text} for b in blocks],
            ensure_ascii=False,
        )
        messages = [
            {"role": "system", "content": TRANSLATION_SYSTEM},
            {
                "role": "user",
                "content": TRANSLATION_JSON_PROMPT.format(
                    target=self.config.translation.target_language,
                    title=title,
                    payload=payload,
                ),
            },
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                content = self._chat(
                    messages,
                    temperature=self.config.llm.temperature,
                    max_tokens=self.config.llm.max_tokens,
                    json_mode=attempt == 0 and self.config.llm.use_json_mode,
                )
                return self._parse_json(content, blocks)
            except TranslationParseError as exc:
                last_error = exc
                log.warning("JSON translation parse failed (attempt %s): %s", attempt + 1, exc)
            except APIStatusError as exc:
                if attempt == 0 and self.config.llm.use_json_mode and exc.status_code in {400, 404, 422}:
                    last_error = exc
                    continue
                raise
        # Robust fallback for servers that cannot produce clean JSON.
        marker_prompt = TRANSLATION_MARKER_PROMPT.format(
            target=self.config.translation.target_language,
            title=title,
            payload="\n\n".join(f"id={b.id}\ntext={b.text}" for b in blocks),
        )
        content = self._chat(
            [
                {"role": "system", "content": TRANSLATION_SYSTEM},
                {"role": "user", "content": marker_prompt},
            ],
            temperature=self.config.llm.temperature,
            max_tokens=self.config.llm.max_tokens,
            json_mode=False,
        )
        return self._parse_markers(content, blocks, last_error)

    def _chat(
        self,
        messages,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        reasoning_effort: str | None = None,
    ) -> str:
        kwargs = {
            "model": self.config.llm.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        effort = (
            self.config.translation.reasoning_effort
            if reasoning_effort is None
            else reasoning_effort
        )
        extra = reasoning_kwargs(effort, self.config.llm.model, "low")
        kwargs.update(extra)
        try:
            response = self.client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if not extra or exc.status_code not in {400, 422}:
                raise
            log.info("model rejected reasoning_effort; retrying without it")
            kwargs.pop("reasoning_effort", None)
            response = self.client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMError("API 返回了空响应。")
        content = choices[0].message.content
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if not content or not str(content).strip():
            raise LLMError("API 返回了空译文。")
        return str(content)

    @staticmethod
    def _parse_json(content: str, blocks: list[Block]) -> dict[str, str]:
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise TranslationParseError("译文中没有 JSON 对象")
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise TranslationParseError(f"JSON 解析失败: {exc}") from exc

        items = None
        if isinstance(data, dict):
            for key in ("translations", "blocks", "items", "result", "results"):
                if isinstance(data.get(key), (list, dict)):
                    items = data[key]
                    break
            if items is None and all(isinstance(v, str) for v in data.values()):
                items = data
        elif isinstance(data, list):
            items = data
        if items is None:
            raise TranslationParseError("JSON 结构中没有 translations")

        mapping: dict[str, str] = {}
        if isinstance(items, dict):
            for key, value in items.items():
                if isinstance(value, str):
                    mapping[str(key)] = value
                elif isinstance(value, dict) and value.get("text") is not None:
                    mapping[str(value.get("id", key))] = str(value["text"])
        else:
            for item in items:
                if isinstance(item, dict) and item.get("id") is not None and item.get("text") is not None:
                    mapping[str(item["id"])] = str(item["text"])
        wanted = {b.id for b in blocks}
        mapping = {k: v for k, v in mapping.items() if k in wanted and v.strip()}
        if not mapping:
            raise TranslationParseError("没有可用的 id→译文映射")
        if set(mapping) != wanted:
            missing = sorted(wanted - set(mapping))
            raise TranslationParseError(f"译文缺失 {len(missing)} 块（例如 {', '.join(missing[:3])}）")
        return mapping

    @staticmethod
    def _parse_markers(
        content: str, blocks: list[Block], previous: Exception | None = None
    ) -> dict[str, str]:
        pattern = re.compile(
            r"@@\s*BLOCK\s+id\s*=\s*([^\s@]+)\s*@@\s*(.*?)\s*@@\s*END\s*@@",
            re.DOTALL | re.IGNORECASE,
        )
        mapping = {match.group(1): match.group(2).strip() for match in pattern.finditer(content)}
        wanted = {b.id for b in blocks}
        mapping = {k: v for k, v in mapping.items() if k in wanted and v}
        if set(mapping) != wanted:
            missing = sorted(wanted - set(mapping))
            detail = f"（之前的 JSON 错误：{previous}）" if previous else ""
            raise TranslationParseError(
                f"模型未按块标记完整返回译文，缺少 {len(missing)} 块（例如 {', '.join(missing[:3])}）{detail}"
            )
        return mapping


# --------------------------------------------------------------------------
# Full-text Q&A context
# --------------------------------------------------------------------------

CHAT_SYSTEM = """你是 EasyPaper 的论文阅读助手。你只能依据用户提供的论文原文/译文片段回答，不能编造论文中没有的内容。
回答要求：
1. 先直接回答，再给依据；必要时分点说明。
2. 每个关键结论尽量标注来源页码与章节，例如（第 3 页，Method）。
3. 区分事实、作者观点和你的推断；不确定时明确说明“论文未给出/无法确定”。
4. 保留关键术语的英文原词，首次出现时可用“中文（English）”形式。
5. 使用与用户提问相同的语言，默认中文；数学公式用 LaTeX 行内形式。
6. 如果上下文不足以回答，明确说明缺哪部分信息，不要凭常识补全。"""


def _tokenize_query(text: str) -> list[str]:
    lower = text.lower()
    words = re.findall(r"[a-z][a-z0-9_\-]{2,}", lower)
    cjk = re.findall(r"[\u3400-\u9fff]", text)
    bigrams = ["".join(cjk[i : i + 2]) for i in range(max(0, len(cjk) - 1))]
    return list(dict.fromkeys(words + bigrams))[:120]


@dataclass
class ContextChunk:
    ids: list[str]
    text: str
    page_min: int
    page_max: int
    order: int


def build_document_chunks(document: DocumentStructure, max_chars: int = 2800) -> list[ContextChunk]:
    chunks: list[ContextChunk] = []
    current_blocks: list[Block] = []
    current_len = 0
    for block in document.blocks:
        if block.hidden or not block.text.strip():
            continue
        text = block.text.strip()
        piece = f"[p.{block.page}][{block.display_label}] {text}"
        if block.translation.strip():
            piece += f"\n[译文] {block.translation.strip()}"
        if current_blocks and current_len + len(piece) > max_chars:
            chunks.append(_make_chunk(current_blocks))
            current_blocks = []
            current_len = 0
        current_blocks.append(block)
        current_len += len(piece) + 2
    if current_blocks:
        chunks.append(_make_chunk(current_blocks))
    return chunks


def _make_chunk(blocks: list[Block]) -> ContextChunk:
    parts: list[str] = []
    for block in blocks:
        piece = f"[p.{block.page}][{block.display_label}] {block.text.strip()}"
        if block.translation.strip():
            piece += f"\n[译文] {block.translation.strip()}"
        parts.append(piece)
    pages = [b.page for b in blocks]
    return ContextChunk(
        ids=[b.id for b in blocks],
        text="\n\n".join(parts),
        page_min=min(pages),
        page_max=max(pages),
        order=blocks[0].order,
    )


def build_chat_context(
    document: DocumentStructure,
    config: AppConfig,
    question: str,
    selected_text: str = "",
    selected_page: int = 0,
) -> tuple[str, dict]:
    budget = max(6000, config.chat.context_chars)
    chunks = build_document_chunks(document)
    meta: dict = {"chunks": len(chunks), "used": 0, "retrieval": False, "chars": 0}
    if not chunks:
        return "", meta

    full_size = sum(len(c.text) + 2 for c in chunks)
    if full_size <= budget:
        selected = chunks
        meta["used"] = len(chunks)
    else:
        meta["retrieval"] = True
        terms = _tokenize_query(question + " " + selected_text)
        scored: list[tuple[float, ContextChunk]] = []
        for chunk in chunks:
            lower = chunk.text.lower()
            score = 0.0
            for term in terms:
                occurrences = lower.count(term)
                if occurrences:
                    score += min(5, occurrences) * (2.0 if len(term) > 3 else 1.0)
            if selected_page and chunk.page_min <= selected_page <= chunk.page_max:
                score += 4.0
            if "abstract" in lower[:300] or "摘要" in lower[:300]:
                score += 2.0
            if chunk.order < 4:
                score += 1.5
            scored.append((score, chunk))
        scored.sort(key=lambda pair: (-pair[0], pair[1].order))
        selected = []
        used = 0
        for _, chunk in scored:
            if used + len(chunk.text) > budget and selected:
                break
            selected.append(chunk)
            used += len(chunk.text) + 2
        if not selected:
            selected = chunks[:1]
        selected.sort(key=lambda c: c.order)
        meta["used"] = len(selected)

    context = "\n\n---\n\n".join(c.text for c in selected)
    if selected_text.strip():
        context = (
            f"【用户在原文中选中的内容（第 {selected_page or '?'} 页）】\n"
            f"{selected_text.strip()}\n\n"
            f"【论文全文上下文】\n{context}"
        )
    meta["chars"] = len(context)
    return context, meta


def build_chat_messages(
    config: AppConfig,
    context: str,
    question: str,
    history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": CHAT_SYSTEM}]
    if history:
        keep = max(0, config.chat.history_turns) * 2
        for item in history[-keep:]:
            role = item.get("role")
            content = item.get("content", "")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content[-6000:]})
    user_content = (
        "以下是论文内容（原文或已有译文，按阅读顺序，含页码）：\n\n"
        f"{context}\n\n"
        "--------------------\n"
        f"用户问题：{question.strip()}\n"
        "请基于上述论文内容回答，并标注依据页码。"
    )
    messages.append({"role": "user", "content": user_content})
    return messages


def stream_chat(
    config: AppConfig,
    messages: list[dict[str, str]],
    on_delta: Callable[[str], None],
    cancelled: Callable[[], bool] | None = None,
) -> str:
    client = make_client(config)
    kwargs = {
        "model": config.llm.model,
        "messages": messages,
        "temperature": config.chat.temperature,
        "max_tokens": config.chat.max_tokens,
        "stream": True,
    }
    extra = reasoning_kwargs(config.chat.reasoning_effort, config.llm.model, "low")
    kwargs.update(extra)
    pieces: list[str] = []
    stream = None
    try:
        try:
            stream = client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if not extra or exc.status_code not in {400, 422}:
                raise
            log.info("chat model rejected reasoning_effort; retrying without it")
            kwargs.pop("reasoning_effort", None)
            stream = client.chat.completions.create(**kwargs)
        for event in stream:
            if cancelled and cancelled():
                try:
                    stream.close()
                except Exception:
                    pass
                break
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            delta = choices[0].delta
            content = getattr(delta, "content", None)
            if isinstance(content, list):
                content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            if content:
                pieces.append(str(content))
                on_delta(str(content))
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    return "".join(pieces)


def estimate_tokens(text: str) -> int:
    """Cheap cross-language estimate used only for the status bar."""

    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    return max(1, int((len(text) - cjk) / 3.6 + cjk / 1.7) + int(math.sqrt(max(len(text), 1)) / 10))
