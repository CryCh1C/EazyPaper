#!/usr/bin/env python
"""EasyPaper OCR sidecar.

The GUI deliberately keeps torch/transformers out of the packaged exe.  This
standalone worker loads the local PaddleOCR-VL-1.6 checkpoint with the
transformers implementation and speaks a tiny JSON-lines protocol over stdio.

Example:
    python tools/ocr_worker.py --serve --model-dir ./paddle_ocr

Protocol (one JSON object per line):
    {"cmd":"ping"}                          -> {"type":"ready", ...}
    {"cmd":"ocr_pages","id":"...", ...}     -> progress / result / error
    {"cmd":"shutdown"}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

APP_VERSION = "1.0.0"
ENGINE = "PaddleOCR-VL-1.6 (local, transformers)"


def emit(payload: dict[str, Any]) -> None:
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def log(message: str) -> None:
    try:
        sys.stderr.write(message.rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def now() -> float:
    return time.perf_counter()


def configure_local_environment() -> None:
    home = Path(__file__).resolve().parents[1]
    # In a frozen build the script is exported with EASYPAPER_OCR_HOME set.
    env_home = os.environ.get("EASYPAPER_HOME", "").strip()
    if env_home:
        home = Path(env_home)
    for key, sub in {
        "HF_HOME": ".hf-cache",
        "HUGGINGFACE_HUB_CACHE": ".hf-cache/hub",
        "TRANSFORMERS_CACHE": ".hf-cache/transformers",
        "TORCH_HOME": ".torch-cache",
        "TORCHINDUCTOR_CACHE_DIR": ".torch-cache/inductor",
        "TRITON_CACHE_DIR": ".torch-cache/triton",
        "TMP": ".tmp",
        "TEMP": ".tmp",
        "TMPDIR": ".tmp",
        "PYTHONPYCACHEPREFIX": ".pycache",
    }.items():
        target = home / sub
        target.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(target)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


class OcrEngine:
    def __init__(
        self,
        model_dir: Path,
        device: str,
        max_pixels: int,
        max_new_tokens: int,
        batch_size: int = 1,
    ):
        self.model_dir = Path(model_dir)
        self.device_preference = device
        self.max_pixels = max_pixels
        self.max_new_tokens = max_new_tokens
        self.batch_size = max(1, min(int(batch_size), 4))
        self.processor = None
        self.model = None
        self.torch = None
        self.device = "cpu"
        self.dtype = None
        self.load_seconds = 0.0
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        if self._loaded:
            return
        started = now()
        if not (self.model_dir / "config.json").is_file():
            raise FileNotFoundError(f"找不到 PaddleOCR-VL 模型配置：{self.model_dir / 'config.json'}")
        if not (self.model_dir / "model.safetensors").is_file():
            raise FileNotFoundError(f"找不到模型权重：{self.model_dir / 'model.safetensors'}")

        import torch
        from transformers import AutoConfig, AutoModelForImageTextToText, AutoProcessor

        self.torch = torch
        cpu_count = os.cpu_count() or 4
        try:
            torch.set_num_threads(max(1, cpu_count - 1))
            torch.set_num_interop_threads(max(1, min(4, cpu_count // 4)))
        except Exception:
            pass

        use_cuda = self.device_preference == "cuda" or (
            self.device_preference == "auto" and torch.cuda.is_available()
        )
        if use_cuda and not torch.cuda.is_available():
            log("CUDA requested but unavailable; falling back to CPU.")
            use_cuda = False
        self.device = "cuda" if use_cuda else "cpu"
        # CPU float32 is the most reliable choice for this 0.9B VLM.  GPU uses
        # fp16 to fit comfortably into 8 GB cards.
        self.dtype = torch.float16 if use_cuda else torch.float32

        log(f"Loading processor from {self.model_dir}")
        self.processor = AutoProcessor.from_pretrained(
            str(self.model_dir),
            trust_remote_code=True,
            local_files_only=True,
        )

        # Transformers 5.x has a native PaddleOCR-VL implementation.  Its
        # config parser normalizes the old `rope_scaling` keys, but auto_map can
        # accidentally select the repository's old remote config while loading
        # the native model.  Pass an explicit native config to avoid that mix.
        load_kwargs: dict[str, Any] = {
            "trust_remote_code": False,
            "local_files_only": True,
            "dtype": self.dtype,
            "low_cpu_mem_usage": True,
            "attn_implementation": "sdpa",
        }
        if use_cuda:
            # Loading directly onto the GPU avoids a full CPU copy and roughly
            # halves cold-start time for this 0.9B model.
            load_kwargs["device_map"] = "cuda"
        try:
            native_config = AutoConfig.from_pretrained(
                str(self.model_dir),
                trust_remote_code=False,
                local_files_only=True,
            )
            self.model = AutoModelForImageTextToText.from_pretrained(
                str(self.model_dir), config=native_config, **load_kwargs
            )
        except Exception as native_error:
            log(f"Native transformers loader failed ({native_error}); trying remote code path.")
            self.model = AutoModelForImageTextToText.from_pretrained(
                str(self.model_dir),
                trust_remote_code=True,
                local_files_only=True,
                dtype=self.dtype,
                low_cpu_mem_usage=True,
            )

        self.model.eval()
        if self.device == "cuda":
            try:
                self.model.to("cuda")
            except Exception:
                pass
            try:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.benchmark = True
            except Exception:
                pass
        try:
            self.model.generation_config.pad_token_id = getattr(self.model.config, "pad_token_id", 0)
            self.model.generation_config.eos_token_id = getattr(self.model.config, "eos_token_id", 2)
        except Exception:
            pass
        self._loaded = True
        self.load_seconds = now() - started
        log(f"Model loaded in {self.load_seconds:.1f}s on {self.device} ({self.dtype})")

    def _messages(self, image) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": "OCR:"},
                ],
            }
        ]

    def recognize(self, image, progress=None) -> str:
        self.load()
        assert self.processor is not None and self.model is not None and self.torch is not None
        torch = self.torch
        images_kwargs: dict[str, Any] = {}
        try:
            min_pixels = int(getattr(self.processor.image_processor, "min_pixels", 112896))
            images_kwargs["size"] = {
                "shortest_edge": min_pixels,
                "longest_edge": int(self.max_pixels),
            }
        except Exception:
            pass
        kwargs: dict[str, Any] = {
            "add_generation_prompt": True,
            "tokenize": True,
            "return_dict": True,
            "return_tensors": "pt",
        }
        if images_kwargs:
            kwargs["images_kwargs"] = images_kwargs
        inputs = self.processor.apply_chat_template([self._messages(image)], **kwargs)
        if hasattr(inputs, "to"):
            inputs = inputs.to(self.device)
        input_len = inputs["input_ids"].shape[-1]
        if progress:
            progress("infer", f"视觉编码：{inputs['input_ids'].shape[-1]} 个输入 token")
        with torch.inference_mode():
            output = self.model.generate(
                **inputs,
                max_new_tokens=int(self.max_new_tokens),
                do_sample=False,
                use_cache=True,
            )
        generated = output[:, input_len:]
        try:
            text = self.processor.batch_decode(generated, skip_special_tokens=True)[0]
        except Exception:
            text = self.processor.decode(generated[0], skip_special_tokens=True)
        return clean_ocr_text(text)

    def recognize_batch(self, images, progress=None) -> list[str]:
        """Run 2–4 page images in one GPU generation step when possible."""
        if not images:
            return []
        if len(images) == 1:
            return [self.recognize(images[0], progress=progress)]
        self.load()
        assert self.processor is not None and self.model is not None and self.torch is not None
        torch = self.torch
        images_kwargs: dict[str, Any] = {}
        try:
            min_pixels = int(getattr(self.processor.image_processor, "min_pixels", 112896))
            images_kwargs["size"] = {
                "shortest_edge": min_pixels,
                "longest_edge": int(self.max_pixels),
            }
        except Exception:
            pass
        kwargs: dict[str, Any] = {
            "add_generation_prompt": True,
            "tokenize": True,
            "return_dict": True,
            "return_tensors": "pt",
        }
        if images_kwargs:
            kwargs["images_kwargs"] = images_kwargs
        try:
            inputs = self.processor.apply_chat_template(
                [self._messages(image) for image in images], **kwargs
            )
            if hasattr(inputs, "to"):
                inputs = inputs.to(self.device)
            input_len = inputs["input_ids"].shape[-1]
            if progress:
                progress(
                    "infer",
                    f"批量视觉编码：{len(images)} 页 / {inputs['input_ids'].shape[-1]} 个输入 token",
                )
            with torch.inference_mode():
                output = self.model.generate(
                    **inputs,
                    max_new_tokens=int(self.max_new_tokens),
                    do_sample=False,
                    use_cache=True,
                )
            generated = output[:, input_len:]
            try:
                texts = self.processor.batch_decode(generated, skip_special_tokens=True)
            except Exception:
                texts = [
                    self.processor.decode(row, skip_special_tokens=True) for row in generated
                ]
            return [clean_ocr_text(text) for text in texts]
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            log("Batch OCR ran out of memory; retrying pages sequentially.")
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            return [self.recognize(image) for image in images]


def clean_ocr_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"<\|[^|>]*\|>", "", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"^```(?:markdown|md|text)?\s*\n", "", value.strip(), flags=re.IGNORECASE)
    value = re.sub(r"\n```\s*$", "", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    if value.lstrip().upper().startswith("OCR:"):
        value = value.lstrip()[4:]
    return value.strip()


def render_page(pdf_path: Path, page_number: int, dpi: int, max_pixels: int):
    import pymupdf
    from PIL import Image

    doc = pymupdf.open(str(pdf_path))
    try:
        if page_number < 1 or page_number > doc.page_count:
            raise ValueError(f"页码超出范围：{page_number}")
        page = doc.load_page(page_number - 1)
        scale = dpi / 72.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, colorspace=pymupdf.csRGB)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()

    # Bound the visual token budget by area, not by the longest side only.
    pixels = image.width * image.height
    if pixels > max_pixels:
        ratio = (max_pixels / pixels) ** 0.5
        size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    return image


def handle_ocr(request: dict[str, Any], engine: OcrEngine, send) -> None:
    request_id = str(request.get("id", ""))
    started = now()
    send(
        {
            "type": "progress",
            "id": request_id,
            "stage": "loading",
            "current": 0,
            "total": 1,
            "message": "正在加载本地 PaddleOCR-VL 模型…",
        }
    )
    engine.load()
    pages = [int(v) for v in request.get("pages", [])]
    pdf_path = Path(str(request.get("pdf", ""))).expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF 不存在：{pdf_path}")
    if not pages:
        send({"type": "result", "id": request_id, "pages": [], "engine": ENGINE, "elapsed": 0.0})
        return

    results: list[dict[str, Any]] = []
    total = len(pages)
    batch_size = max(1, min(int(getattr(engine, "batch_size", 1)), 4))
    for start in range(0, total, batch_size):
        chunk_pages = pages[start : start + batch_size]
        send(
            {
                "type": "progress",
                "id": request_id,
                "stage": "ocr",
                "current": start,
                "total": total,
                "message": (
                    f"PaddleOCR-VL 批量识别第 {chunk_pages[0]}–{chunk_pages[-1]} 页"
                    if len(chunk_pages) > 1
                    else f"PaddleOCR-VL 识别第 {chunk_pages[0]} 页"
                ),
            }
        )
        batch_started = now()
        images = [
            render_page(
                pdf_path,
                page_no,
                int(request.get("dpi", 160)),
                int(request.get("max_pixels", 1605632)),
            )
            for page_no in chunk_pages
        ]
        texts = engine.recognize_batch(images)
        batch_elapsed = now() - batch_started
        per_page = batch_elapsed / max(1, len(chunk_pages))
        for offset, (page_no, text) in enumerate(zip(chunk_pages, texts), start=1):
            index = start + offset
            send(
                {
                    "type": "progress",
                    "id": request_id,
                    "stage": "ocr",
                    "current": index,
                    "total": total,
                    "message": f"第 {page_no} 页识别完成（{per_page:.1f}s，{len(text)} 字符）",
                }
            )
            results.append(
                {
                    "page": page_no,
                    "text": text,
                    "chars": len(text),
                    "elapsed": round(per_page, 2),
                }
            )

    send(
        {
            "type": "result",
            "id": request_id,
            "pages": results,
            "engine": ENGINE,
            "load_seconds": round(engine.load_seconds, 2),
            "elapsed": round(now() - started, 2),
        }
    )


def dispatch(request: dict[str, Any], engine: OcrEngine, send) -> bool:
    """Handle one decoded request; return False to stop the server."""

    command = str(request.get("cmd", ""))
    if command == "shutdown":
        send({"type": "bye"})
        return False
    if command == "load":
        try:
            engine.load()
            send(
                {
                    "type": "ready",
                    "backend": ENGINE,
                    "model_dir": str(engine.model_dir),
                    "loaded": True,
                    "load_seconds": round(engine.load_seconds, 2),
                    "cuda": bool(engine.torch and engine.torch.cuda.is_available()),
                    "torch": engine.torch.__version__ if engine.torch else "",
                    "protocol": 1,
                }
            )
        except Exception as exc:
            log(traceback.format_exc())
            send(
                {
                    "type": "error",
                    "id": "",
                    "message": f"模型预加载失败：{type(exc).__name__}: {exc}",
                }
            )
        return True
    if command == "ping":
        import torch

        send(
            {
                "type": "ready",
                "backend": ENGINE,
                "model_dir": str(engine.model_dir),
                "loaded": engine.loaded,
                "cuda": bool(torch.cuda.is_available()),
                "torch": torch.__version__,
                "protocol": 1,
            }
        )
        return True
    if command == "ocr_pages":
        try:
            handle_ocr(request, engine, send)
        except Exception as exc:
            log(traceback.format_exc())
            send(
                {
                    "type": "error",
                    "id": str(request.get("id", "")),
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        return True
    send({"type": "error", "id": str(request.get("id", "")), "message": f"未知命令：{command}"})
    return True


def serve_stdio(args: argparse.Namespace) -> int:
    engine = OcrEngine(
        Path(args.model_dir),
        args.device,
        args.max_pixels,
        args.max_new_tokens,
        getattr(args, "batch_size", 1),
    )
    emit(
        {
            "type": "ready",
            "backend": ENGINE,
            "model_dir": str(Path(args.model_dir)),
            "loaded": engine.loaded,
            "protocol": 1,
            "app_version": APP_VERSION,
        }
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            emit({"type": "error", "id": "", "message": f"JSON 协议错误: {exc}"})
            continue
        if not dispatch(request, engine, emit):
            return 0
    return 0


def serve_tcp(args: argparse.Namespace) -> int:
    """Localhost TCP transport avoids Windows named-pipe/stdout issues.

    The GUI starts this process with stdout/stderr redirected to a log file and
    connects to a random loopback port.  The first message must be a hello with
    the per-launch auth token.
    """

    engine = OcrEngine(
        Path(args.model_dir),
        args.device,
        args.max_pixels,
        args.max_new_tokens,
        getattr(args, "batch_size", 1),
    )
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((args.host, int(args.port)))
        server.listen(1)
        log(f"OCR TCP server listening on {args.host}:{args.port}")
        conn, address = server.accept()
        log(f"GUI connected from {address}")
        conn.settimeout(None)
        reader = conn.makefile("r", encoding="utf-8", newline="\n")
        writer = conn.makefile("w", encoding="utf-8", newline="\n")

        def send(payload: dict[str, Any]) -> None:
            writer.write(json.dumps(payload, ensure_ascii=False) + "\n")
            writer.flush()

        first = reader.readline()
        if not first:
            return 0
        try:
            hello = json.loads(first)
        except json.JSONDecodeError:
            send({"type": "error", "id": "", "message": "hello 不是 JSON"})
            return 1
        if hello.get("cmd") != "hello" or (args.auth and hello.get("auth") != args.auth):
            send({"type": "error", "id": "", "message": "鉴权失败"})
            return 2
        send(
            {
                "type": "ready",
                "backend": ENGINE,
                "model_dir": str(Path(args.model_dir)),
                "loaded": engine.loaded,
                "protocol": 1,
                "app_version": APP_VERSION,
            }
        )
        for line in reader:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                send({"type": "error", "id": "", "message": f"JSON 协议错误: {exc}"})
                continue
            if not dispatch(request, engine, send):
                break
        return 0
    finally:
        try:
            server.close()
        except OSError:
            pass


def check(args: argparse.Namespace) -> int:
    result: dict[str, Any] = {
        "ok": True,
        "app_version": APP_VERSION,
        "python": sys.version,
        "model_dir": args.model_dir,
        "model_exists": (Path(args.model_dir) / "model.safetensors").is_file(),
        "torch": None,
        "transformers": None,
        "cuda": False,
        "backend": ENGINE,
    }
    try:
        import torch
        import transformers

        result["torch"] = torch.__version__
        result["transformers"] = transformers.__version__
        result["cuda"] = bool(torch.cuda.is_available())
        if result["cuda"]:
            result["gpu"] = torch.cuda.get_device_name(0)
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="EasyPaper local PaddleOCR-VL worker")
    parser.add_argument("--serve", action="store_true", help="JSON-lines stdio server")
    parser.add_argument("--serve-tcp", action="store_true", help="JSON-lines localhost TCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--auth", default="")
    parser.add_argument("--check", action="store_true", help="print runtime diagnostics")
    parser.add_argument("--model-dir", default=os.environ.get("EASYPAPER_OCR_MODEL", "paddle_ocr"))
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--max-pixels", type=int, default=800000)
    parser.add_argument("--max-new-tokens", type=int, default=1536)
    parser.add_argument("--batch-size", type=int, default=1)
    args = parser.parse_args()
    configure_local_environment()
    if args.check:
        return check(args)
    if args.serve_tcp:
        if args.port <= 0:
            parser.error("--serve-tcp requires --port")
        return serve_tcp(args)
    if args.serve:
        return serve_stdio(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
