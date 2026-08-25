from pathlib import Path
from typing import List, Dict, Iterator, Tuple
import re
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, TextLoader
from utils import embedding, config

from elasticsearch import Elasticsearch, helpers

# ---------- Config ----------
INDEX_NAME = "doc_chunks"
DOCTRINE_INDEX_NAME = "doctrine_chunks"
CHUNK_SIZE = 5000         # characters
CHUNK_OVERLAP = 0         # characters
MIN_CHUNK_SIZE = 200      # characters

import logging

es = None
def get_es():
    global es 
    if not es:
        ES_URL = f"http://{config.Config.ES_HOST()}:9200"
        es = Elasticsearch(
            ES_URL, 
            verify_certs=False, 
            api_key=(config.Config.ES_ACCESS_ID(), config.Config.ES_ACCESS_KEY()),)
    return es

def _es_available(index_name: str = INDEX_NAME) -> bool:
    """Check if Elasticsearch is reachable and authenticated."""
    try:
        # Use index-level check; the API key may lack cluster privileges
        # so ping() (HEAD /) returns 403. indices.exists only needs
        # view_index_metadata on the target index.
        get_es().indices.exists(index=index_name)
        return True  # no exception means ES is reachable
    except Exception as e:
        logging.warning(f"Elasticsearch availability check failed for index '{index_name}': {e}")
        return False

# ---------- Helpers ----------
def _clean_text(s: str) -> str:
    s = s.replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()

def _read_pages(file_path: Path) -> List[Tuple[int, str]]:
    suffix = file_path.suffix.lower()

    if suffix.lower().endswith(".pdf"):
        loader = PyPDFLoader(file_path)
    elif suffix.lower().endswith(".docx"):
        loader = Docx2txtLoader(file_path)
    elif suffix.lower().endswith(".txt"):
        loader = TextLoader(file_path)
        
    pages = []
    for i, page in enumerate(loader.load_and_split()):
        try:
            text = page.page_content.strip() or ""
        except Exception:
            text = ""
        if len(text) > 1: # not a blank page
            pages.append((i + 1, _clean_text(text)))
    return pages

def _chunk_text(text: str, size: int, overlap: int) -> List[str]:
    if not text or len(text) < MIN_CHUNK_SIZE: # text is too small
        return []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + size)
        chunk = text[start:end]
        if len(chunk) >= MIN_CHUNK_SIZE:
            chunks.append(chunk)
        if end == n:
            break
        start = max(end - overlap, start + 1)
    return chunks

def _ensure_index(dims: int, index_name: str = INDEX_NAME) -> None:
    if not _es_available(index_name=index_name):
        raise ConnectionError("Elasticsearch is not available")
    if get_es().indices.exists(index=index_name):
        return
    body = {
        "settings": {
            "index": {"number_of_shards": 1, "number_of_replicas": 0}
        },
        "mappings": {
            "properties": {
                "user_id":    {"type": "keyword"},
                "doc_id":    {"type": "keyword"},
                "chunk_id":  {"type": "integer"},
                "content":   {"type": "keyword"},
                "embedding": {"type": "dense_vector", "dims": dims, "index": True, "similarity": "cosine"},
                "page":      {"type": "integer"},
                "link":      {"type": "keyword"},
                "title":     {"type": "keyword"}
            }
        }
    }
    get_es().indices.create(index=index_name, body=body)

def _actions_for_chunks(user_id: str,
                        file_id: str,
                        title: str,
                        link: str,
                        page_chunks: List[Tuple[int, List[str]]],
                        embed_dims: int,
                        index_name: str = INDEX_NAME,
                        embed_title_in_content: bool = False) -> Iterator[Dict]:
    chunk_counter = 0
    title_prefix = f"[{title}] " if embed_title_in_content and title else ""
    for page_num, chunks in page_chunks:
        for c in chunks:
            content = title_prefix + c
            yield {
                "_index": index_name,
                "_routing": file_id,
                "_source": {
                    "user_id": user_id,
                    "doc_id": file_id,
                    "chunk_id": chunk_counter,
                    "content": content,
                    "embedding": embedding.get_embedding(content),
                    "page": page_num,
                    "link": link,
                    "title": title
                }
            }
            chunk_counter += 1

