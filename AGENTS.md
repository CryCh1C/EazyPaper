# Repository Guidelines

## Project Structure

EasyPaper is a Windows desktop app for reading, translating, and asking questions about PDFs. `app/main.py` launches the PySide6 interface in `app/ui/`. PDF extraction and caching live in `app/pdf/`, OCR integration in `app/ocr/`, and the OXR worker in `tools/oxr_worker.py`. Translation and Q&A use `app/llm.py` and background tasks in `app/workers.py`. Bundled icons and offline KaTeX assets live in `app/resources/`. See `packaging/` for the PyInstaller spec and `tests/` for tests and PDF fixtures.

## Build, Run, and Test

Run these commands from the repository root in PowerShell 7 with `uv` available. The project targets Python 3.12.

- `.\scripts\build.ps1` prepares `.venv` and builds `EasyPaper.exe` with its companion `_internal/` directory. Use `-SkipInstall` when build dependencies are ready.
- `.\scripts\run.ps1 .\tests\fixtures\sample.pdf` launches from source and opens the sample PDF. Omit the path to open the app alone.
- `.\scripts\test.ps1 -Install` installs development dependencies and runs the suite. Use `.\scripts\test.ps1` for subsequent runs.
- `.\scripts\setup_ocr.ps1` installs optional OCR dependencies and checks for local weights in `oxr/`. Add `-Gpu` for a compatible NVIDIA setup.

## Coding Style

Use four-space indentation, `snake_case` for Python modules and functions, and `PascalCase` for classes. Match nearby formatting and use lightweight type annotations. Keep user-facing UI text in Chinese. Use the existing worker and Qt signal patterns for long-running PDF and API tasks so the interface stays responsive.

## Testing

Use pytest and pytest-qt. Put regression tests in `tests/test_*.py` and name functions `test_<behavior>`. Reuse `sample_pdf`, `mock_api`, and `qtbot` where applicable. Run one file with `.\.venv\Scripts\python.exe -m pytest tests/test_llm.py`. The default suite uses offscreen Qt and the QTextBrowser fallback. Verify KaTeX and WebEngine rendering in a normal Windows session when changing the translation view.

## Commits and Pull Requests

Use short, imperative commit subjects such as `Fix PDF reading order`. In pull requests, summarize the behavior change, list verification steps, link relevant issues, and include screenshots for visible UI changes. Keep local API keys in the ignored `config.json` and keep generated builds, caches, and model weights out of commits.
