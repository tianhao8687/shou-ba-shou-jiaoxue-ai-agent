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


class ModelQueueFullError(RuntimeError):
    pass


class ModelQueueTimeoutError(RuntimeError):
    pass


class Runtime:
    def __init__(
        self,
        model_path: Path,
        device: str,
        *,
        max_pending_requests: int = 2,
        queue_timeout_seconds: float = 30.0,
    ) -> None:
        if max_pending_requests < 0:
            raise ValueError("max_pending_requests must be non-negative")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        self.model_path = model_path
        self.device = device
        self.pipeline: Any | None = None
        self.lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._admission = threading.BoundedSemaphore(1 + max_pending_requests)
        self.max_pending_requests = max_pending_requests
        self.queue_timeout_seconds = queue_timeout_seconds
        self.load_seconds = 0.0
        self.request_count = 0
        self.failure_count = 0
        self.rejected_count = 0
        self.queue_timeout_count = 0
        self.active_requests = 0
        self.queued_requests = 0
        self.queue_wait_count = 0
        self.total_generation_seconds = 0.0
        self.total_queue_wait_seconds = 0.0

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

    def generate(self, prompt: str, max_tokens: int) -> tuple[str, float, float]:
        if not self._admission.acquire(blocking=False):
            with self._state_lock:
                self.rejected_count += 1
            raise ModelQueueFullError("local model admission queue is full")

        waiting_started = time.perf_counter()
        queued_accounted = True
        with self._state_lock:
            self.queued_requests += 1
        acquired = False
        try:
            acquired = self.lock.acquire(timeout=self.queue_timeout_seconds)
            queue_wait_seconds = time.perf_counter() - waiting_started
            with self._state_lock:
                self.queued_requests -= 1
                queued_accounted = False
                self.total_queue_wait_seconds += queue_wait_seconds
                self.queue_wait_count += 1
                if acquired:
                    self.active_requests += 1
                else:
                    self.queue_timeout_count += 1
            if not acquired:
                raise ModelQueueTimeoutError("local model queue wait timed out")

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
                with self._state_lock:
                    self.request_count += 1
                    generation_seconds = time.perf_counter() - started
                    self.total_generation_seconds += generation_seconds
                return text, queue_wait_seconds, generation_seconds
            except Exception:
                with self._state_lock:
                    self.failure_count += 1
                raise
        finally:
            if acquired:
                with self._state_lock:
                    self.active_requests -= 1
                self.lock.release()
            elif queued_accounted:
                with self._state_lock:
                    self.queued_requests -= 1
            self._admission.release()

    def metrics_snapshot(self) -> dict[str, float | int]:
        with self._state_lock:
            return {
                "request_count": self.request_count,
                "failure_count": self.failure_count,
                "rejected_count": self.rejected_count,
                "queue_timeout_count": self.queue_timeout_count,
                "active_requests": self.active_requests,
                "queued_requests": self.queued_requests,
                "average_generation_seconds": (
                    self.total_generation_seconds / self.request_count
                    if self.request_count
                    else 0.0
                ),
                "average_queue_wait_seconds": (
                    self.total_queue_wait_seconds / self.queue_wait_count
                    if self.queue_wait_count
                    else 0.0
                ),
            }


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
        server_version = "HarborLocalModel/3.3"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Deliberately log only request metadata; prompts and model output stay local and private.
            print(f"{self.address_string()} - {fmt % args}")

        def _json(
            self,
            status: int,
            payload: dict[str, Any],
            headers: dict[str, str] | None = None,
        ) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)

        def _authorized(self) -> bool:
            return not token or self.headers.get("Authorization") == f"Bearer {token}"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(
                    200,
                    {
                        "status": "alive",
                        "model": MODEL_ID,
                        "runtime_available": runtime.available,
                        "loaded": runtime.pipeline is not None,
                        "device": runtime.device,
                        "load_seconds": runtime.load_seconds,
                        "capabilities": {
                            "chat_completions": True,
                            "native_json_schema": False,
                            "validation_contract": "prompt-plus-upstream-pydantic",
                            "admission_control": "bounded-single-inference-queue",
                        },
                        "max_pending_requests": runtime.max_pending_requests,
                        "queue_timeout_seconds": runtime.queue_timeout_seconds,
                        "detail": (
                            "gateway alive; model is loaded"
                            if runtime.pipeline is not None
                            else "gateway alive; model assets are available but not loaded"
                            if runtime.available
                            else "model directory or OpenVINO runtime is missing"
                        ),
                    },
                )
                return
            if self.path == "/ready":
                ready = runtime.available and runtime.pipeline is not None
                self._json(
                    200 if ready else 503,
                    {
                        "status": (
                            "ready" if ready else "warming" if runtime.available else "unavailable"
                        ),
                        "model": MODEL_ID,
                        "runtime_available": runtime.available,
                        "loaded": runtime.pipeline is not None,
                        "device": runtime.device,
                        "detail": (
                            "model pipeline is loaded and can accept inference"
                            if ready
                            else "start with --eager or complete one warm-up request"
                            if runtime.available
                            else "model directory or OpenVINO runtime is missing"
                        ),
                    },
                )
                return
            if self.path == "/metrics":
                metrics = runtime.metrics_snapshot()
                body = (
                    "# TYPE harbor_local_model_requests_total counter\n"
                    f"harbor_local_model_requests_total {metrics['request_count']}\n"
                    "# TYPE harbor_local_model_failures_total counter\n"
                    f"harbor_local_model_failures_total {metrics['failure_count']}\n"
                    "# TYPE harbor_local_model_rejected_total counter\n"
                    f"harbor_local_model_rejected_total {metrics['rejected_count']}\n"
                    "# TYPE harbor_local_model_queue_timeouts_total counter\n"
                    f"harbor_local_model_queue_timeouts_total {metrics['queue_timeout_count']}\n"
                    "# TYPE harbor_local_model_active_requests gauge\n"
                    f"harbor_local_model_active_requests {metrics['active_requests']}\n"
                    "# TYPE harbor_local_model_queued_requests gauge\n"
                    f"harbor_local_model_queued_requests {metrics['queued_requests']}\n"
                    "# TYPE harbor_local_model_loaded gauge\n"
                    f"harbor_local_model_loaded {1 if runtime.pipeline is not None else 0}\n"
                    "# TYPE harbor_local_model_average_generation_seconds gauge\n"
                    f"harbor_local_model_average_generation_seconds {metrics['average_generation_seconds']:.6f}\n"
                    "# TYPE harbor_local_model_average_queue_wait_seconds gauge\n"
                    f"harbor_local_model_average_queue_wait_seconds {metrics['average_queue_wait_seconds']:.6f}\n"
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
                if "response_format" in payload:
                    raise ValueError(
                        "response_format is not supported by this gateway; "
                        "use prompt-constrained JSON plus upstream schema validation"
                    )
                messages = payload.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ValueError("messages must be a non-empty array")
                # Plan IR needs more room than a chat answer. The caller still controls a
                # lower bound, while this hard cap prevents an accidental unbounded CPU run.
                max_tokens = max(16, min(int(payload.get("max_tokens", 420)), 1400))
                prompt = _chat_prompt(messages)
                started = time.perf_counter()
                text, queue_wait_seconds, generation_seconds = runtime.generate(
                    prompt, max_tokens
                )
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
                            "request_seconds": round(elapsed, 3),
                            "generation_seconds": round(generation_seconds, 3),
                            "queue_wait_seconds": round(queue_wait_seconds, 3),
                        },
                    },
                )
            except ValueError as exc:
                self._json(400, {"error": {"message": str(exc)}})
            except ModelQueueFullError as exc:
                self._json(
                    429,
                    {"error": {"message": str(exc), "code": "model_queue_full"}},
                    {"Retry-After": "2"},
                )
            except ModelQueueTimeoutError as exc:
                self._json(
                    503,
                    {"error": {"message": str(exc), "code": "model_queue_timeout"}},
                    {"Retry-After": "2"},
                )
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
    parser.add_argument(
        "--max-pending-requests",
        type=int,
        default=int(os.getenv("HARBOR_MODEL_MAX_PENDING_REQUESTS", "2")),
    )
    parser.add_argument(
        "--queue-timeout-seconds",
        type=float,
        default=float(os.getenv("HARBOR_MODEL_QUEUE_TIMEOUT_SECONDS", "30")),
    )
    parser.add_argument("--eager", action="store_true", help="load the 5.46 GB model before serving")
    args = parser.parse_args()

    runtime = Runtime(
        args.model_path.expanduser().resolve(),
        args.device,
        max_pending_requests=args.max_pending_requests,
        queue_timeout_seconds=args.queue_timeout_seconds,
    )
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
                "max_pending_requests": runtime.max_pending_requests,
                "queue_timeout_seconds": runtime.queue_timeout_seconds,
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