# ---------- Main APIs ----------
def insert_by_file_id(user_id: str, file_id: str, title: str, link: str, index_name: str = INDEX_NAME, embed_title_in_content: bool = False) -> Dict:
    """
    Reads ./UPLOAD_DIR/{file_id}, chunks content, embeds via get_embedding, and inserts into ES.
    Returns a summary dict with counts and basic info.

    If embed_title_in_content is True, each chunk's content is prefixed with
    "[{title}] " before embedding and storage. This ensures vector search
    matches queries that mention the title (e.g. "according to FM 2.0...")
    even when the body text doesn't repeat the title verbatim.
    """
    file_path = Path(config.Config.UPLOAD_DIR()) / f"{file_id}"
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # If we're going to prefix every chunk anyway, also delete any prior
    # chunks for this file_id so re-indexing with a new title doesn't leave
    # stale rows behind.
    if embed_title_in_content:
        try:
            if get_es().indices.exists(index=index_name):
                get_es().delete_by_query(
                    index=index_name,
                    body={"query": {"term": {"doc_id": file_id}}},
                    routing=file_id,
                    refresh=True,
                )
        except Exception as e:
            logging.warning(f"Pre-insert cleanup for {file_id} in {index_name} failed: {e}")

    # 1) Read & chunk
    pages = _read_pages(file_path)
    page_chunks: List[Tuple[int, List[str]]] = []
    for page_num, text in pages:
        chunks = _chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)
        page_chunks.append((page_num, chunks))

    # If the file is empty, nothing to do
    total_chunks = sum(len(cs) for _, cs in page_chunks)
    if total_chunks == 0:
        return {"file_id": file_id, "title": title, "inserted": 0, "note": "No extractable text"}

    # 2) Probe embedding dims from the first non-empty chunk
    first_chunk_text = next(c for _, cs in page_chunks for c in cs if c)
    probe_embedding = embedding.get_embedding(first_chunk_text)

    dims = len(probe_embedding)

    # 3) Ensure index exists with correct dims
    _ensure_index(dims, index_name=index_name)

    # 4) Bulk insert
    actions = _actions_for_chunks(
        user_id, file_id, title, link, page_chunks, dims,
        index_name=index_name,
        embed_title_in_content=embed_title_in_content,
    )
    ok, errors = helpers.bulk(get_es(), actions, refresh=True)

    return {
        "user_id": user_id,
        "file_id": file_id,
        "title": title,
        "path": link,
        "chunks_indexed": ok,
        "errors": errors if errors else None,
        "index": index_name,
        "embedding_dims": dims
    }

def delete_by_file_id(file_id: str, index_name: str = INDEX_NAME) -> Dict:
    """
    Deletes all chunks for a given file_id (doc_id).
    """
    file_path = Path(config.Config.UPLOAD_DIR()) / f"{file_id}"
    if file_path.exists():
        file_path.unlink()

    resp = get_es().delete_by_query(
        index=index_name,
        body={"query": {"term": {"doc_id": file_id}}},
        routing=file_id,
        refresh=True
    )
    return {"file_id": file_id, "deleted": resp.get("deleted", 0), "timed_out": resp.get("timed_out", False)}

def get_documents_by_title(query: str | None = None, index_name: str = INDEX_NAME):
    """
    List documents grouped by title. If `query` is provided, it's treated as a
    partial (substring) match against the title (case-sensitive on keyword).
    Returns: [{title, doc_id, link, chunks_count}, ...]
    """
    try:
        es = get_es()
        if not es.indices.exists(index=index_name):
            return []
    except Exception as e:
        logging.warning(f"Elasticsearch unavailable, skipping document listing: {e}")
        return []

    es_query = {"wildcard": {"title": {"value": f"*{query}*", "case_insensitive": True}}} if query and len(query) > 1 else None

    resp = es.search(
        index=index_name,
        size=0,
        query=es_query,
        aggs={
            "by_title": {
                "terms": {
                    "field": "title",
                    "size": 100000,
                    "order": {"_key": "asc"}
                  },
                "aggs": {
                    "sample_doc": {
                        "top_hits": {"_source": ["doc_id", "title", "link"], "size": 1}
                    }
                }
            }
        },
    )

    results = []
    for bucket in resp["aggregations"]["by_title"]["buckets"]:
        sample = bucket["sample_doc"]["hits"]["hits"][0]["_source"]
        results.append({
            "title": sample.get("title"),
            "doc_id": sample.get("doc_id"),
            "link": sample.get("link"),
            "chunks_count": bucket["doc_count"],
        })
    return results


def top_k_similar_chunks(
    query_text: str,
    k: int = 5,
    *,
    num_candidates: int = 200,
    index_name: str = INDEX_NAME,
    doc_ids: List[str] | None = None,
) -> List:
    """
    Return the top-k most similar chunks using vector kNN search.
    Each hit includes its doc_id, title, link, page, content, and similarity score.

    Args:
        query_text: text to embed and search for
        k: number of chunks to return
        num_candidates: ANN candidate pool before re-ranking
        index_name: Elasticsearch index name
        doc_ids: when provided, restrict the search to these doc_ids (used by
                 Doctrine Chat to scope answers to a selected set of publications)
    """
    try:
        es = get_es()
        if not es.indices.exists(index=index_name):
            return []
    except Exception as e:
        logging.warning(f"Elasticsearch unavailable, skipping vector search: {e}")
        return []

    # 1. Convert query to embedding vector
    qv = embedding.get_embedding(query_text)

    # 2. kNN query
    body = {
        "size": k,
        "knn": {
            "field": "embedding",
            "query_vector": qv,
            "k": k,
            "num_candidates": num_candidates
        },
        "_source": ["doc_id", "chunk_id","title", "link", "page", "content"]
    }

    # Restrict the ANN search to a selected set of publications when requested.
    if doc_ids:
        body["knn"]["filter"] = {"terms": {"doc_id": doc_ids}}

    # 3. Execute search
    resp = get_es().search(index=index_name, body=body)

    # 4. Parse results
    results = []
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        results.append({
            "doc_id": src.get("doc_id"),
            "chunk_id": src.get("chunk_id"),
            "title": src.get("title"),
            "link": src.get("link"),
            "page": src.get("page"),
            "content": src.get("content"),
            "score": hit["_score"]
        })

    return results

