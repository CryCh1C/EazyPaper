"""Provider-neutral OCR interface.

The application layer only depends on this module.  A concrete adapter (OXR,
PaddleOCR, a remote service, ...) implements :class:`OcrEngine`; replacing the
backend never requires touching the PDF or UI layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class OcrPage:
    page: int
    text: str
    engine: str = ""
    elapsed: float = 0.0
    blocks: list[dict] = field(default_factory=list)


@dataclass
class OcrDocument:
    pages: dict[int, OcrPage]
    engine: str
    elapsed: float = 0.0


class OcrEngine(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def load(self) -> None: ...

    def parse_page_image(self, image, page: int = 0) -> OcrPage: ...

    def recognize_formula(self, image) -> str: ...

    def close(self) -> None: ...


class OcrError(RuntimeError):
    pass


def clean_model_text(text: str) -> str:
    """Remove protocol artefacts from a model response."""

    value = str(text or "").strip()
    value = value.replace("<|endoftext|>", "").replace("<|im_end|>", "")
    value = value.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 2:
            value = "\n".join(lines[1:-1]).strip()
    return value
