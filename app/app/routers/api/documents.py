"""Document management endpoints."""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
from utils import search, events

router = APIRouter()


class DocumentIn(BaseModel):
    user_id: str = Field(..., min_length=1)
    file_id: str
    title: str
    link: str


class DocumentQueryIn(BaseModel):
    user_id: str = Field(..., min_length=1)
    query: Optional[str] = None


class SimilarChunksIn(BaseModel):
    user_id: str = Field(..., min_length=1)
    query_text: str
    k: int = 5
    num_candidates: int = 200


@router.post("/insert_document")
def insert_document(document: DocumentIn):
    result = search.insert_by_file_id(document.user_id, document.file_id, document.title, document.link)
    
    events.log_event(document.user_id, "vector_db", f"Uploaded {document.title}")
    return JSONResponse(content=result, status_code=200)


# ibl's search layer dropped per-user scoping (the shared index carries
# doc_id-scoped chunks; deployments are single-tenant stores) — the
# router keeps the wire contract (user_id still arrives) but calls
# ibl's signatures.
@router.delete("/delete_document")
def delete_document(user_id: str = Query(...), file_id: str = Query(...), title: str = Query(...)):
    result = search.delete_by_file_id(file_id)

    events.log_event(user_id, "vector_db", f"Deleted {title}")
    return JSONResponse(content=result, status_code=200)


@router.post("/query_documents")
def query_documents(document_query: DocumentQueryIn):
    result = search.get_documents_by_title(document_query.query)
    return JSONResponse(content=result, status_code=200)


@router.post("/similar_chunks")
def similar_chunks(request: SimilarChunksIn):
    """Find top-k similar document chunks using vector search."""
    result = search.top_k_similar_chunks(
        query_text=request.query_text,
        k=request.k,
        num_candidates=request.num_candidates
    )
    return JSONResponse(content=result, status_code=200)
