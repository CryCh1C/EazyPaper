from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# QtWebEngine cannot spawn Chromium under the restricted CI sandbox; tests use
# the QTextBrowser fallback.  Production uses QWebEngineView.
os.environ.setdefault("EASYPAPER_DISABLE_WEBENGINE", "1")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))
# pytest's --basetemp below points inside .tmp; create its parent before pytest
# asks Path.mkdir() to create only the final component.
(_PROJECT_ROOT / ".tmp").mkdir(parents=True, exist_ok=True)

# Python 3.14 on Windows maps mkdir(mode=0o700) to a restrictive ACL.  Under
# some restricted CI accounts this makes pytest's own temp directory
# unreadable.  Tests do not need private temp directories, so ignore the mode
# only inside the test process.
if os.name == "nt":
    import pathlib

    _original_mkdir = pathlib.Path.mkdir

    def _portable_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        return _original_mkdir(self, mode=0o777, parents=parents, exist_ok=exist_ok)

    pathlib.Path.mkdir = _portable_mkdir  # type: ignore[method-assign]

from app.config import AppConfig  # noqa: E402
from app.paths import CACHE_DIR  # noqa: E402
from tests.make_sample_pdf import build  # noqa: E402


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("fixtures") / "sample.pdf"
    return build(path)


@pytest.fixture()
def config(tmp_path, monkeypatch) -> AppConfig:
    import app.pdf.cache as cache_module

    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")
    cfg = AppConfig()
    cfg.ocr.enabled = False
    cfg.ocr.mode = "off"
    cfg.llm.api_key = ""
    return cfg


def _translation_for(block: dict) -> str:
    kind = block.get("type", "paragraph")
    text = block.get("text", "")
    if kind == "title":
        return "EasyPaper：轻量级本地论文阅读工具"
    if kind == "heading":
        return "【章节】" + text
    if kind == "table":
        return "| 阶段 | 延迟 | 说明 |\n| --- | --- | --- |\n| 解析 | 0.4 秒 | 本地 |"
    if kind == "caption":
        return "图注：" + text
    return "【译文】" + text


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {"id": "mock-model", "object": "model", "created": 0, "owned_by": "test"},
                        {"id": "other-model", "object": "model", "created": 0, "owned_by": "test"},
                    ],
                }
            )
        else:
            self._send_json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.endswith("/chat/completions"):
            self._send_json({"error": {"message": "not found"}}, 404)
            return
        messages = request.get("messages", [])
        content = "\n".join(str(m.get("content", "")) for m in messages)
        if request.get("stream"):
            self._stream()
            return
        if "候选短文本块" in content:
            marker = content.find("候选块：")
            start = content.find("[", marker)
            candidates = []
            if start >= 0:
                try:
                    candidates, _ = json.JSONDecoder().raw_decode(content[start:])
                except Exception:
                    candidates = []
            remove = [
                {"id": item["id"], "reason": "test header/footer"}
                for item in candidates
                if "header" in str(item.get("text", "")).lower()
                or str(item.get("text", "")).strip().isdigit()
            ]
            self._send_json(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "mock-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"remove": remove}, ensure_ascii=False),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            return
        blocks = []
        marker = content.find("待翻译块")
        if marker >= 0:
            start = content.find("[", marker)
            if start >= 0:
                try:
                    blocks, _ = json.JSONDecoder().raw_decode(content[start:])
                except Exception:
                    blocks = []
        if blocks:
            answer = json.dumps(
                {"translations": [{"id": b.get("id", ""), "text": _translation_for(b)} for b in blocks]},
                ensure_ascii=False,
            )
        else:
            answer = "这是测试环境的模拟回答。"
        self._send_json(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "mock-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    def _stream(self):
        answer = "根据论文第 1 页，EasyPaper 在本地解析 PDF，并分块调用兼容接口进行翻译。"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for index in range(0, len(answer), 9):
            chunk = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "mock-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": ({"role": "assistant"} if index == 0 else {}) | {"content": answer[index : index + 9]},
                        "finish_reason": None,
                    }
                ],
            }
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        final = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        self.wfile.write(f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture()
def mock_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}/v1"
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
