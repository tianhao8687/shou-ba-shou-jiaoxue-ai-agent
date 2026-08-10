from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

import httpx

from .schemas import SourceHit


EMBEDDING_DIMENSIONS = 256
CHUNK_TARGET_CHARACTERS = 720
CHUNK_OVERLAP_CHARACTERS = 120


@dataclass(frozen=True)
class KnowledgeDocument:
    doc_id: str
    title: str
    section: str
    content: str
    uri: str


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    content: str
    uri: str


def _tokens(text: str) -> list[str]:
    parts = re.findall(r"[a-z0-9][a-z0-9_.-]*|[\u4e00-\u9fff]+", text.lower())
    result: list[str] = []
    for part in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            result.extend(part[index : index + 2] for index in range(max(1, len(part) - 1)))
            result.extend(list(part))
        else:
            result.append(part)
    return result


def hashing_embedding(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


class EmbeddingProvider(Protocol):
    name: str
    quality: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FeatureHashingProvider:
    """Deterministic lexical-feature baseline. It is deliberately not called semantic."""

    name = "feature-hashing-256"
    quality = "lexical-feature-baseline"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return hashing_embedding(text)


class OpenAIEmbeddingProvider:
    name = "openai-compatible-embedding"
    quality = "semantic"
    DEFAULT_QUERY_INSTRUCTION = (
        "Instruct: Given an AIOps incident query, retrieve the most relevant "
        "operational runbook passage.\nQuery: "
    )

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 30.0,
        *,
        query_instruction: str = DEFAULT_QUERY_INSTRUCTION,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.query_instruction = query_instruction
        self.transport = transport
        self._cache: dict[str, list[float]] = {}
        self._dimensions: int | None = None
        self._lock = RLock()

    @property
    def dimensions(self) -> int | None:
        return self._dimensions

    def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with httpx.Client(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = client.post(
                f"{self.endpoint}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise RuntimeError("embedding endpoint returned an invalid batch")
        ordered = sorted(rows, key=lambda item: int(item.get("index", -1)))
        if [int(item.get("index", -1)) for item in ordered] != list(range(len(texts))):
            raise RuntimeError("embedding endpoint returned invalid or duplicate indices")
        normalized: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise RuntimeError("embedding endpoint returned an empty vector")
            values = [float(value) for value in vector]
            if not all(math.isfinite(value) for value in values):
                raise RuntimeError("embedding endpoint returned a non-finite vector")
            if self._dimensions is None:
                self._dimensions = len(values)
            elif len(values) != self._dimensions:
                raise RuntimeError(
                    f"embedding dimension drift: expected {self._dimensions}, got {len(values)}"
                )
            norm = math.sqrt(sum(value * value for value in values))
            if norm <= 1e-12:
                raise RuntimeError("embedding endpoint returned a zero vector")
            normalized.append([value / norm for value in values])
        return normalized

    def _embed_cached(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            missing = list(dict.fromkeys(text for text in texts if text not in self._cache))
            if missing:
                for text, vector in zip(
                    missing,
                    self._embed_uncached(missing),
                    strict=True,
                ):
                    self._cache[text] = vector
            return [list(self._cache[text]) for text in texts]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed_cached(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed_cached([f"{self.query_instruction}{text}"])[0]


def _split_long_text(text: str) -> list[str]:
    if len(text) <= CHUNK_TARGET_CHARACTERS:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_TARGET_CHARACTERS)
        if end < len(text):
            break_at = max(text.rfind("。", start, end), text.rfind("\n", start, end))
            if break_at > start + CHUNK_TARGET_CHARACTERS // 2:
                end = break_at + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP_CHARACTERS)
    return [item for item in chunks if item]


def chunk_document(document: KnowledgeDocument) -> list[KnowledgeChunk]:
    sections: list[tuple[str, list[str]]] = []
    current_heading = document.section
    current_lines: list[str] = []
    for raw_line in document.content.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            if current_lines:
                sections.append((current_heading, current_lines))
            current_heading = line.removeprefix("## ").strip()
            current_lines = []
        elif line and not line.startswith("# "):
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, current_lines))
    if not sections:
        sections = [(document.section, [document.content])]

    chunks: list[KnowledgeChunk] = []
    for section, lines in sections:
        text = "\n".join(lines)
        for fragment in _split_long_text(text):
            chunk_id = f"{document.doc_id}#C{len(chunks) + 1:02d}"
            chunks.append(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    doc_id=document.doc_id,
                    title=document.title,
                    section=section,
                    content=fragment,
                    uri=f"{document.uri}#{chunk_id}",
                )
            )
    return chunks


