"""Shared utilities: chunking, cleaning, Voyage embedding, Pinecone upsert."""

import hashlib
import os
import re
import time
from typing import Iterator

import voyageai
from pinecone import Pinecone

VOYAGE_MODEL = "voyage-3"
EMBED_BATCH = 128        # voyage-3 max batch size
PINECONE_BATCH = 100     # upsert batch size
CHUNK_TOKENS = 512
OVERLAP_TOKENS = 64


def get_voyage_client() -> voyageai.Client:
    return voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])


def get_pinecone_index():
    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    return pc.Index(host=os.environ["PINECONE_C3PO_HOST"])


def clean_text(text: str) -> str:
    """Basic text cleaning: normalize whitespace, remove control chars."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, chunk_tokens: int = CHUNK_TOKENS,
               overlap_tokens: int = OVERLAP_TOKENS) -> list[str]:
    """
    Naive word-count-based chunking (approximates tokens at ~0.75 words/token).
    For production, replace with a tiktoken or voyage tokenizer.
    """
    words = text.split()
    chunk_words = int(chunk_tokens * 0.75)
    overlap_words = int(overlap_tokens * 0.75)
    step = chunk_words - overlap_words

    chunks = []
    for i in range(0, len(words), step):
        chunk = " ".join(words[i: i + chunk_words])
        if chunk:
            chunks.append(chunk)
        if i + chunk_words >= len(words):
            break
    return chunks


def chunk_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def embed_chunks(texts: list[str], vc: voyageai.Client) -> list[list[float]]:
    """Embed a list of text strings in batches, returning vectors."""
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i: i + EMBED_BATCH]
        result = vc.embed(batch, model=VOYAGE_MODEL, input_type="document")
        vectors.extend(result.embeddings)
        if i + EMBED_BATCH < len(texts):
            time.sleep(0.5)  # respect rate limits
    return vectors


def upsert_chunks(chunks: list[str], vectors: list[list[float]],
                  metadata_base: dict, index, namespace: str = "") -> int:
    """Upsert chunks with metadata to Pinecone. Returns count upserted."""
    total = len(chunks)
    records = []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        meta = {**metadata_base, "chunk_index": i, "chunk_total": total, "text": chunk[:1000]}
        records.append({"id": chunk_id(chunk), "values": vector, "metadata": meta})

    kwargs = {}
    if namespace:
        kwargs["namespace"] = namespace

    for i in range(0, len(records), PINECONE_BATCH):
        index.upsert(vectors=records[i: i + PINECONE_BATCH], **kwargs)

    return len(records)
