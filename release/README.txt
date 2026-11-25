EasyPaper 安装说明

运行 EasyPaper-Setup.exe。默认安装到 %LOCALAPPDATA%\Programs\EasyPaper。
安装程序离线部署 PDF 阅读器、翻译和问答界面。翻译和问答使用你在设置中配置的 AI 接口。

扫描件 OCR 按需配置。可在「设置 → OCR 与翻译 → 模型目录」中选择已有的 OXR 文件夹，例如原发布目录中的 oxr。
升级时可继续使用安装目录内已有的 ocr-runtime。首次配置 OCR 时，先安装 PowerShell 7 和 uv，再在安装目录运行：

  pwsh -File .\scripts\setup_ocr.ps1 -ModelDir "D:\Models\oxr"

需要 CUDA 时追加 -Gpu。此命令安装 Python 3.12 OCR 依赖，并检查指定目录中的模型文件。

可在 Windows「已安装的应用」中卸载 EasyPaper。卸载后配置、缓存、导出文件、OCR 运行时和模型保留在原位置。
