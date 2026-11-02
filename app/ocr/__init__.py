"""OCR provider boundary."""

from .base import OcrDocument, OcrEngine, OcrError, OcrPage
from .service import OcrService

__all__ = ["OcrDocument", "OcrEngine", "OcrError", "OcrPage", "OcrService"]
