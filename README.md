# EasyPaper

本地优先的论文阅读 / 全文翻译 / AI 问答桌面工具。PDF、OCR 与缓存留在本机，翻译和问答只发送必要文本。

> 界面示意：本地 mock OpenAI 服务 + 示例论文；实际模型与界面完全一致。

* **三栏界面**：左侧 PDF 原文，中间结构化全文译文，右侧基于全文的 AI 问答。
* **独立缩放/横移**：在任意一栏按 `Ctrl + 滚轮` 缩放，`Shift + 滚轮` 横向滚动；`Ctrl + 0` 重置。

## 1\. 当前环境与目录

```text
F:\\\\EXP\\\\easypaper\\\\
├─ EasyPaper.exe                 # 构建产物（GUI 启动器，约 14 MB）
├─ \\\_internal\\\\                    # PyInstaller 运行时（PySide6 + QtWebEngine/Chromium + PyMuPDF + OpenAI，约 524 MB；torch/transformers 在外置 .venv）
├─ .venv\\\\                        # uv 管理的 Python 3.12 虚拟环境
├─ oxr\\\\                   # 用户已下载的 OXR-1.0 本地权重
├─ config.json                   # API / OCR / 界面配置（自动生成）
├─ cache\\\\                        # 结构化 JSON + 翻译缓存
│  └─ <pdf sha256 前 20 位>\\\\
│     ├─ structure.json
│     └─ translation-<签名>.json
├─ logs\\\\                         # easypaper.log / ocr-worker.log
├─ exports\\\\                      # 导出的 Markdown / 项目包
├─ imports\\\\                      # 导入项目时解出的 PDF 与资产
├─ app\\\\                          # 应用源码
├─ tools\\\\ocr\\\_worker.py           # 独立 OCR 子进程（127.0.0.1 TCP / stdio JSON 协议）
├─ scripts\\\\                      # build / run / setup\\\_ocr / test / doctor
├─ packaging\\\\                    # PyInstaller spec、版本信息、manifest
├─ tests\\\\                        # pytest 测试与示例 PDF 生成器
├─ requirements\\\*.txt             # CPU / GPU OCR 依赖
└─ uv.lock
```

所有运行期数据都相对于 **EasyPaper.exe 所在目录**（源码运行时是项目根目录）。可用环境变量 `EASYPAPER\\\_HOME` 覆盖，但默认不需要。

> `.venv` 的 `pyvenv.cfg` 记录的是本机绝对解释器路径；把整个目录复制到另一台机器/另一个盘符后，请重新运行 `.\\\\scripts\\\\setup\\\_ocr.ps1` 重建 `.venv`。`EasyPaper.exe` + `\\\_internal` 本体没有硬编码项目路径。

\---

## 2\. 安装与运行

### 2.1 前置条件

* Windows 10/11 x64
* [uv](https://docs.astral.sh/uv/) 0.9+
* 已下载的本地模型目录：`.\\\\oxr\\\\`，至少包含：

  * `config.json`
  * `model.safetensors`（约 3.0 GB）
  * tokenizer / processor / 自定义 modeling 文件
* 一个 OpenAI 兼容 API（OpenAI、DeepSeek、通义千问兼容模式、LM Studio、Ollama 的 `/v1` 等）

### 2.2 一键准备本地 Python 3.12 环境

```powershell
cd F:\\\\EXP\\\\easypaper

# 可选：GitHub 下载受限时，让 uv 通过镜像下载 python-build-standalone
$env:UV\\\_PYTHON\\\_INSTALL\\\_MIRROR = "https://gh-proxy.com/https://github.com/astral-sh/python-build-standalone/releases/download"

.\\\\scripts\\\\setup\\\_ocr.ps1

# NVIDIA GPU 机器（CUDA 13.0 wheel，驱动需支持 CUDA 13.x）
.\\\\scripts\\\\setup\\\_ocr.ps1 -Gpu
```

该脚本会：

1. 用 `uv` 创建 `.venv`（必须为 Python **3.14**）。
2. 安装 GUI 依赖与 OCR sidecar 依赖（torch / transformers 等）。
3. `-Gpu` 时安装 `torch 2.14.1+cu130` / `torchvision 0.29.1+cu130`。
4. 执行 `tools/ocr\\\_worker.py --check`，验证模型和推理环境。

> uv 缓存目录由脚本固定为项目内的 `.uv-cache`、`.uv-python`、`.pip-cache`，不会写入用户目录。

### 2.3 直接运行源码

```powershell
.\\\\scripts\\\\run.ps1
# 或指定 PDF
.\\\\scripts\\\\run.ps1 .\\\\tests\\\\fixtures\\\\sample.pdf
```

也可以在 `.venv` 激活后：

```powershell
.\\\\.venv\\\\Scripts\\\\python.exe -m app.main
```

### 2.4 配置 API

点击窗口**左下角齿轮“⚙ 设置”**（或 `Ctrl + ,`）：

|字段|说明|
|-|-|
|Base URL|默认 `https://api.deepseek.com`；其他兼容服务通常以 `/v1` 结尾|
|API Key|仅保存在本机 `config.json`，界面中默认掩码显示|
|模型|默认 `deepseek-flash`；也可填写 `deepseek-chat` 或本地模型|
|请求超时 / 温度|高级参数，可保持默认|

点击**测试连接**会：

1. 请求 `/models` 验证地址与鉴权；
2. 如果服务端不实现 `/models`，自动退化为一次 1-token 的 `/chat/completions` 探测；
3. 显示延迟、模型数量、当前模型是否在模型列表中；
4. 测试过程在后台线程执行，不冻结界面。

保存后即可打开 PDF。未配置 API 时，PDF 解析、OCR、原文阅读仍可正常使用。

