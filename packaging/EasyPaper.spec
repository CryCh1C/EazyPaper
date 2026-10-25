# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for EasyPaper.

The GUI executable deliberately excludes torch / transformers / PaddlePaddle.
The local PaddleOCR-VL worker is launched from .venv (or ocr-runtime) as a
separate process, keeping the executable small and the interface responsive.
"""

import os
from pathlib import Path

project_root = Path(SPECPATH).parent
debug_build = os.environ.get("EASYPAPER_BUILD_DEBUG") == "1"
onefile_build = os.environ.get("EASYPAPER_BUILD_ONEFILE") == "1"
exe_name = "EasyPaperDebug" if debug_build else "EasyPaper"

datas = [
    (str(project_root / "app" / "resources" / "easypaper.ico"), "app/resources"),
    (str(project_root / "app" / "resources" / "easypaper.png"), "app/resources"),
    (str(project_root / "app" / "resources" / "katex"), "app/resources/katex"),
    (str(project_root / "tools" / "ocr_worker.py"), "tools"),
]

hiddenimports = [
    "pymupdf",
    "openai",
    "openai.resources.chat.completions",
    "openai.resources.models",
    "openai.types.chat",
    "openai.types.chat.chat_completion",
    "openai.types.chat.chat_completion_chunk",
    "truststore",
    "httpx2",
    "httpcore2",
    "anyio",
    "sniffio",
    "jiter",
    "pydantic",
    "pydantic_core",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel",
    "PySide6.QtNetwork",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "app.translation_package",
    "app.project_package",
]

excludes = [
    # OCR runtime is a sidecar process, never imported by the GUI.
    "torch",
    "torchvision",
    "transformers",
    "tokenizers",
    "accelerate",
    "safetensors",
    "huggingface_hub",
    "hf_xet",
    "sympy",
    "tqdm",
    "paddle",
    "paddleocr",
    "paddlex",
    "onnxruntime",
    "networkx",
    "pymupdf4llm",
    "pymupdf_layout",
    "numpy",
    "PIL",
    # Unused UI/runtime modules.
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSvg",
    "PySide6.QtDBus",
    "PySide6.QtConcurrent",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSerialPort",
    "PySide6.QtStateMachine",
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "jupyter",
    "pytest",
]

a = Analysis(
    [str(project_root / "run_easypaper.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries if onefile_build else [],
    a.datas if onefile_build else [],
    [],
    name=exe_name,
    debug=debug_build,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=debug_build,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
    icon=str(project_root / "app" / "resources" / "easypaper.ico"),
    version=str(project_root / "packaging" / "version_info.txt"),
    manifest=str(project_root / "packaging" / "app.manifest"),
)

if not onefile_build:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=exe_name,
    )

