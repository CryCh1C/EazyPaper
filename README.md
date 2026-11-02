# EasyPaper

本地优先的论文阅读 / 全文翻译 / AI 问答桌面工具。PDF、OCR 与缓存全部留在本机，只有翻译和问答会把你配置的 API 所需的文本发出去。

![EasyPaper 界面](docs/screenshot.png)

## 功能简介

- **三栏阅读**：左侧原文 PDF、中间结构化全文译文、右下角悬浮 AI 问答球，各自独立缩放（`Ctrl + 滚轮`）与横移（`Shift + 滚轮`）。
- **结构化翻译**：保留标题、章节、段落、表格、公式、图注与阅读顺序，按块调用任意 OpenAI 兼容 API；表格、公式、图片按原始区域嵌入译文，行内公式由本地 KaTeX 渲染。
- **本地 OCR**：扫描页由本地 **OXR-1.0** 处理，数字版 PDF 直接使用内嵌文字层；不需要联网即可解析。
- **项目导入 / 导出**：一键导出原文、译文、图片资产与问答记录为 ZIP，换机器后可离线导入继续阅读。
- **便携**：配置、缓存、日志、模型均位于项目目录，不写用户目录；无 Electron / Node、无数据库。

## 自行编译

仓库只提供源码，不包含任何编译产物（没有 `EasyPaper.exe`、`_internal\`）和模型权重，需要按以下步骤在本机生成。

### 1. 前置条件

- Windows 10/11 x64
- [uv](https://docs.astral.sh/uv/) 0.9+
- 一个 OpenAI 兼容 API（默认 `https://api.deepseek.com` / `deepseek-flash`）

### 2. 下载 OCR 模型

OCR 模型权重约 3.0 GB，不随仓库分发，使用 ModelScope 下载到 `oxr\`：

```powershell
pip install modelscope
modelscope download --model SoMark/OXR-1.0 --local_dir .\oxr\
```

下载完成后 `oxr\` 应包含 `config.json`、`model.safetensors` 以及 tokenizer / processor / 自定义 modeling 文件。

### 3. 准备 Python 3.12 环境

```powershell
cd <项目根目录>

# 可选：GitHub 下载受限时，让 uv 通过镜像下载 python-build-standalone
$env:UV_PYTHON_INSTALL_MIRROR = "https://gh-proxy.com/https://github.com/astral-sh/python-build-standalone/releases/download"

# 创建 .venv 并安装 GUI + OCR 依赖，最后自动执行模型自检
.\scripts\setup_ocr.ps1

# NVIDIA GPU 机器（CUDA 13.0 wheel，驱动需支持 CUDA 13.x）
.\scripts\setup_ocr.ps1 -Gpu
```

脚本使用项目内的 `.uv-cache`、`.uv-python`、`.pip-cache` 作为缓存目录，不会写入用户目录。

### 4. 构建 EasyPaper.exe

```powershell
# 完整安装依赖 + 构建
.\scripts\build.ps1

# 依赖已装好，仅重新打包
.\scripts\build.ps1 -SkipInstall
```

产物（one-folder 便携应用，合计约 3.4 GB）：

```text
<项目根目录>\
├─ EasyPaper.exe      # GUI 启动器
└─ _internal\         # PySide6 + QtWebEngine + PyMuPDF + OpenAI + torch/transformers
```

### 5. 运行

```powershell
# 直接运行源码
.\scripts\run.ps1

# 或双击构建好的 EasyPaper.exe
.\EasyPaper.exe
```

首次运行会生成 `config.json`（含缓存、日志、OCR 等设置）。点击窗口左下角齿轮“⚙ 设置”（或 `Ctrl + ,`）填写 Base URL、API Key 和模型名即可开始使用；未配置 API 时，PDF 解析、OCR 与原文阅读仍可正常使用。

运行 exe 需要同目录下存在：

- `.venv\Scripts\python.exe`（本地 OCR 运行时，缺失时自动降级为“仅 PDF 文字层”）
- `oxr\`（OCR 模型）
- 可写目录（`cache\`、`logs\`、`exports\`）

## 许可

MIT。OXR-1.0 模型与第三方依赖遵循各自上游许可证；本项目不重新分发模型权重。
