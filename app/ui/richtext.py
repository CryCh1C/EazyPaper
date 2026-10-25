"""Small, dependency-free HTML renderer for translations and chat."""

from __future__ import annotations

import html
import re
from pathlib import Path

from ..models import Block, DocumentStructure
from ..paths import resource_path
from ..tables import grid_to_html, parse_markdown_table
from . import theme as theme_module

_CSS = """
html { scroll-behavior:auto; }
body { color:#20262E; max-width:1000px; margin:0 auto; padding:18px 24px 70px 24px;
       background:#FFFFFF; }
.block { scroll-margin-top:8px; }
h1 { font-size:20pt; margin:10px 0 6px 0; line-height:1.35; color:#17212B; }
h2 { font-size:15pt; margin:16px 0 5px 0; color:#17212B; border-bottom:1px solid #E6E1D6; padding-bottom:3px; }
h3 { font-size:12.5pt; margin:13px 0 4px 0; color:#1E3A3A; }
p { margin:6px 0 9px 0; line-height:1.75; }
.meta { color:#6F746F; font-size:9pt; }
.byline { color:#6F746F; font-size:10pt; margin-bottom:6px; }
.page-divider { margin:22px 0 8px 0; border-top:1px dashed #D8D2C6; padding-top:7px;
                color:#8A8E88; font-size:8.5pt; letter-spacing:0.5px; }
.block-label { color:#9A8F7C; font-size:8pt; margin-right:7px; }
.paragraph { color:#20262E; }
.caption { color:#555A54; font-size:9.5pt; font-style:italic; margin:5px 0 10px 0; }
.formula { background:#F7F5F0; border-left:3px solid #D6A46A; padding:8px 11px;
           margin:8px 0; font-family:Cambria Math, Consolas, monospace; font-size:10pt;
           white-space:pre-wrap; overflow-wrap:anywhere; color:#3A3328; }
.table-grid { display:grid; gap:1px; margin:8px 0 12px 0; padding:1px;
              background:#D9D3C7; border:1px solid #D9D3C7; font-size:9.5pt; }
.table-grid.wide { font-size:8.5pt; }
.grid-cell { background:#FFFFFF; padding:6px 8px; min-width:0; text-align:left;
             vertical-align:top; overflow-wrap:anywhere; line-height:1.45; }
.grid-cell.header { background:#F4F1EA; font-weight:600; }
.grid-cell.empty { background:#FCFBF8; }
.figure { margin:10px 0 4px 0; text-align:center; }
.figure img { border:1px solid #DED8CC; border-radius:4px; background:#FFFFFF; }
.region-image { margin:9px 0 12px 0; text-align:center; }
.region-image img { max-width:100%; height:auto; background:#FFFFFF; border-radius:3px;
                    border:1px solid #D9D3C7; }
.formula-image img { border:none; padding:1px 0; }
.translation-page { margin:0 0 16px 0; }
.reference { color:#555A54; font-size:9pt; line-height:1.55; }
.reference-title { color:#8A8E88; font-size:8.5pt; margin-top:15px; }
.figure-ph { border:1px dashed #D2CABC; border-radius:6px; padding:14px; text-align:center;
             color:#9A9182; font-size:9pt; margin:8px 0; background:#FBFAF7; }
.untranslated { color:#837C6F; }
.translating { color:#9B9487; font-style:italic; }
.badge { background:#F3EEE3; border-radius:7px; padding:1px 5px; color:#8C7B5F; font-size:7.5pt; }
.source-note { color:#A79D8D; font-size:8pt; }
.toc-note { background:#F8F6F1; border:1px solid #E8E2D7; border-radius:8px; padding:8px 10px;
            color:#6F746F; font-size:9pt; margin:8px 0 14px 0; }
"""