def load_documents(directory: Path) -> list[KnowledgeDocument]:
    documents: list[KnowledgeDocument] = []
    for path in sorted(directory.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        heading = next((line.removeprefix("# ").strip() for line in content.splitlines() if line.startswith("# ")), path.stem)
        doc_id = path.stem.split("-", 2)
        stable_id = "-".join(doc_id[:2]) if len(doc_id) >= 2 else path.stem
        documents.append(KnowledgeDocument(stable_id, heading, "运行手册", content, f"knowledge://{path.name}"))
    if not documents:
        raise RuntimeError(f"知识库目录为空：{directory}")
    return documents


class HybridMemoryIndex:

    def __init__(self, chunks: list[KnowledgeChunk], embedding_provider: EmbeddingProvider | None = None) -> None:
        self.chunks = chunks
        self.embedding_provider = embedding_provider or FeatureHashingProvider()
        self.name = f"memory-bm25-{self.embedding_provider.name}-rrf"
        self.vector_quality = self.embedding_provider.quality
        self.by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self.token_counts = {chunk.chunk_id: Counter(_tokens(f"{chunk.title} {chunk.section} {chunk.content}")) for chunk in chunks}
        self.lengths = {chunk_id: sum(counts.values()) for chunk_id, counts in self.token_counts.items()}
        self.average_length = sum(self.lengths.values()) / max(1, len(self.lengths))
        document_frequency: Counter[str] = Counter()
        for counts in self.token_counts.values():
            document_frequency.update(counts.keys())
        count = max(1, len(chunks))
        self.idf = {token: math.log(1 + (count - frequency + 0.5) / (frequency + 0.5)) for token, frequency in document_frequency.items()}
        document_texts = [
            f"{chunk.title}\n{chunk.section}\n{chunk.content}"
            for chunk in chunks
        ]
        document_vectors = self.embedding_provider.embed_documents(document_texts)
        if len(document_vectors) != len(chunks):
            raise RuntimeError("embedding provider did not return one vector per knowledge chunk")
        self.embeddings = {
            chunk.chunk_id: vector
            for chunk, vector in zip(chunks, document_vectors, strict=True)
        }

    def _bm25(self, query: str) -> list[str]:
        query_tokens = _tokens(query)
        k1, b = 1.5, 0.75
        scored: list[tuple[float, str]] = []
        for chunk_id, counts in self.token_counts.items():
            length = self.lengths[chunk_id]
            score = 0.0
            for token in query_tokens:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                denominator = frequency + k1 * (1 - b + b * length / max(1.0, self.average_length))
                score += self.idf.get(token, 0.0) * frequency * (k1 + 1) / denominator
            scored.append((score, chunk_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [chunk_id for score, chunk_id in scored if score > 0]

    def _lexical_strength(self, query: str, top_chunk_id: str | None) -> float:
        """Normalize the top BM25 evidence by unique query features.

        This is a calibration signal rather than a probability. On strong exact
        operational evidence, lexical ranking should dominate. When few query
        features match, semantic ranking receives more weight.
        """

        if top_chunk_id is None:
            return 0.0
        counts = self.token_counts[top_chunk_id]
        length = self.lengths[top_chunk_id]
        query_tokens = _tokens(query)
        score = 0.0
        k1, b = 1.5, 0.75
        for token in query_tokens:
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            denominator = frequency + k1 * (
                1 - b + b * length / max(1.0, self.average_length)
            )
            score += (
                self.idf.get(token, 0.0)
                * frequency
                * (k1 + 1)
                / denominator
            )
        return score / max(1, len(set(query_tokens)))

    def _dense(self, query: str) -> list[str]:
        query_vector = self.embedding_provider.embed_query(query)
        scored = [(cosine_similarity(query_vector, vector), chunk_id) for chunk_id, vector in self.embeddings.items()]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [chunk_id for _, chunk_id in scored]

    @staticmethod
    def _rrf(
        dense: list[str],
        lexical: list[str],
        pool: int = 48,
        dense_weight: float = 0.52,
    ) -> list[tuple[float, str]]:
        scores: dict[str, float] = {}
        for weight, ranking in (
            (dense_weight, dense),
            (1.0 - dense_weight, lexical),
        ):
            for rank, chunk_id in enumerate(ranking[:pool], start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (60 + rank)
        ordered = sorted(((score, chunk_id) for chunk_id, score in scores.items()), key=lambda item: (-item[0], item[1]))
        maximum = ordered[0][0] if ordered else 1.0
        return [(score / maximum, chunk_id) for score, chunk_id in ordered]

    def search(self, query: str, limit: int) -> list[SourceHit]:
        dense = self._dense(query)
        lexical = self._bm25(query)
        dense_weight = 0.52
        if self.embedding_provider.quality == "semantic":
            lexical_strength = self._lexical_strength(
                query,
                lexical[0] if lexical else None,
            )
            # Calibrated on retrieval_eval_v3_1 calibration split. Exact AIOps
            # terms retain deterministic BM25 dominance; oblique paraphrases
            # receive enough dense weight to recover latent intent.
            dense_weight = 0.25 if lexical_strength < 0.50 else 0.10
        fused = self._rrf(dense, lexical, dense_weight=dense_weight)
        return [self._to_hit(self.by_id[chunk_id], score) for score, chunk_id in fused[:limit]]

    def best_for_doc(self, doc_id: str) -> SourceHit | None:
        chunk = next((item for item in self.chunks if item.doc_id == doc_id), None)
        return self._to_hit(chunk, 0.72) if chunk else None

    @staticmethod
    def _to_hit(chunk: KnowledgeChunk, score: float) -> SourceHit:
        return SourceHit(
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            title=chunk.title,
            section=chunk.section,
            excerpt=" ".join(chunk.content.split())[:280],
            score=round(min(1.0, max(0.0, score)), 3),
            uri=chunk.uri,
            retrieval_channel="hybrid",
        )


class PgVectorHybridIndex(HybridMemoryIndex):
    def __init__(
        self,
        database_url: str,
        chunks: list[KnowledgeChunk],
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        super().__init__(chunks, embedding_provider)
        self.database_url = database_url
        self.dimensions = len(next(iter(self.embeddings.values())))
        # Embedding dimensions are part of pgvector's column type. A
        # dimension-qualified derived table lets operators switch between the
        # 256-d lexical baseline and 1024-d Qwen model without a destructive
        # in-place ALTER or a startup dimension mismatch.
        self.table_name = f"knowledge_chunks_v3_1_d{self.dimensions}"
        self.name = f"postgres-pgvector-bm25-{self.embedding_provider.name}-rrf"

    @staticmethod
    def _literal(vector: list[float]) -> str:
        return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"

    def initialize(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url) as connection, connection.cursor() as cursor:
            # API and standalone workers can start against the same fresh
            # database at the same instant. PostgreSQL's IF NOT EXISTS does
            # not make concurrent CREATE EXTENSION calls race-free, so keep
            # extension, table, seed and index setup in one database-scoped
            # transaction lock.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "current_database() || ':harbor-agentops:retrieval-index', 0))"
            )
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    section TEXT NOT NULL,
                    content TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    embedding VECTOR({self.dimensions}) NOT NULL
                )
                """
            )
            for chunk in self.chunks:
                cursor.execute(
                    f"""
                    INSERT INTO {self.table_name}(chunk_id, doc_id, title, section, content, uri, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        doc_id=excluded.doc_id, title=excluded.title, section=excluded.section,
                        content=excluded.content, uri=excluded.uri, embedding=excluded.embedding
                    """,
                    (chunk.chunk_id, chunk.doc_id, chunk.title, chunk.section, chunk.content, chunk.uri, self._literal(self.embeddings[chunk.chunk_id])),
                )
            cursor.execute(
                f"DELETE FROM {self.table_name} WHERE NOT (chunk_id = ANY(%s))",
                ([chunk.chunk_id for chunk in self.chunks],),
            )
            cursor.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {self.table_name}_embedding_hnsw
                ON {self.table_name} USING hnsw (embedding vector_cosine_ops)
                """
            )
            cursor.execute(f"ANALYZE {self.table_name}")

    def _dense(self, query: str) -> list[str]:
        import psycopg

        embedding = self._literal(self.embedding_provider.embed_query(query))
        with psycopg.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT chunk_id FROM {self.table_name} ORDER BY embedding <=> %s::vector LIMIT 64",
                (embedding,),
            )
            return [row[0] for row in cursor.fetchall()]


class Retriever:
    def __init__(self, documents: list[KnowledgeDocument], chunks: list[KnowledgeChunk], index: HybridMemoryIndex) -> None:
        self.documents = documents
        self.chunks = chunks
        self.index = index

    @property
    def backend_name(self) -> str:
        return self.index.name

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def vector_quality(self) -> str:
        return self.index.vector_quality

    def search(
        self,
        query: str,
        limit: int = 6,
        preferred_doc_ids: list[str] | None = None,
    ) -> list[SourceHit]:
        candidates = self.index.search(query, max(limit * 3, limit))
        hits: list[SourceHit] = []
        seen_docs: set[str] = set()
        for doc_id in preferred_doc_ids or []:
            routed = self.index.best_for_doc(doc_id)
            if routed is None or routed.doc_id in seen_docs:
                continue
            routed.retrieval_channel = "routed"
            hits.append(routed)
            seen_docs.add(routed.doc_id)
        for candidate in candidates:
            if candidate.doc_id in seen_docs:
                continue
            hits.append(candidate)
            seen_docs.add(candidate.doc_id)
            if len(hits) == limit:
                break
        # Safety and verification policies are mandatory context, not optional semantic matches.
        pinned_ids = ["SEC-07", "OPS-12"]
        existing = {hit.doc_id for hit in hits}
        for doc_id in pinned_ids:
            if doc_id in existing:
                # A mandatory control document remains a pinned control-plane
                # reference even when lexical/dense ranking also found it.
                next(hit for hit in hits if hit.doc_id == doc_id).retrieval_channel = "pinned"
                continue
            pinned = self.index.best_for_doc(doc_id)
            if pinned is None:
                continue
            pinned.retrieval_channel = "pinned"
            replace = next((index for index in range(len(hits) - 1, -1, -1) if hits[index].doc_id not in pinned_ids), None)
            if len(hits) < limit:
                hits.append(pinned)
            elif replace is not None:
                hits[replace] = pinned
            existing.add(doc_id)
        return hits[:limit]


def create_retriever(
    data_dir: Path,
    vector_backend: str,
    database_url: str,
    embedding_backend: str = "lexical-feature-baseline",
    embedding_endpoint: str = "",
    embedding_model: str = "",
    embedding_api_key: str = "",
) -> Retriever:
    documents = load_documents(data_dir / "knowledge")
    chunks = [chunk for document in documents for chunk in chunk_document(document)]
    if embedding_backend == "openai-compatible":
        if not embedding_endpoint or not embedding_model:
            raise ValueError("semantic embedding requires EMBEDDING_ENDPOINT and EMBEDDING_MODEL")
        embedding_provider: EmbeddingProvider = OpenAIEmbeddingProvider(
            embedding_endpoint, embedding_model, embedding_api_key
        )
    elif embedding_backend == "lexical-feature-baseline":
        embedding_provider = FeatureHashingProvider()
    else:
        raise ValueError(
            "EMBEDDING_BACKEND must be 'openai-compatible' or 'lexical-feature-baseline'"
        )
    if vector_backend == "pgvector":
        index = PgVectorHybridIndex(database_url, chunks, embedding_provider)
        index.initialize()
        return Retriever(documents, chunks, index)
    if vector_backend != "memory":
        raise ValueError("VECTOR_BACKEND must be 'memory' or 'pgvector'")
    return Retriever(documents, chunks, HybridMemoryIndex(chunks, embedding_provider))
