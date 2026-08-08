from __future__ import annotations

import argparse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from typing import Any

import numpy as np
import openvino as ov
import openvino_tokenizers  # noqa: F401 - registers tokenizer extension operations


MODEL_ID = "OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov"


@dataclass
class RuntimeStats:
    loaded_at: float
    load_seconds: float
    requests: int = 0
    texts: int = 0
    inference_ms: float = 0.0


class OpenVINOEmbeddingRuntime:
    def __init__(self, model_dir: Path, device: str = "CPU") -> None:
        if not (model_dir / "openvino_model.xml").exists():
            raise FileNotFoundError(f"missing OpenVINO embedding model: {model_dir}")
        started = time.perf_counter()
        core = ov.Core()
        self.tokenizer = core.compile_model(
            model_dir / "openvino_tokenizer.xml",
            "CPU",
        )
        self.model = core.compile_model(
            model_dir / "openvino_model.xml",
            device,
        )
        self.device = device
        self.model_dir = model_dir
        self.dimensions = self.model.output(
            "last_hidden_state"
        ).partial_shape[-1].get_length()
        self.lock = threading.Lock()
        self.stats = RuntimeStats(
            loaded_at=time.time(),
            load_seconds=round(time.perf_counter() - started, 3),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > 64:
            raise ValueError("input must contain between 1 and 64 texts")
        if any(not text.strip() or len(text) > 12_000 for text in texts):
            raise ValueError("each input must contain 1 to 12000 characters")
        with self.lock:
            started = time.perf_counter()
            tokens = self.tokenizer(texts)
            input_ids = tokens["input_ids"]
            attention_mask = tokens["attention_mask"]
            hidden = self.model(
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                }
            )["last_hidden_state"]
            left_padded = bool(
                attention_mask[:, -1].sum() == attention_mask.shape[0]
            )
            if left_padded:
                vectors = hidden[:, -1, :]
            else:
                sequence_lengths = attention_mask.sum(axis=1) - 1
                vectors = hidden[
                    np.arange(attention_mask.shape[0]),
                    sequence_lengths,
                ]
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(~np.isfinite(vectors)) or np.any(norms <= 1e-12):
                raise RuntimeError("model produced an invalid embedding")
            vectors = vectors / norms
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.stats.requests += 1
            self.stats.texts += len(texts)
            self.stats.inference_ms += elapsed_ms
            return vectors.astype(np.float32).tolist()

    def health(self) -> dict[str, Any]:
        average = (
            self.stats.inference_ms / self.stats.requests
            if self.stats.requests
            else 0.0
        )
        return {
            "status": "ok",
            "loaded": True,
            "model": MODEL_ID,
            "model_path": str(self.model_dir),
            "device": self.device,
            "dimensions": self.dimensions,
            "normalization": "l2",
            "pooling": "last-token",
            "load_seconds": self.stats.load_seconds,
            "requests": self.stats.requests,
            "texts": self.stats.texts,
            "average_inference_ms": round(average, 2),
        }


class EmbeddingHandler(BaseHTTPRequestHandler):
    runtime: OpenVINOEmbeddingRuntime
    api_token: str

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not self.api_token:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.api_token}"

    def do_GET(self) -> None:
        if self.path.rstrip("/") in {"/health", "/v1/health"}:
            self._json(HTTPStatus.OK, self.runtime.health())
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/embeddings":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bearer token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("request body size is invalid")
            payload = json.loads(self.rfile.read(length))
            if payload.get("model") not in {MODEL_ID, "qwen3-embedding-0.6b"}:
                raise ValueError("requested model is not loaded")
            raw_input = payload.get("input")
            texts = [raw_input] if isinstance(raw_input, str) else raw_input
            if not isinstance(texts, list) or not all(
                isinstance(text, str) for text in texts
            ):
                raise ValueError("input must be a string or a list of strings")
            vectors = self.runtime.embed(texts)
            self._json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "model": MODEL_ID,
                    "data": [
                        {
                            "object": "embedding",
                            "index": index,
                            "embedding": vector,
                        }
                        for index, vector in enumerate(vectors)
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "total_tokens": 0,
                        "note": "OpenVINO tokenizer does not expose token usage",
                    },
                },
            )
        except ValueError as exc:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
        except Exception as exc:
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"embedding inference failed: {type(exc).__name__}"},
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible OpenVINO Qwen3 embedding sidecar"
    )
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    runtime = OpenVINOEmbeddingRuntime(args.model_dir.resolve(), args.device)
    EmbeddingHandler.runtime = runtime
    EmbeddingHandler.api_token = args.token
    server = ThreadingHTTPServer((args.host, args.port), EmbeddingHandler)
    print(
        json.dumps(
            {
                "event": "embedding-service-ready",
                "url": f"http://{args.host}:{args.port}/v1",
                **runtime.health(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