_DARK_CSS = """
body.dark { background:#12161B; color:#E8EBEF; }
body.dark h1, body.dark h2, body.dark h3 { color:#F0F3F6; }
body.dark h2 { border-bottom-color:#303841; }
body.dark .meta, body.dark .byline, body.dark .caption, body.dark .source-note,
body.dark .toc-note, body.dark .reference-title, body.dark .translating { color:#A3ACB6; }
body.dark .paragraph, body.dark .reference { color:#E8EBEF; }
body.dark .page-divider { border-top-color:#303841; color:#8E98A3; }
body.dark .formula { background:#222831; border-left-color:#FBBF24; color:#E8EBEF; }
body.dark .table-grid { background:#303841; border-color:#303841; }
body.dark .grid-cell { background:#1B2027; color:#E8EBEF; }
body.dark .grid-cell.header { background:#222831; }
body.dark .grid-cell.empty { background:#171B21; }
body.dark .region-image img, body.dark .figure img { background:#FFFFFF; border-color:#46515D; }
body.dark .figure-ph { background:#171B21; border-color:#46515D; color:#A3ACB6; }
body.dark .toc-note { background:#171B21; border-color:#303841; color:#A3ACB6; }
body.dark .badge { background:#222831; color:#C4B5FD; }
body.dark a { color:#5EEAD4; }
body.dark .untranslated { color:#A3ACB6; }
body.dark .block-label { color:#C4B5FD; }
"""


def escape(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def inline(text: str) -> str:
    value = escape(text)
    value = re.sub(
        r"`([^`\n]+)`",
        lambda m: f'<code style="background:#F3F0E9;padding:1px 4px;border-radius:4px;">{m.group(1)}</code>',
        value,
    )
    value = re.sub(
        r"\*\*([^*\n]+)\*\*",
        lambda m: f"<b>{m.group(1)}</b>",
        value,
    )
    value = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        lambda m: f"<i>{m.group(1)}</i>",
        value,
    )
    value = re.sub(
        r"\$([^$\n]+)\$",
        lambda m: f'<span style="font-family:Cambria Math,Consolas,monospace;">${m.group(1)}$</span>',
        value,
    )
    return value


def _split_table_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value:
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        escaped = char == "\\" and not escaped
    cells.append("".join(current).strip())
    return cells


def render_table(markdown: str) -> str:
    parsed = parse_markdown_table(markdown)
    if parsed is None or not parsed.rows:
        if any("|" in line for line in markdown.splitlines()):
            return f'<pre class="formula">{escape(markdown)}</pre>'
        return f'<pre class="formula">{escape(markdown)}</pre>'
    rows = parsed.rows
    ncols = max(len(row) for row in rows)
    cells = []
    index = 0
    for r, row in enumerate(rows):
        for c in range(ncols):
            text = row[c] if c < len(row) else ""
            cells.append(
                {"r": r, "c": c, "rs": 1, "cs": 1, "text": text, "cell_index": index}
            )
            index += 1
    return grid_to_html(
        {"nrows": len(rows), "ncols": ncols, "has_header": True, "cells": cells}
    )


def _scaled_css(zoom: float) -> str:
    z = max(0.5, min(3.0, float(zoom)))
    return f"""
    body {{ font-size:{10.2 * z:.2f}pt; }}
    h1 {{ font-size:{20 * z:.2f}pt; }}
    h2 {{ font-size:{15 * z:.2f}pt; }}
    h3 {{ font-size:{12.5 * z:.2f}pt; }}
    .meta {{ font-size:{9 * z:.2f}pt; }}
    .byline {{ font-size:{10 * z:.2f}pt; }}
    .page-divider {{ font-size:{8.5 * z:.2f}pt; }}
    .block-label {{ font-size:{8 * z:.2f}pt; }}
    .caption {{ font-size:{9.5 * z:.2f}pt; }}
    .formula {{ font-size:{10 * z:.2f}pt; }}
    .reference {{ font-size:{9 * z:.2f}pt; }}
    .reference-title {{ font-size:{8.5 * z:.2f}pt; }}
    .figure-ph {{ font-size:{9 * z:.2f}pt; }}
    .toc-note {{ font-size:{9 * z:.2f}pt; }}
    table {{ font-size:{9.5 * z:.2f}pt; }}
    table.wide {{ font-size:{8.5 * z:.2f}pt; }}
    """


