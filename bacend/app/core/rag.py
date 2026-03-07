import asyncio
import hashlib

import chromadb
import structlog
from app.config import settings
from langchain.schema import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader
from openai import AsyncOpenAI

logger = structlog.get_logger()
openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
chroma_client = chromadb.HttpClient(
    host=settings.CHROMA_HOST, port=settings.CHROMA_PORT
)
collection = chroma_client.get_or_create_collection(
    name=settings.CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"}
)


def _get_collection(org_id: str):
    """Each organisation has an isolatd ChromaDb collection."""
    collection_name = f"org_{org_id.replace('-', '_')}"
    return chromadb.get_or_create_collection(
        name=collection_name, metadata={"hnsw:space": "cosine"}
    )


async def get_embedding(text: str) -> list[float]:
    response = await openai_client.embeddings.create(
        model=settings.OPENAI_EMBEDDING_MODEL, input=text
    )
    return response.data[0].embedding


async def ingest_document(file_path: str, doc_name: str, metadata: dict = {}) -> int:
    if file_path.endswith(".pdf"):
        loader = PyPDFLoader(file_path)
        documents = loader.load()
    else:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        from langchain.schema import Document

        documents = [Document(page_content=text, metadata={"source": file_path})]

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)

    chunks = splitter.split_documents(documents)

    for i, chunk in enumerate(chunks):
        text = chunk.page_content
        embedding = await get_embedding(text)
        chunk_id = hashlib.md5(f"{doc_name}_{i}_{text[:50]}".encode()).hexdigest()

        collection.upsert(
            ids=[chunk_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{"doc_name": doc_name, "chunk_index": i, **metadata}],
        )
    return len(chunks)


async def search_knowledge_base(query: str, top_k: int = 5) -> list[dict]:
    query_embedding = await get_embedding(query)

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []

    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        relevance = 1 - dist
        if relevance > 0.3:
            chunks.append(
                {
                    "content": doc,
                    "source": meta.get("doc_name", "Unknown"),
                    "relevance": round(relevance, 3),
                }
            )

    return chunks
