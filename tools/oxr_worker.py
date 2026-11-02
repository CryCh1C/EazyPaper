#!/usr/bin/env python
"""Standalone OXR inference worker.

This script is intentionally independent from the GUI package.  It is executed
by the external OCR runtime (the Python 3.12 ``.venv``), so torch/transformers
never enter the PyInstaller ``_internal`` folder.

stdin: one JSON request
stdout: one JSON response
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# The worker is launched by the external .venv.  Keep every cache beside the
# application instead of touching an inaccessible user-profile cache.
_home = Path(os.environ.get("EASYPAPER_HOME", "") or Path.cwd()).expanduser()
for _key, _sub in {
    "HF_HOME": ".hf-cache",
    "HUGGINGFACE_HUB_CACHE": ".hf-cache/hub",
    "TRANSFORMERS_CACHE": ".hf-cache/transformers",
    "TORCH_HOME": ".torch-cache",
    "MPLCONFIGDIR": ".mpl-cache",
    "TMP": ".tmp",
    "TEMP": ".tmp",
}.items():
    _value = _home / _sub
    try:
        _value.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    os.environ.setdefault(_key, str(_value))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import pymupdf
import torch
from PIL import Image
from transformers import AutoModelForCausalLM

PROMPT_PAGE = "Recognize the whole page content of the image."
PROMPT_FORMULA = "Recognize the formula of the image into LaTeX format."


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def clean(text: str) -> str:
    value = str(text or "").strip()
    value = value.replace("<|endoftext|>", "").replace("<|im_end|>", "").strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 2:
            value = "\n".join(lines[1:-1]).strip()
    return value


class Engine:
    def __init__(self, model_dir: Path, device: str, max_pixels: int):
        self.model_dir = Path(model_dir)
        self.max_pixels = max_pixels
        self.device = "cuda" if device == "cuda" and torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if self.device == "cuda" else torch.float32
        kwargs: dict[str, Any] = {
            "trust_remote_code": True,
            "local_files_only": True,
            "dtype": dtype,
            "low_cpu_mem_usage": True,
        }
        if self.device == "cuda":
            kwargs["device_map"] = "cuda"
        self.model = AutoModelForCausalLM.from_pretrained(str(self.model_dir), **kwargs)
        self.model.eval()

    def generate(self, image: Image.Image, prompt: str, max_new_tokens: int) -> str:
        min_pixels = 224 * 224 if prompt == PROMPT_FORMULA else 448 * 448
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        inputs, pixel_values, grid_thws = self.model.preprocess_inputs(
            messages,
            min_pixels=min_pixels,
            max_pixels=int(self.max_pixels),
        )
        inputs = inputs.to(self.device)
        if pixel_values is not None:
            pixel_values = pixel_values.to(self.device)
        if grid_thws is not None:
            grid_thws = grid_thws.to(self.device)
        with torch.inference_mode():
            output = self.model.generate(
                inputs=inputs,
                pixel_values=pixel_values,
                grid_thws=grid_thws,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                use_cache=True,
            )
        text = self.model.text_tokenizer.batch_decode(output, skip_special_tokens=True)[0]
        return clean(text)

    def formula(self, image: Image.Image) -> str:
        value = self.generate(image, PROMPT_FORMULA, 512)
        if value.startswith("$$") and value.endswith("$$") and len(value) > 4:
            value = value[2:-2].strip()
        if value.startswith("\\[") and value.endswith("\\]"):
            value = value[2:-2].strip()
        return value


def render_page(pdf: pymupdf.Document, page_number: int, dpi: int, max_pixels: int) -> Image.Image:
    page = pdf.load_page(page_number - 1)
    scale = dpi / 72.0
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale),
        alpha=False,
        colorspace=pymupdf.csRGB,
    )
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    pixels = image.width * image.height
    if pixels > max_pixels:
        ratio = (max_pixels / pixels) ** 0.5
        image = image.resize(
            (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    return image


def crop(pdf: pymupdf.Document, page_number: int, bbox: list[float], scale: float = 3.0) -> Image.Image | None:
    try:
        page = pdf.load_page(page_number - 1)
        rect = pymupdf.Rect(bbox) & page.rect
        if rect.is_empty or rect.width < 2 or rect.height < 2:
            return None
        factor = min(scale, 2400.0 / max(rect.width, rect.height, 1.0))
        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(factor, factor),
            clip=rect,
            alpha=False,
            colorspace=pymupdf.csRGB,
        )
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--max-pixels", type=int, default=1024 * 1024)
    parser.add_argument("--dpi", type=int, default=160)
    parser.add_argument("--max-new-tokens", type=int, default=1536)
    args = parser.parse_args()

    try:
        request = json.loads(sys.stdin.read() or "{}")
        pdf_path = Path(str(request.get("pdf", ""))).expanduser().resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(f"PDF 不存在：{pdf_path}")
        started = time.perf_counter()
        model_dir = Path(args.model_dir).expanduser()
        if not (model_dir / "config.json").is_file() or not (model_dir / "model.safetensors").is_file():
            raise FileNotFoundError(f"OXR 模型不完整：{model_dir}")
        engine = Engine(model_dir, args.device, args.max_pixels)
        pdf = pymupdf.open(str(pdf_path))
        try:
            pages: dict[str, str] = {}
            for page_number in request.get("pages", []) or []:
                image = render_page(pdf, int(page_number), args.dpi, args.max_pixels)
                pages[str(page_number)] = engine.generate(
                    image, PROMPT_PAGE, int(request.get("max_new_tokens", args.max_new_tokens))
                )
            formulas: dict[str, str] = {}
            for item in request.get("formulas", []) or []:
                image = crop(pdf, int(item["page"]), item["bbox"])
                if image is not None:
                    formulas[str(item["id"])] = engine.formula(image)
            emit(
                {
                    "ok": True,
                    "pages": pages,
                    "formulas": formulas,
                    "elapsed": round(time.perf_counter() - started, 3),
                }
            )
        finally:
            pdf.close()
        return 0
    except Exception as exc:
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
