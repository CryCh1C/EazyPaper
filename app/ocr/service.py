"""Application-facing OCR orchestration.

The GUI process never imports torch or transformers.  ``OcrService`` starts the
external Python 3.12 OCR runtime and talks to ``tools/oxr_worker.py`` over a
single JSON stdin/stdout request.  This keeps the packaged ``_internal`` small
while preserving a provider-neutral application boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from ..config import AppConfig
from ..logging_setup import get_logger
from ..models import Block, DocumentStructure
from ..paths import HOME, find_ocr_python
import pymupdf
from .base import OcrPage

log = get_logger("ocr.service")


def _worker_script() -> Path:
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", HOME))
        bundled = meipass / "tools" / "oxr_worker.py"
        if bundled.is_file():
            return bundled
    return HOME / "tools" / "oxr_worker.py"


class OcrService:
    """Runs the external OXR worker on demand."""

    def __init__(self, config: AppConfig):
        self.config = config

    def close(self) -> None:
        # The worker is short-lived; nothing to keep alive.
        pass

    def _run(self, request: dict, timeout: int | None = None) -> dict:
        python = find_ocr_python(self.config.ocr.python_path)
        if python is None:
            raise RuntimeError(
                "未找到 OXR 运行时。请运行 scripts/setup_ocr.ps1 -Gpu，"
                "或把 .venv\\Scripts\\python.exe 放到应用目录。"
            )
        script = _worker_script()
        if not script.is_file():
            raise FileNotFoundError(f"OXR worker 脚本不存在：{script}")
        model_dir = Path(self.config.ocr.model_dir).expanduser()
        if not model_dir.is_absolute():
            model_dir = HOME / model_dir
        payload = dict(request)
        payload.setdefault("max_new_tokens", int(self.config.ocr.max_new_tokens))
        cmd = [
            str(python),
            str(script),
            "--model-dir",
            str(model_dir.resolve()),
            "--device",
            str(self.config.ocr.device),
            "--max-pixels",
            str(int(self.config.ocr.max_pixels)),
            "--dpi",
            str(int(self.config.ocr.dpi)),
            "--max-new-tokens",
            str(int(self.config.ocr.max_new_tokens)),
        ]
        env = os.environ.copy()
        env["EASYPAPER_HOME"] = str(HOME)
        env["HF_HOME"] = str(HOME / ".hf-cache")
        env["TORCH_HOME"] = str(HOME / ".torch-cache")
        env["TMP"] = str(HOME / ".tmp")
        env["TEMP"] = str(HOME / ".tmp")
        result = subprocess.run(
            cmd,
            env=env,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or int(self.config.ocr.timeout_seconds),
            cwd=str(HOME),
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip().splitlines()
            raise RuntimeError(message[-1] if message else "OXR worker 执行失败")
        line = ""
        for candidate in reversed((result.stdout or "").splitlines()):
            if candidate.strip().startswith("{"):
                line = candidate.strip()
                break
        if not line:
            raise RuntimeError("OXR worker 没有返回 JSON")
        data = json.loads(line)
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or "OXR worker 失败"))
        return data

    # --------------------------------------------------------------- layout
    def locate_formulas(self, pdf_path: Path, pages: list[int]) -> dict[int, list[dict]]:
        """Ask OXR's D-FINE layout model for Formula boxes."""
        if not pages:
            return {}
        data = self._run(
            {"cmd": "layout", "pdf": str(pdf_path), "pages": [int(p) for p in pages]}
        )
        result: dict[int, list[dict]] = {}
        for number, values in (data.get("formulas") or {}).items():
            result[int(number)] = [item for item in values if isinstance(item, dict)]
        return result

    def apply_layout_formulas(
        self,
        document: DocumentStructure,
        pdf_path: Path,
        formulas: dict[int, list[dict]],
        asset_dir: Path,
    ) -> int:
        """Replace PDF formula heuristics with OXR-located single-line boxes."""
        if not formulas:
            return 0
        pdf = pymupdf.open(str(pdf_path))
        added = 0
        try:
            for page in document.pages:
                if page.ocr_done:
                    # Page-level OCR blocks have no individual coordinates to match.
                    continue
                boxes = formulas.get(page.number)
                if not boxes:
                    # OXR layout may miss a page; retain the existing image
                    # formula fallback instead of dropping formulas entirely.
                    continue
                pdf_page = pdf.load_page(page.number - 1)
                used_assets = {block.meta.get("asset") for block in page.blocks}
                asset_number = 0
                for item in boxes:
                    bbox = [float(v) for v in item.get("bbox", [])]
                    if len(bbox) != 4:
                        continue
                    if _inside_text_block(page, bbox):
                        continue
                    if bbox[2] - bbox[0] < 4 or bbox[3] - bbox[1] < 4:
                        continue
                    replaced = [
                        old
                        for old in page.blocks
                        if old.kind == "formula" and old.source != "oxr_layout"
                        and (_overlaps(old.bbox, bbox) or _overlaps(bbox, old.bbox))
                    ]
                    if any(old.source == "oxr_layout" and _overlaps(old.bbox, bbox) for old in page.blocks):
                        continue
                    rect = pymupdf.Rect(bbox) + (-2, -2, 2, 2)
                    for old in replaced:
                        if old.meta.get("merged_formula") and old.meta.get("asset_bbox"):
                            rect |= pymupdf.Rect(old.meta["asset_bbox"])
                    rect &= pdf_page.rect
                    asset_number += 1
                    relative = f"assets/p{page.number:03d}_layout_formula{asset_number}.png"
                    while relative in used_assets:
                        asset_number += 1
                        relative = f"assets/p{page.number:03d}_layout_formula{asset_number}.png"
                    target = asset_dir.parent / relative
                    try:
                        if rect.is_empty:
                            continue
                        factor = min(3.0, 1800.0 / max(rect.width, rect.height, 1.0))
                        pix = pdf_page.get_pixmap(
                            matrix=pymupdf.Matrix(factor, factor),
                            clip=rect,
                            alpha=False,
                            colorspace=pymupdf.csRGB,
                        )
                        target.parent.mkdir(parents=True, exist_ok=True)
                        pix.save(str(target))
                    except Exception:
                        continue
                    display = int(max(180, min(700, rect.width * 1.1)))
                    text = " ".join(old.text for old in replaced).strip()
                    if not text:
                        text = " ".join(pdf_page.get_text("text", clip=rect).split())
                    block = Block(
                        id="",
                        page=page.number,
                        order=0,
                        kind="formula",
                        text=text,
                        bbox=list(rect),
                        source="oxr_layout",
                        meta={
                            "region_image": True,
                            "asset": relative,
                            "asset_width": pix.width,
                            "asset_height": pix.height,
                            "display_width": display,
                            "layout_score": item.get("score", 0),
                        },
                    )
                    replaced_ids = {id(old) for old in replaced}
                    page.blocks = [old for old in page.blocks if id(old) not in replaced_ids]
                    page.blocks.append(block)
                    added += 1
                if boxes:
                    page.blocks = _order_blocks(page)
        finally:
            pdf.close()
        if added:
            document.reindex()
        return added

    # ------------------------------------------------------------- scanned text
    def parse_scanned_pages(
        self,
        document: DocumentStructure,
        pdf_path: Path,
        pages: list[int],
        progress: Callable[[str, int, int, str], None] | None = None,
    ) -> dict[int, OcrPage]:
        if not pages:
            return {}
        if progress:
            progress("ocr", 0, len(pages), f"OXR 正在解析 {len(pages)} 个扫描页…")
        data = self._run({"pdf": str(pdf_path), "pages": [int(p) for p in pages]})
        elapsed = float(data.get("elapsed", 0.0) or 0.0)
        result: dict[int, OcrPage] = {}
        for number, text in (data.get("pages") or {}).items():
            result[int(number)] = OcrPage(
                page=int(number),
                text=str(text),
                engine="OXR-1.0",
                elapsed=elapsed,
            )
        if progress:
            progress("ocr", len(pages), len(pages), "OXR 扫描页解析完成")
        return result

    # ----------------------------------------------------------------- formulas
    def enhance_formulas(
        self,
        document: DocumentStructure,
        pdf_path: Path,
        asset_dir: Path,
        progress: Callable[[str, int, int, str], None] | None = None,
    ) -> int:
        formulas = [b for b in document.blocks if b.kind == "formula"]
        if not formulas:
            return 0
        items = [
            {"id": block.id, "page": block.page, "bbox": block.bbox}
            for block in formulas
        ]
        data = self._run({"pdf": str(pdf_path), "formulas": items})
        mapping = data.get("formulas") or {}
        changed = 0
        for block in formulas:
            latex = str(mapping.get(block.id, "") or "").strip()
            if latex:
                block.meta["latex"] = latex
                changed += 1
        if progress:
            progress("formula", changed, len(formulas), "独立公式 LaTeX 缓存完成")
        return changed

    def enhance_inline_formulas(
        self,
        document: DocumentStructure,
        pdf_path: Path,
        asset_dir: Path,
        progress: Callable[[str, int, int, str], None] | None = None,
    ) -> int:
        if not getattr(self.config.ocr, "inline_latex", True):
            return 0
        limit = int(getattr(self.config.ocr, "inline_formula_limit", 0) or 0)
        candidates: list[tuple[Block, list[dict]]] = []
        for block in document.blocks:
            if block.kind in {"formula", "table", "figure", "reference"}:
                continue
            spans = block.meta.get("spans")
            if not isinstance(spans, list):
                continue
            runs = [run for run in _math_runs(spans) if _run_size_ok(run)]
            if runs:
                candidates.append((block, runs))
        if not candidates:
            return 0
        items: list[dict] = []
        lookup: list[tuple[Block, dict, str]] = []
        done = 0
        for block, runs in candidates:
            for run_index, run in enumerate(runs):
                if limit > 0 and done >= limit:
                    break
                item_id = f"{block.id}__{run_index}"
                items.append({"id": item_id, "page": block.page, "bbox": run["bbox"]})
                lookup.append((block, run, item_id))
                done += 1
            if limit > 0 and done >= limit:
                break
        if not items:
            return 0
        if progress:
            progress("formula", 0, len(items), f"OXR 正在识别 {len(items)} 个行内公式…")
        data = self._run({"pdf": str(pdf_path), "formulas": items})
        mapping = data.get("formulas") or {}
        changed = 0
        by_block: dict[str, list[dict[str, str]]] = {}
        for block, run, item_id in lookup:
            latex = str(mapping.get(item_id, "") or "").strip()
            if not latex:
                continue
            placeholder = f"⟦MATH_{item_id}⟧"
            raw = str(run.get("text") or "")
            if raw and raw in block.text:
                block.text = block.text.replace(raw, placeholder, 1)
                by_block.setdefault(block.id, []).append(
                    {"placeholder": placeholder, "latex": latex}
                )
                changed += 1
        for block in document.blocks:
            values = by_block.get(block.id)
            if values:
                block.meta["inline_math"] = values
        if progress:
            progress("formula", len(items), len(items), "行内公式识别完成")
        return changed


