from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChunkConfig(StrictConfig):
    target_characters: int = Field(ge=256, le=4096)
    overlap_characters: int = Field(ge=0, le=1024)

    @model_validator(mode="after")
    def overlap_is_smaller_than_chunk(self) -> "ChunkConfig":
        if self.overlap_characters >= self.target_characters:
            raise ValueError("chunk overlap must be smaller than target size")
        return self


class RRFConfig(StrictConfig):
    pool: int = Field(ge=5, le=256)
    constant: int = Field(ge=1, le=500)


class SemanticConfig(StrictConfig):
    lexical_threshold: float = Field(ge=0, le=2)
    low_lexical_dense_weight: float = Field(ge=0, le=1)
    high_lexical_dense_weight: float = Field(ge=0, le=1)


class RetrievalConfig(StrictConfig):
    schema_name: str = Field(alias="schema", pattern=r"^harbor-retrieval-config/v1$")
    chunk: ChunkConfig
    rrf: RRFConfig
    semantic: SemanticConfig

    @property
    def config_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


DEFAULT_RETRIEVAL_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "retrieval.json"
)


def load_retrieval_config(path: Path | None = None) -> RetrievalConfig:
    source = path or DEFAULT_RETRIEVAL_CONFIG_PATH
    return RetrievalConfig.model_validate_json(source.read_text(encoding="utf-8"))
