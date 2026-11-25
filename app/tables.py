"""Table parsing and rendering used for structure-preserving translation."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any


@dataclass
class TableData:
    rows: list[list[str]]
    has_header: bool = True

    @property
    def columns(self) -> int:
        return max((len(row) for row in self.rows), default=0)


_SEPARATOR = re.compile(r"^:?-{1,}:?$")


def split_row(line: str) -> list[str]:
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


def parse_markdown_table(text: str) -> TableData | None:
    rows: list[list[str]] = []
    header_separator_seen = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or "|" not in stripped:
            continue
        cells = split_row(stripped)
        if cells and all(not cell or _SEPARATOR.match(cell.replace(" ", "")) for cell in cells):
            if rows:
                header_separator_seen = True
            continue
        rows.append(cells)
    if not rows:
        return None
    width = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (width - len(row)))
    return TableData(rows=rows, has_header=True)


def clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "\\|")


def to_markdown(rows: list[list[str]], has_header: bool = True) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [[clean_cell(cell) for cell in row] + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(normalized[0]) + " |"]
    if has_header:
        lines.append("| " + " | ".join(["---"] * width) + " |")
        body = normalized[1:]
    else:
        body = normalized
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


class _OcrTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[tuple[str, int, int]]] = []
        self.row: list[tuple[str, int, int]] | None = None
        self.parts: list[str] | None = None
        self.rowspan = self.colspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.handle_endtag("tr")
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.handle_endtag("td")
            self.parts = []
            values = dict(attrs)
            self.rowspan = self._span(values.get("rowspan"))
            self.colspan = self._span(values.get("colspan"))
        elif tag == "br" and self.parts is not None:
            self.parts.append("\n")

    @staticmethod
    def _span(value: str | None) -> int:
        return int(value) if value and re.fullmatch(r"[1-9][0-9]?", value) else 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th", "tr", "table"} and self.parts is not None and self.row is not None:
            self.row.append(("".join(self.parts).strip(), self.rowspan, self.colspan))
            self.parts = None
        if tag in {"tr", "table"} and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data: str) -> None:
        if self.parts is not None:
            self.parts.append(data)


def parse_ocr_html_table(text: str) -> tuple[list[list[str]], dict[str, Any]] | None:
    """Convert the OCR worker's HTML into plain cell text and grid geometry."""
    parser = _OcrTableParser()
    parser.feed(text)
    parser.close()
    nrows = len(parser.rows)
    cells = []
    occupied_until: dict[int, int] = {}
    for row, values in enumerate(parser.rows):
        col = 0
        for value, rowspan, colspan in values:
            while any(occupied_until.get(c, 0) > row for c in range(col, col + colspan)):
                col += 1
            rowspan = min(rowspan, nrows - row)
            cells.append({"r": row, "c": col, "rs": rowspan, "cs": colspan,
                          "text": value, "cell_index": len(cells)})
            for c in range(col, col + colspan):
                occupied_until[c] = row + rowspan
            col += colspan
    if not cells:
        return None
    ncols = max(occupied_until) + 1
    rows = [[""] * ncols for _ in range(nrows)]
    for cell in cells:
        rows[cell["r"]][cell["c"]] = cell["text"]
    return rows, {"nrows": nrows, "ncols": ncols, "has_header": True, "cells": cells}


def grid_to_html(grid: dict[str, Any], translations: dict[int, str] | None = None) -> str:
    """Render a geometry-derived table grid with CSS Grid.

    ``grid`` format:
        {"nrows": int, "ncols": int, "has_header": bool,
         "cells": [{"r": int, "c": int, "rs": int, "cs": int, "text": str}]}

    Explicit ``grid-row`` / ``grid-column`` placement preserves rowspan and
    colspan and prevents a missing cell from shifting later columns left.
    """

    cells = [dict(cell) for cell in grid.get("cells", [])]
    if not cells:
        return ""
    nrows = int(grid.get("nrows") or max(c["r"] + c.get("rs", 1) for c in cells))
    ncols = int(grid.get("ncols") or max(c["c"] + c.get("cs", 1) for c in cells))
    has_header = bool(grid.get("has_header", True))
    translations = translations or {}

    # Approximate useful column widths from the longest text in each column.
    weights = [6.0] * max(1, ncols)
    for cell in cells:
        text = str(cell.get("text", ""))
        c0 = int(cell.get("c", 0))
        cs = max(1, int(cell.get("cs", 1)))
        share = max(4.0, min(36.0, len(text) / cs)) if text else 4.0
        for column in range(c0, min(ncols, c0 + cs)):
            weights[column] = max(weights[column], share)
    total = sum(weights) or 1.0
    widths = [max(5, int(round(weight / total * 100))) for weight in weights]
    widths[-1] += 100 - sum(widths)
    columns = " ".join(f"minmax(0, {width}fr)" for width in widths)

    by_start: dict[tuple[int, int], dict[str, Any]] = {
        (int(cell.get("r", 0)), int(cell.get("c", 0))): cell for cell in cells
    }
    covered = [[False] * ncols for _ in range(nrows)]
    pieces: list[str] = []
    for row_index in range(nrows):
        column = 0
        while column < ncols:
            if covered[row_index][column]:
                column += 1
                continue
            cell = by_start.get((row_index, column))
            if cell is None:
                pieces.append(
                    '<div class="grid-cell empty" '
                    f'style="grid-row:{row_index + 1};grid-column:{column + 1};"></div>'
                )
                column += 1
                continue
            text = str(cell.get("text", ""))
            if cell.get("cell_index") in translations:
                text = translations[cell["cell_index"]]
            rowspan = max(1, min(nrows - row_index, int(cell.get("rs", 1))))
            colspan = max(1, min(ncols - column, int(cell.get("cs", 1))))
            header = has_header and row_index == 0
            classes = "grid-cell header" if header else "grid-cell"
            style = (
                f"grid-row:{row_index + 1} / span {rowspan};"
                f"grid-column:{column + 1} / span {colspan};"
            )
            content = html.escape(text, quote=False).replace("\n", "<br>")
            pieces.append(f'<div class="{classes}" style="{style}">{content}</div>')
            for rr in range(row_index, min(nrows, row_index + rowspan)):
                for cc in range(column, min(ncols, column + colspan)):
                    covered[rr][cc] = True
            column += colspan

    table_class = "table-grid wide" if ncols >= 5 else "table-grid"
    return (
        f'<div class="{table_class}" style="grid-template-columns:{columns};">'
        + "".join(pieces)
        + "</div>"
    )