def _figure_html(
    block: Block,
    asset_base: Path | None,
    zoom: float = 1.0,
    max_width: int | None = None,
) -> str:
    asset = str(block.meta.get("asset") or "")
    if asset and asset_base is not None:
        candidate = (Path(asset_base) / asset).resolve()
        try:
            uri = candidate.as_uri()
            if candidate.is_file():
                width = int(block.meta.get("display_width") or 560)
                width = int(max(220, min(720, width)) * max(0.5, min(3.0, zoom)))
                if max_width:
                    width = min(width, max_width)
                return (
                    f'<div class="figure"><img src="{escape(uri)}" width="{width}" '
                    f'alt="Figure" /></div>'
                )
        except (OSError, ValueError):
            pass
    return '<div class="figure-ph">图像 / Figure（未提取到图像）</div>'


def _region_image_html(
    block: Block,
    asset_base: Path | None,
    css_class: str,
    alt: str,
    zoom: float = 1.0,
    max_width: int | None = None,
) -> str:
    asset = str(block.meta.get("asset") or "")
    if not asset or asset_base is None:
        return ""
    candidate = (Path(asset_base) / asset).resolve()
    try:
        if not candidate.is_file():
            return ""
        width = int(block.meta.get("display_width") or 520)
        width = int(max(160, min(760, width)) * max(0.5, min(3.0, zoom)))
        if max_width:
            width = min(width, max_width)
        uri = candidate.as_uri()
        return (
            f'<div class="region-image {css_class}">'
            f'<img src="{escape(uri)}" width="{width}" alt="{escape(alt)}" />'
            "</div>"
        )
    except (OSError, ValueError):
        return ""


def render_block(
    block: Block,
    status: str = "done",
    asset_base: Path | None = None,
    zoom: float = 1.0,
    max_image_width: int | None = None,
) -> str:
    label = escape(block.display_label)
    if block.kind == "caption" and re.match(r"^\s*(?:table|tab\.?|表)", block.text, re.IGNORECASE):
        label = "表注"
    translated = block.translation.strip()
    source = block.text.strip()
    if block.kind == "figure":
        return _figure_html(block, asset_base, zoom, max_image_width)
    if block.kind == "table":
        if block.meta.get("region_image"):
            image = _region_image_html(
                block, asset_base, "table-image", "Table", zoom, max_image_width
            )
            if image:
                return image
        if translated.startswith("<div class=\"table-grid") or (
            translated.startswith("<div class=\"table-wrap\"") and "<table" in translated
        ):
            return translated
        grid = block.meta.get("table")
        if isinstance(grid, dict) and grid.get("cells"):
            return grid_to_html(grid)
        content = translated or source
        if not translated and status == "running":
            return f'<div class="translating">表格翻译中…</div>{render_table(source)}'
        return render_table(content)
    if block.kind == "formula":
        if block.meta.get("region_image"):
            image = _region_image_html(
                block, asset_base, "formula-image", "Formula", zoom, max_image_width
            )
            if image:
                return image
        if not source:
            return ""
        tex = source.strip()
        if tex.startswith("$$") and tex.endswith("$$") and len(tex) > 4:
            tex = tex[2:-2].strip()
        elif tex.startswith("\\[") and tex.endswith("\\]"):
            tex = tex[2:-2].strip()
        return (
            f'<div class="formula" data-tex="{html.escape(tex, quote=True)}">'
            f"{escape(source)}</div>"
        )
    if block.kind == "title":
        return f"<h1>{inline(translated or source)}</h1>"
    if block.kind == "heading":
        tag = "h2" if block.level <= 1 else "h3"
        return f"<{tag}>{inline(translated or source)}</{tag}>"
    if block.kind == "caption":
        value = translated or source
        return f'<p class="caption"><span class="block-label">{label}</span>{inline(value)}</p>'
    if block.kind == "reference":
        return f'<p class="reference">{inline(source)}</p>'
    if not translated:
        if status == "running":
            return f'<p class="translating">{inline(source)}</p>'
        return f'<p class="untranslated">{inline(source)}</p>'
    return f'<p class="paragraph">{inline(translated)}</p>'


