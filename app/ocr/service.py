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
            if current is None:
                current = {"text": text, "bbox": list(span.get("bbox") or [])}
            else:
                current["text"] += text
                box = list(span.get("bbox") or [])
                if len(box) == 4 and len(current["bbox"]) == 4:
                    current["bbox"] = [
                        min(current["bbox"][0], box[0]),
                        min(current["bbox"][1], box[1]),
                        max(current["bbox"][2], box[2]),
                        max(current["bbox"][3], box[3]),
                    ]
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
