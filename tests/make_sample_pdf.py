"""Create a deterministic PDF used by the tests and UI smoke checks."""

from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont


def _text(page, rect, text, size=9, font="helv", color=(0, 0, 0)):
    page.insert_textbox(
        pymupdf.Rect(*rect),
        text,
        fontsize=size,
        fontname=font,
        color=color,
        align=pymupdf.TEXT_ALIGN_LEFT,
    )


def build(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    _text(page, (60, 52, 535, 102), "EasyPaper: A Lightweight Local Paper Reader", 19)
    _text(page, (60, 108, 535, 128), "Alice Example, Bob Sample — Example University", 9)
    _text(page, (60, 145, 535, 172), "Abstract", 12, "hebo")
    _text(
        page,
        (60, 176, 535, 246),
        "We present a compact desktop system for reading, translating and questioning academic "
        "papers. The system keeps the document structure, extracts text locally, runs OCR only "
        "for scanned pages, and sends chunked text to an OpenAI-compatible endpoint.",
        9,
    )

    # Two columns of body text.
    left_x, right_x = 54, 306
    col_w = 235
    y = 270
    _text(page, (left_x, y, left_x + col_w, y + 20), "1  Introduction", 12, "hebo")
    y += 26
    left_para = (
        "Reading a paper on a desktop should not require uploading the whole PDF to a remote "
        "service. EasyPaper therefore performs layout analysis and OCR in a separate local "
        "process, preserving the embedded text layer whenever it is available. The translation "
        "layer keeps headings, paragraphs, tables, formulas, captions and reading order."
    )
    _text(page, (left_x, y, left_x + col_w, y + 150), left_para, 9)
    y += 160
    _text(page, (left_x, y, left_x + col_w, y + 20), "2  Method", 12, "hebo")
    y += 26
    _text(
        page,
        (left_x, y, left_x + col_w, y + 150),
        "The PDF is parsed into blocks with PyMuPDF. A two-column reading-order heuristic sorts "
        "each band before sending blocks to the translator. The OCR worker is lazy: it is started "
        "only when a page has little or no text and a large image area.",
        9,
    )

    ry = 270
    _text(page, (right_x, ry, right_x + col_w, ry + 20), "3  Translation Pipeline", 12, "hebo")
    ry += 26
    _text(
        page,
        (right_x, ry, right_x + col_w, ry + 160),
        "Blocks are grouped into batches of roughly 3,600 characters. Every request returns a "
        "mapping from stable block ids to translations, so the display order cannot drift even "
        "when requests complete out of order. Formula blocks are preserved verbatim and table "
        "cells keep their Markdown grid.",
        9,
    )
    ry += 170
    _text(page, (right_x, ry, right_x + col_w, ry + 20), "4  Energy Model", 12, "hebo")
    ry += 30
    _text(page, (right_x, ry, right_x + col_w, ry + 45), "E = m c^2 + sum_i x_i", 11, "cour")
    ry += 58
    _text(
        page,
        (right_x, ry, right_x + col_w, ry + 120),
        "The equation above is a structural placeholder used to check that formula blocks are "
        "not reflowed into prose. Captions and tables are rendered as separate block types.",
        9,
    )

    page2 = doc.new_page(width=595, height=842)
    _text(page2, (54, 50, 540, 72), "5  Results and Discussion", 13, "hebo")
    _text(
        page2,
        (54, 82, 540, 150),
        "Table 1 summarizes the latency of each stage. Local parsing dominates startup, while "
        "OCR is only paid on scanned documents. The interface remains responsive because every "
        "long-running task executes outside the GUI thread.",
        9,
    )

    # A real ruled table so PyMuPDF's find_tables can detect it.
    x0, y0, x1, y1 = 80, 180, 515, 320
    rows, cols = 4, 3
    page2.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=(0.2, 0.2, 0.2), width=1)
    for r in range(1, rows):
        yy = y0 + (y1 - y0) * r / rows
        page2.draw_line((x0, yy), (x1, yy), color=(0.2, 0.2, 0.2), width=0.8)
    for c in range(1, cols):
        xx = x0 + (x1 - x0) * c / cols
        page2.draw_line((xx, y0), (xx, y1), color=(0.2, 0.2, 0.2), width=0.8)
    cells = [
        ["Stage", "Latency", "Notes"],
        ["Parse", "0.4 s", "local"],
        ["OCR", "8.2 s / page", "scanned only"],
        ["Translate", "API-bound", "chunked"],
    ]
    for r, row in enumerate(cells):
        for c, value in enumerate(row):
            cx = x0 + (x1 - x0) * c / cols + 8
            cy = y0 + (y1 - y0) * r / rows + 10
            page2.insert_text((cx, cy), value, fontsize=9, fontname="helv")
    _text(page2, (54, 334, 540, 356), "Table 1: Stage latency breakdown.", 9, "heit")

    # An embedded raster figure and caption (tests image extraction + embed).
    figure = Image.new("RGB", (790, 360), "white")
    fig_draw = ImageDraw.Draw(figure)
    try:
        fig_font = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        fig_font = ImageFont.load_default()
    fig_draw.rounded_rectangle((22, 56, 210, 300), radius=18, outline="#0F766E", width=5)
    fig_draw.rounded_rectangle((300, 56, 510, 300), radius=18, outline="#C2410C", width=5)
    fig_draw.rounded_rectangle((590, 56, 768, 300), radius=18, outline="#20262E", width=5)
    fig_draw.text((62, 150), "PDF", fill="#0F766E", font=fig_font)
    fig_draw.text((322, 150), "OCR", fill="#C2410C", font=fig_font)
    fig_draw.text((600, 150), "Q&A", fill="#20262E", font=fig_font)
    fig_draw.line((210, 178, 300, 178), fill="#777777", width=4)
    fig_draw.line((510, 178, 590, 178), fill="#777777", width=4)
    figure_path = path.parent / "_figure_page2.png"
    figure.save(figure_path)
    page2.insert_image(pymupdf.Rect(100, 390, 495, 570), filename=str(figure_path))
    figure_path.unlink(missing_ok=True)
    _text(page2, (54, 584, 540, 606), "Figure 1: Local OCR pipeline.", 9, "heit")

    _text(page2, (54, 640, 540, 662), "References", 12, "hebo")
    _text(
        page2,
        (54, 670, 540, 760),
        "[1] A. Example. Local-first document tools. In Proceedings of ExampleConf, 2026.\n"
        "[2] B. Sample. Practical PDF layout analysis. Journal of Examples, 12(3):1-10, 2025.",
        8.5,
    )

    # Simulated scanned page: image only, no text layer.
    page3 = doc.new_page(width=595, height=842)
    image = Image.new("RGB", (1190, 1684), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
        small = ImageFont.truetype("arial.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
        small = ImageFont.load_default()
    draw.text((90, 90), "6  Scanned Appendix", fill="black", font=font)
    body = (
        "This page intentionally contains only a raster image. A local OCR model must be used "
        "before translation. The OCR worker returns Markdown-like text and the main process "
        "turns it into structured blocks."
    )
    draw.multiline_text((90, 170), body, fill="black", font=small, spacing=14)
    draw.rectangle((90, 330, 1080, 480), outline="black", width=3)
    draw.text((110, 360), "Step | Action | Output", fill="black", font=small)
    draw.text((110, 410), "1 | Render page | RGB image", fill="black", font=small)
    temp = path.parent / "_scanned_page.png"
    image.save(temp)
    page3.insert_image(pymupdf.Rect(0, 0, 595, 842), filename=str(temp))
    temp.unlink(missing_ok=True)

    doc.set_metadata(
        {
            "title": "EasyPaper: A Lightweight Local Paper Reader",
            "author": "Alice Example, Bob Sample",
            "subject": "Test fixture",
        }
    )
    doc.save(path, deflate=True, garbage=3)
    doc.close()
    return path


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "fixtures" / "sample.pdf"
    print(build(out))