def _block_html(
    block: Block,
    document: DocumentStructure,
    status: str,
    asset_base: Path | None,
    zoom: float,
    max_image_width: int | None = None,
) -> str:
    if block.hidden:
        return ""
    if block.kind == "title" and block.text.strip() == document.title.strip():
        return ""
    anchor = f'<a name="block-{html.escape(block.id)}"></a>'
    inner = render_block(block, status, asset_base, zoom, max_image_width)
    return (
        f'<div class="block" data-block="{html.escape(block.id)}">'
        f"{anchor}{inner}</div>"
    )


def _render_page(
    page,
    document: DocumentStructure,
    status: str,
    asset_base: Path | None,
    zoom: float,
) -> str:
    blocks = [b for b in page.blocks if not b.hidden]
    out = [
        f'<div class="translation-page"><a name="page-{page.number}"></a>',
        f'<div class="page-divider">第 {page.number} 页 / Page {page.number}</div>',
    ]
    if not blocks:
        out.append("</div>")
        return "".join(out)
    for block in blocks:
        out.append(_block_html(block, document, status, asset_base, zoom))
    out.append("</div>")
    return "".join(out)


def render_document(
    document: DocumentStructure | None,
    status: str = "idle",
    model: str = "",
    message: str = "",
    asset_base: Path | None = None,
    zoom: float = 1.0,
    dark: bool | None = None,
) -> str:
    if dark is None:
        dark = theme_module.is_dark()
    if document is None:
        return _empty_state(
            "打开一篇 PDF",
            "",
            dark=dark,
        )
    zoom = max(0.5, min(3.0, float(zoom)))
    katex_dir = resource_path("katex")
    katex_css = (katex_dir / "katex.min.css")
    katex_js = (katex_dir / "katex.min.js")
    auto_js = (katex_dir / "contrib" / "auto-render.min.js")
    katex_links = ""
    if katex_css.is_file() and katex_js.is_file() and auto_js.is_file():
        katex_links = (
            f'<link rel="stylesheet" href="{katex_css.as_uri()}">'
        )
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        katex_links,
        f"<style>{_CSS}{_DARK_CSS if dark else ''}{_scaled_css(zoom)}</style>",
        f"</head><body class=\"{'dark' if dark else ''}\">",
        '<a name="top"></a>',
        f"<h1>{inline(document.title or document.file_name)}</h1>",
    ]
    if document.authors:
        parts.append(f'<div class="byline">{inline(document.authors)}</div>')
    stats = document.stats()
    meta = f"{stats['pages']} 页 · {stats['translatable']} 个可译块"
    if stats["tables"]:
        meta += f" · {stats['tables']} 个表格"
    if stats["formulas"]:
        meta += f" · {stats['formulas']} 个公式"
    if model:
        meta += f" · {escape(model)}"
    parts.append(f'<div class="meta">{escape(meta)}</div>')
    if message:
        parts.append(f'<div class="toc-note">{inline(message)}</div>')
    visible = 0
    for page in document.pages:
        if any(not block.hidden for block in page.blocks):
            visible += sum(1 for block in page.blocks if not block.hidden)
        parts.append(_render_page(page, document, status, asset_base, zoom))
    if not visible:
        parts.append('<div class="toc-note">未提取到可显示的文本。若为扫描版 PDF，请配置本地 OCR 后重新解析。</div>')
    if katex_css.is_file() and katex_js.is_file() and auto_js.is_file():
        parts.extend(
            [
                f'<script src="{katex_js.as_uri()}"></script>',
                f'<script src="{auto_js.as_uri()}"></script>',
            ]
        )
    parts.append(_MATH_AND_ZOOM_JS)
    parts.append("</body></html>")
    return "".join(parts)