def _run_size_ok(run: dict) -> bool:
    box = run.get("bbox") or [0, 0, 0, 0]
    return len(box) == 4 and box[3] - box[1] <= 42 and box[2] - box[0] <= 360


def _math_runs(spans: list[dict]) -> list[dict]:
    import re

    runs: list[dict] = []
    current: dict | None = None
    for span in spans:
        text = str(span.get("text") or "")
        if not text.strip():
            continue
        if span.get("math"):
            box = list(span.get("bbox") or [])
            if len(box) != 4:
                continue
            if current is not None and (
                current["line"] != span.get("line") or box[0] - current["bbox"][2] > 2
            ):
                runs.append(current)
                current = None
            if current is None:
                current = {"text": text, "bbox": box, "line": span.get("line")}
            else:
                current["text"] += text
                current["bbox"] = list(pymupdf.Rect(current["bbox"]) | pymupdf.Rect(box))
        else:
            if current is not None:
                runs.append(current)
                current = None
    if current is not None:
        runs.append(current)
    filtered: list[dict] = []
    for run in runs:
        text = str(run.get("text") or "").strip()
        box = run.get("bbox") or []
        if len(box) != 4 or box[2] - box[0] < 3 or box[3] - box[1] < 3:
            continue
        if len(text) < 2:
            continue
        if not re.search(
            r"[=+\-*/^_<>∑∫√∞≈≠≤≥±×÷·αβγδεζηθλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ\\]",
            text,
        ):
            continue
        filtered.append(run)
    return filtered


def _overlaps(a: list[float], b: list[float]) -> bool:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / area > 0.25


def _inside_text_block(page, bbox: list[float]) -> bool:
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    height = y1 - y0
    for block in page.blocks:
        bx0, by0, bx1, by1 = block.bbox
        if block.kind in {"table", "figure"} and bx0 <= cx <= bx1 and by0 <= cy <= by1:
            return True
        if block.kind not in {"paragraph", "heading", "caption", "title"}:
            continue
        if not block.text.strip():
            continue
        if bx0 <= cx <= bx1 and by0 <= cy <= by1 and height <= 32:
            return True
    return False


def _order_blocks(page):
    from ..pdf.extractor import _layout_blocks
    return _layout_blocks(page.blocks, page.width)
