from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from typing import Any


MODEL_ID = "OpenVINO/Qwen3-VL-8B-Instruct-int4-ov"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DISCOVERED_MODEL = REPOSITORY_ROOT / ".models" / "Qwen3-VL-8B-Instruct-int4-ov"
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class Runtime:
    def __init__(self, model_path: Path, device: str) -> None:
        self.model_path = model_path
        self.device = device
        self.pipeline: Any | None = None
        self.lock = threading.Lock()
        self.load_seconds = 0.0
        self.request_count = 0
        self.failure_count = 0
        self.total_generation_seconds = 0.0

    @property
    def available(self) -> bool:
        return self.model_path.is_dir() and importlib.util.find_spec("openvino_genai") is not None

    def load(self) -> None:
        if self.pipeline is not None:
            return
        if not self.model_path.is_dir():
            raise RuntimeError(f"local model directory does not exist: {self.model_path}")
        import openvino_genai as ov_genai

        started = time.perf_counter()
        self.pipeline = ov_genai.VLMPipeline(str(self.model_path), self.device)
        self.load_seconds = round(time.perf_counter() - started, 3)

    def generate(self, prompt: str, max_tokens: int) -> str:
        with self.lock:
            self.load()
            started = time.perf_counter()
            try:
                result = self.pipeline.generate(prompt, max_new_tokens=max_tokens)
                if isinstance(result, str):
                    text = result
                elif getattr(result, "texts", None):
                    text = str(result.texts[0])
                else:
                    text = str(result)
                self.request_count += 1
                self.total_generation_seconds += time.perf_counter() - started
                return text
            except Exception:
                self.failure_count += 1
                raise


def _chat_prompt(messages: list[dict[str, Any]]) -> str:
    parts = []
    for message in messages:
        role = str(message.get("role", "user")).strip().upper()
        content = message.get("content", "")
        if not isinstance(content, str):
            raise ValueError("this local gateway accepts text-only message content")
        parts.append(f"{role}: {content}")
    parts.append("ASSISTANT:")
    return "\n\n".join(parts)


def make_handler(runtime: Runtime, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HarborLocalModel/3.2"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Deliberately log only request metadata; prompts and model output stay local and private.
            print(f"{self.address_string()} - {fmt % args}")

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def _authorized(self) -> bool:
            return not token or self.headers.get("Authorization") == f"Bearer {token}"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(
                    200 if runtime.available else 503,
                    {
                        "status": "ready" if runtime.available else "unavailable",
                        "model": MODEL_ID,
                        "loaded": runtime.pipeline is not None,
                        "device": runtime.device,
                        "load_seconds": runtime.load_seconds,
                        "detail": (
                            "model directory and OpenVINO runtime are available"
                            if runtime.available
                            else "model directory or OpenVINO runtime is missing"
                        ),
                    },
                )
                return
            if self.path == "/metrics":
                average = (
                    runtime.total_generation_seconds / runtime.request_count
                    if runtime.request_count
                    else 0.0
                )
                body = (
                    "# TYPE harbor_local_model_requests_total counter\n"
                    f"harbor_local_model_requests_total {runtime.request_count}\n"
                    "# TYPE harbor_local_model_failures_total counter\n"
                    f"harbor_local_model_failures_total {runtime.failure_count}\n"
                    "# TYPE harbor_local_model_loaded gauge\n"
                    f"harbor_local_model_loaded {1 if runtime.pipeline is not None else 0}\n"
                    "# TYPE harbor_local_model_average_generation_seconds gauge\n"
                    f"harbor_local_model_average_generation_seconds {average:.6f}\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(404, {"error": {"message": "route not found"}})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/chat/completions":
                self._json(404, {"error": {"message": "route not found"}})
                return
            if not self._authorized():
                self._json(401, {"error": {"message": "invalid bearer token"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("request body size is invalid")
                payload = json.loads(self.rfile.read(length))
                messages = payload.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ValueError("messages must be a non-empty array")
                # Plan IR needs more room than a chat answer. The caller still controls a
                # lower bound, while this hard cap prevents an accidental unbounded CPU run.
                max_tokens = max(16, min(int(payload.get("max_tokens", 420)), 1400))
                prompt = _chat_prompt(messages)
                started = time.perf_counter()
                text = runtime.generate(prompt, max_tokens)
                elapsed = time.perf_counter() - started
                self._json(
                    200,
                    {
                        "id": f"local-{time.time_ns()}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": MODEL_ID,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": max(1, len(prompt) // 3),
                            "completion_tokens": max(1, len(text) // 3),
                            "total_tokens": max(2, (len(prompt) + len(text)) // 3),
                        },
                        "local_runtime": {
                            "device": runtime.device,
                            "loaded": True,
                            "load_seconds": runtime.load_seconds,
                            "generation_seconds": round(elapsed, 3),
                        },
                    },
                )
            except ValueError as exc:
                self._json(400, {"error": {"message": str(exc)}})
            except Exception as exc:
                self._json(
                    500,
                    {"error": {"message": f"{type(exc).__name__}: local inference failed"}},
                )

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI-compatible gateway for the local OpenVINO Qwen model")
    parser.add_argument("--host", default=os.getenv("HARBOR_MODEL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HARBOR_MODEL_PORT", "8091")))
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path(os.getenv("HARBOR_LOCAL_MODEL_PATH", str(DEFAULT_DISCOVERED_MODEL))),
    )
    parser.add_argument("--device", default=os.getenv("HARBOR_LOCAL_MODEL_DEVICE", "CPU"))
    parser.add_argument("--eager", action="store_true", help="load the 5.46 GB model before serving")
    args = parser.parse_args()

    runtime = Runtime(args.model_path.expanduser().resolve(), args.device)
    if args.eager:
        runtime.load()
    token = os.getenv("HARBOR_MODEL_GATEWAY_TOKEN", "harbor-local-model-token")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(runtime, token))
    print(
        json.dumps(
            {
                "event": "gateway.started",
                "address": f"http://{args.host}:{args.port}",
                "model": MODEL_ID,
                "model_path": str(runtime.model_path),
                "device": runtime.device,
                "loaded": runtime.pipeline is not None,
            },
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