_MATH_AND_ZOOM_JS = r"""
<script>
(function () {
  let currentZoom = 1.0;
  let zoomTarget = 1.0;
  let zoomRaf = 0;
  const ZMIN = 0.5, ZMAX = 2.5;

  function topBlock() {
    let best = "", bestTop = -1e9;
    document.querySelectorAll("[data-block]").forEach(function (el) {
      const t = el.getBoundingClientRect().top;
      if (t <= 80 && t > bestTop) { best = el.getAttribute("data-block"); bestTop = t; }
    });
    return best;
  }

  function scrollToBlock(id) {
    if (!id) return;
    const el = document.querySelector('[data-block="' + id + '"]');
    if (el) el.scrollIntoView({block: "start"});
  }

  function renderMath() {
    if (window.katex) {
      document.querySelectorAll(".formula[data-tex]").forEach(function (el) {
        try {
          katex.render(el.getAttribute("data-tex") || el.textContent, el,
                       {displayMode: true, throwOnError: false, strict: false});
        } catch (e) {}
      });
    }
    if (window.renderMathInElement) {
      try {
        renderMathInElement(document.body, {
          delimiters: [
            {left: "$$", right: "$$", display: true},
            {left: "\\[", right: "\\]", display: true},
            {left: "$", right: "$", display: false},
            {left: "\\(", right: "\\)", display: false}
          ],
          throwOnError: false,
          ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"]
        });
      } catch (e) {}
    }
  }

  function setZoom(value, notify) {
    const anchor = topBlock();
    currentZoom = Math.max(ZMIN, Math.min(ZMAX, value));
    zoomTarget = currentZoom;
    document.body.style.zoom = String(currentZoom);
    requestAnimationFrame(function () {
      scrollToBlock(anchor);
      if (notify) document.title = "EASYPAPER_ZOOM:" + Math.round(currentZoom * 100);
    });
  }

  window.__topBlock = topBlock;
  window.__scrollToBlock = scrollToBlock;
  window.__zoomBy = function (step) {
    zoomTarget = Math.max(ZMIN, Math.min(ZMAX, zoomTarget * (step > 0 ? 1.1 : 0.9)));
    if (zoomRaf) return;
    zoomRaf = requestAnimationFrame(function () {
      zoomRaf = 0;
      setZoom(zoomTarget, true);
    });
  };
  window.__setZoom = function (value) { setZoom(value, true); };
  window.__afterLoad = function (anchor) {
    renderMath();
    if (anchor) setTimeout(function () { scrollToBlock(anchor); }, 40);
    document.title = "EASYPAPER_ZOOM:" + Math.round(currentZoom * 100);
  };

  window.addEventListener("wheel", function (event) {
    if (event.ctrlKey) {
      event.preventDefault();
      window.__zoomBy(event.deltaY > 0 ? -1 : 1);
    } else if (event.shiftKey) {
      event.preventDefault();
      window.scrollBy(event.deltaY, 0);
    }
  }, {passive: false});

  window.addEventListener("load", function () {
    if (window.__afterLoad) window.__afterLoad("");
  });
})();
</script>
"""


