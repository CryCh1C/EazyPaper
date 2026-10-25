"""Localhost TCP client for the PaddleOCR-VL sidecar.

A QProcess starts the worker; JSON-lines flow over QTcpSocket rather than
stdio.  This keeps the GUI non-blocking and works around Windows deployments
where QProcess pipe handles are restricted.
"""

from __future__ import annotations

import codecs
import json
import secrets
import socket
from pathlib import Path
from typing import Any

from PySide6.QtCore import QIODevice, QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtNetwork import QTcpSocket

from .config import AppConfig
from .logging_setup import get_logger
from .paths import HOME, LOG_DIR, TMP_DIR, app_home, find_ocr_python, ocr_worker_script

log = get_logger("ocr.client")


class OcrClient(QObject):
    starting = Signal(str)
    progress = Signal(str, int, int, str)
    finished = Signal(int, object, str, float)
    failed = Signal(str)
    cancelled = Signal()
    prewarm_failed = Signal(str)
    process_state = Signal(str)

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.process: QProcess | None = None
        self.socket: QTcpSocket | None = None
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending: dict[str, Any] | None = None
        self._request_id = 0
        self._port = 0
        self._auth = ""
        self._connect_attempts = 0
        self._shutting_down = False
        self._prewarm_requested = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        self._connect_timer = QTimer(self)
        self._connect_timer.setInterval(250)
        self._connect_timer.timeout.connect(self._try_connect)

    # ------------------------------------------------------------------ state
    @property
    def busy(self) -> bool:
        return self._pending is not None

    def is_connected(self) -> bool:
        return self.socket is not None and self.socket.state() == QTcpSocket.SocketState.ConnectedState

    def is_running(self) -> bool:
        return self.process is not None and self.process.state() == QProcess.ProcessState.Running

    def start(self) -> None:
        if self.is_running():
            if not self.is_connected():
                self._try_connect()
            return
        if self.socket is not None:
            try:
                self.socket.abort()
                self.socket.deleteLater()
            except Exception:
                pass
            self.socket = None
        if self.process is not None:
            try:
                self.process.deleteLater()
            except Exception:
                pass
            self.process = None
        python = find_ocr_python(self.config.ocr.python_path)
        if python is None:
            self._emit_failure(
                "未找到可用的本地 OCR Python 运行时。请先运行 scripts/setup_ocr.ps1，"
                "或在设置中指定包含 torch/transformers 的 python.exe。"
            )
            return
        script = ocr_worker_script()
        model_dir = Path(self.config.ocr.model_dir).expanduser()
        if not model_dir.is_absolute():
            model_dir = HOME / model_dir
        model_dir = model_dir.resolve()
        if not script.is_file():
            self._emit_failure(f"OCR 工作脚本不存在：{script}")
            return
        if not (model_dir / "config.json").is_file():
            self._emit_failure(
                f"本地 PaddleOCR-VL 模型不存在：{model_dir}\n"
                "请确认 paddle_ocr/model.safetensors 与 config.json 已下载。"
            )
            return

        self._port = _free_port()
        self._auth = secrets.token_hex(12)
        self._shutting_down = False
        self._connect_attempts = 0
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / "ocr-worker.log"

        process = QProcess(self)
        process.setProgram(str(python))
        process.setArguments(
            [
                "-u",
                str(script),
                "--serve-tcp",
                "--host",
                "127.0.0.1",
                "--port",
                str(self._port),
                "--auth",
                self._auth,
                "--model-dir",
                str(model_dir),
                "--device",
                self.config.ocr.device,
                "--max-pixels",
                str(self.config.ocr.max_pixels),
                "--max-new-tokens",
                str(self.config.ocr.max_new_tokens),
                "--batch-size",
                str(self.config.ocr.batch_size),
            ]
        )
        process.setWorkingDirectory(str(HOME))
        # Redirect stdio to local files; no QProcess named pipes are needed.
        # A real empty file is more reliable than NUL under restricted Windows
        # sandboxes (QProcess may fail to open NUL for reading there).
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        empty_stdin = TMP_DIR / "ocr-stdin-empty.txt"
        if not empty_stdin.is_file():
            empty_stdin.write_bytes(b"")
        process.setStandardInputFile(str(empty_stdin))
        process.setStandardOutputFile(str(log_path), QIODevice.OpenModeFlag.Append)
        process.setStandardErrorFile(str(log_path), QIODevice.OpenModeFlag.Append)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("EASYPAPER_HOME", str(app_home()))
        env.insert("HF_HUB_OFFLINE", "1")
        env.insert("TOKENIZERS_PARALLELISM", "false")
        for key, sub in {
            "HF_HOME": ".hf-cache",
            "TORCH_HOME": ".torch-cache",
            "TMP": ".tmp",
            "TEMP": ".tmp",
        }.items():
            env.insert(key, str(HOME / sub))
        process.setProcessEnvironment(env)
        process.errorOccurred.connect(self._on_process_error)
        process.finished.connect(self._on_process_finished)
        self.process = process
        self._buffer = ""
        self._decoder.reset()

        socket_obj = QTcpSocket(self)
        socket_obj.connected.connect(self._on_connected)
        socket_obj.readyRead.connect(self._on_ready_read)
        socket_obj.disconnected.connect(self._on_disconnected)
        socket_obj.errorOccurred.connect(self._on_socket_error)
        self.socket = socket_obj

        self.starting.emit("正在启动本地 OCR 进程…")
        self.process_state.emit("starting")
        process.start()
        self._connect_timer.start()
        QTimer.singleShot(80, self._try_connect)

    def prewarm(self) -> None:
        """Start the sidecar and ask it to load weights before a PDF needs OCR."""
        if not self.config.ocr.enabled or self.config.ocr.mode == "off":
            return
        self._prewarm_requested = True
        if self.is_connected():
            self._send({"cmd": "load"})
        else:
            self.start()

    # --------------------------------------------------------------- requests
    def ocr_document(self, pdf_path: Path, pages: list[int]) -> None:
        if self._pending:
            self.failed.emit("上一次 OCR 任务尚未结束。")
            return
        if not pages:
            self.finished.emit(0, {}, "", 0.0)
            return
        self._request_id += 1
        self._pending = {
            "cmd": "ocr_pages",
            "id": str(self._request_id),
            "pdf": str(Path(pdf_path).resolve()),
            "pages": [int(p) for p in pages],
            "dpi": self.config.ocr.dpi,
            "max_pixels": self.config.ocr.max_pixels,
            "max_new_tokens": self.config.ocr.max_new_tokens,
        }
        self._timer.start(max(60, self.config.ocr.timeout_seconds) * 1000)
        self.progress.emit("starting", 0, len(pages), "准备本地 OCR…")
        if self.is_connected():
            self._send_pending()
        else:
            self.start()

    def _send_pending(self) -> None:
        if not self._pending or not self.is_connected():
            return
        self._send(self._pending)
        self.starting.emit("本地 OCR 正在加载模型并识别页面…")

    def _send(self, payload: dict[str, Any]) -> None:
        if not self.is_connected() or self.socket is None:
            return
        self.socket.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.socket.flush()

    # ----------------------------------------------------------------- socket
    def _try_connect(self) -> None:
        if self._shutting_down:
            return
        if self.is_connected():
            self._connect_timer.stop()
            return
        if self.socket is None:
            return
        if self.process is None or self.process.state() == QProcess.ProcessState.NotRunning:
            if self.process is not None:
                self._emit_failure("OCR 进程未能启动。详见 logs/ocr-worker.log。")
            self._connect_timer.stop()
            return
        self._connect_attempts += 1
        if self._connect_attempts > 80:
            self._connect_timer.stop()
            self._emit_failure("连接本地 OCR 服务超时。详见 logs/ocr-worker.log。")
            self.cancel()
            return
        if self.socket.state() == QTcpSocket.SocketState.UnconnectedState:
            self.socket.connectToHost("127.0.0.1", self._port)

    def _on_connected(self) -> None:
        self._connect_timer.stop()
        self.process_state.emit("running")
        self._send({"cmd": "hello", "auth": self._auth})
        self.starting.emit("本地 OCR 服务已连接")
        if self._prewarm_requested:
            self._send({"cmd": "load"})
        if self._pending:
            self._send_pending()

    def _on_ready_read(self) -> None:
        if self.socket is None:
            return
        chunk = bytes(self.socket.readAll())
        self._buffer += self._decoder.decode(chunk)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line.strip())

    def _on_disconnected(self) -> None:
        self.process_state.emit("disconnected")
        if self._pending and not self._shutting_down:
            self._emit_failure("OCR 服务连接中断。详见 logs/ocr-worker.log。")

    def _on_socket_error(self, error) -> None:
        if self._shutting_down:
            return
        # ConnectionRefused during startup is expected; the retry timer handles it.
        if self._pending and self.is_connected():
            self._emit_failure(f"OCR 通信错误：{self.socket.errorString() if self.socket else error}")

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log.debug("OCR non-JSON TCP data: %s", line[:500])
            return
        kind = message.get("type")
        if kind == "ready":
            self.process_state.emit("ready")
            if message.get("loaded"):
                self._prewarm_requested = False
                self.starting.emit("本地 OCR 模型已就绪")
        elif kind == "progress":
            current = int(message.get("current", 0) or 0)
            total = int(message.get("total", 0) or 0)
            text = str(message.get("message", ""))
            self._timer.start(max(60, self.config.ocr.timeout_seconds) * 1000)
            self.progress.emit(str(message.get("stage", "ocr")), current, total, text)
        elif kind == "result":
            request_id = int(message.get("id", 0) or 0)
            pages = {
                int(item.get("page", 0)): str(item.get("text", ""))
                for item in message.get("pages", [])
                if int(item.get("page", 0)) > 0
            }
            engine = str(message.get("engine", "PaddleOCR-VL"))
            elapsed = float(message.get("elapsed", 0.0) or 0.0)
            self._pending = None
            self._timer.stop()
            self.progress.emit("done", len(pages), len(pages), "本地 OCR 完成")
            self.finished.emit(request_id, pages, engine, elapsed)
        elif kind == "error":
            message_text = str(message.get("message", "OCR 失败"))
            if self._pending:
                self._pending = None
                self._timer.stop()
                self.failed.emit(message_text)
            elif self._prewarm_requested:
                self._prewarm_requested = False
                self.prewarm_failed.emit(message_text)
        elif kind == "bye":
            self.process_state.emit("stopped")

    # --------------------------------------------------------------- shutdown
    def cancel(self) -> None:
        if not self._pending and not self.is_running():
            return
        log.info("cancelling OCR request")
        self._pending = None
        self._timer.stop()
        self._connect_timer.stop()
        if self.socket is not None:
            try:
                self.socket.abort()
            except Exception:
                pass
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(2000)
        self.cancelled.emit()

    def shutdown(self) -> None:
        self._shutting_down = True
        self._timer.stop()
        self._connect_timer.stop()
        self._pending = None
        if self.is_connected() and self.socket is not None:
            try:
                self.socket.write(b'{"cmd":"shutdown"}\n')
                self.socket.flush()
                self.socket.waitForBytesWritten(300)
            except Exception:
                pass
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            if not self.process.waitForFinished(2000):
                self.process.kill()
                self.process.waitForFinished(1000)
        if self.socket is not None:
            try:
                self.socket.abort()
                self.socket.deleteLater()
            except Exception:
                pass
            self.socket = None
        if self.process is not None:
            self.process.deleteLater()
            self.process = None

    # ----------------------------------------------------------------- errors
    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if self._shutting_down:
            return
        if self.process is not None and self.process.state() == QProcess.ProcessState.NotRunning:
            self._emit_failure(
                f"OCR 进程启动失败：{self.process.errorString() if self.process else error}"
            )

    def _on_process_finished(self, exit_code: int, exit_status) -> None:
        log.info("OCR worker finished: code=%s status=%s", exit_code, exit_status)
        self.process_state.emit("stopped")
        if self._pending and not self._shutting_down:
            self._emit_failure(
                f"OCR 进程退出（exit={exit_code}）。详见 logs/ocr-worker.log。"
            )
        self._connect_timer.stop()

    def _on_timeout(self) -> None:
        if self._pending:
            self._emit_failure("OCR 超时。可在设置中增大超时时间，或减少一次处理的页数。")
            self.cancel()

    def _emit_failure(self, message: str) -> None:
        prewarm = self._prewarm_requested and self._pending is None
        self._prewarm_requested = False
        self._pending = None
        self._timer.stop()
        if prewarm:
            self.prewarm_failed.emit(message)
        else:
            self.failed.emit(message)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()