def _empty_state(title: str, subtitle: str, dark: bool = False) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'></head>
    <body class="{'dark' if dark else ''}">
    <style>{_CSS}{_DARK_CSS if dark else ''}
    .empty {{ margin-top:14vh; text-align:center; color:#8A8E88; }}
    .empty h2 {{ border:none; font-size:17pt; color:#6F746F; }}
    body.dark .empty, body.dark .empty h2 {{ color:#A3ACB6; }}
    </style>
    <div class='empty'><h2>{escape(title)}</h2>{f'<p>{escape(subtitle)}</p>' if subtitle else ''}</div>
    </body></html>"""


_CHAT_CSS = """
body { color:#20262E; }
.msg { margin:9px 0 14px 0; }
.role { font-size:8pt; font-weight:700; letter-spacing:0.4px; color:#8A8E88; margin-bottom:4px; }
.user .bubble { background:#E7F3F0; border:1px solid #CCE5E0; border-radius:10px; padding:8px 10px; }
.assistant .bubble { background:#FFFFFF; border:1px solid #E8E3D8; border-radius:10px; padding:8px 10px; }
.bubble p { margin:4px 0 7px 0; line-height:1.7; }
.bubble ul, .bubble ol { margin:4px 0 8px 20px; }
.bubble li { margin:2px 0; line-height:1.65; }
.bubble pre { background:#F5F2EC; border:1px solid #E6E0D4; border-radius:6px; padding:8px;
              white-space:pre-wrap; font-family:Consolas,monospace; font-size:9pt; }
.bubble code { background:#F3F0E9; padding:1px 4px; border-radius:4px; font-family:Consolas,monospace; }
.quote { border-left:3px solid #D6A46A; background:#FBF7F1; padding:6px 9px; color:#5C5447;
         margin:6px 0; font-size:9.5pt; }
.cursor { color:#0F766E; font-weight:bold; }
.meta { color:#9A9E98; font-size:8pt; margin-top:4px; }
.empty { margin-top:12vh; text-align:center; color:#969A94; padding:18px; }
.empty h3 { color:#6F746F; font-size:13pt; }
.chips { color:#8A8E88; font-size:8pt; }
"""

_CHAT_DARK_CSS = """
body.dark { color:#E8EBEF; background:#12161B; }
body.dark .role, body.dark .meta, body.dark .chips { color:#A3ACB6; }
body.dark .user .bubble { background:#183C39; border-color:#2A5F59; }
body.dark .assistant .bubble { background:#1B2027; border-color:#303841; }
body.dark .bubble pre { background:#222831; border-color:#303841; }
body.dark .bubble code { background:#2A313A; }
body.dark .quote { background:#222831; border-left-color:#FBBF24; color:#D8DEE5; }
body.dark .cursor { color:#5EEAD4; }
body.dark .empty, body.dark .empty h3 { color:#A3ACB6; }
body.dark a { color:#5EEAD4; }
"""


def render_chat_messages(
    messages: list[dict],
    streaming: bool = False,
    meta: str = "",
    dark: bool | None = None,
) -> str:
    if dark is None:
        dark = theme_module.is_dark()
    extra = _CHAT_DARK_CSS if dark else ""
    body_class = "dark" if dark else ""
    if not messages:
        return (
            f"<html><head><meta charset='utf-8'><style>{_CHAT_CSS}{extra}</style></head>"
            f"<body class='{body_class}'><div class='empty'><h3>基于全文提问</h3></div></body></html>"
        )
    parts = [
        f"<html><head><meta charset='utf-8'><style>{_CHAT_CSS}{extra}</style></head>"
        f"<body class='{body_class}'>",
    ]
    for index, message in enumerate(messages):
        role = message.get("role", "assistant")
        content = str(message.get("content", ""))
        name = "你" if role == "user" else "EasyPaper AI"
        quote = str(message.get("quote", "") or "")
        body = _render_chat_text(content)
        if quote:
            body = f'<div class="quote">{escape(quote)}</div>' + body
        cursor = ""
        if streaming and index == len(messages) - 1 and role == "assistant":
            cursor = '<span class="cursor">▌</span>'
        parts.append(
            f'<div class="msg {role}"><div class="role">{escape(name)}</div>'
            f'<div class="bubble">{body}{cursor}</div></div>'
        )
    if meta:
        parts.append(f'<div class="meta">{escape(meta)}</div>')
    parts.append("</body></html>")
    return "".join(parts)


def _render_chat_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n")
    blocks = re.split(r"\n{2,}", text)
    out: list[str] = []
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[^\n]*\n?", "", stripped)
            stripped = re.sub(r"\n?```$", "", stripped)
            out.append(f"<pre>{escape(stripped)}</pre>")
            continue
        if re.match(r"^#{1,4}\s+", stripped):
            title = re.sub(r"^#{1,4}\s+", "", stripped)
            out.append(f"<p><b>{inline(title)}</b></p>")
            continue
        lines = stripped.splitlines()
        if lines and all(re.match(r"^\s*[-*+]\s+", line) for line in lines):
            items = [re.sub(r"^\s*[-*+]\s+", "", line) for line in lines]
            out.append("<ul>" + "".join(f"<li>{inline(item)}</li>" for item in items) + "</ul>")
            continue
        if lines and all(re.match(r"^\s*\d+[.)]\s+", line) for line in lines):
            items = [re.sub(r"^\s*\d+[.)]\s+", "", line) for line in lines]
            out.append("<ol>" + "".join(f"<li>{inline(item)}</li>" for item in items) + "</ol>")
            continue
        paragraphs = [line for line in lines if line.strip()]
        out.append("<p>" + "<br>".join(inline(line) for line in paragraphs) + "</p>")
    return "".join(out) if out else "<p></p>"
