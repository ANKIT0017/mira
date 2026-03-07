"""
RAG Knowledge Base connector for MIRA
Provides local-first semantic search using ChromaDB + sentence-transformers
"""
import os
import hashlib
from pathlib import Path
from ..tools import register_tool

# --- Configuration ---
SANDBOX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "SANDBOX")
KB_DIR = os.path.join(SANDBOX_DIR, "knowledge_db")
COLLECTION_NAME = "mira_knowledge"

# --- Chunking ---
def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 100) -> list:
    """Split text into overlapping chunks by paragraph boundaries."""
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 > chunk_size and current:
            chunks.append(current.strip())
            # Keep overlap from end of current chunk
            if overlap > 0 and len(current) > overlap:
                current = current[-overlap:] + "\n\n" + para
            else:
                current = para
        else:
            current = (current + "\n\n" + para).strip()
    
    if current.strip():
        chunks.append(current.strip())
    
    # If no paragraph breaks, chunk by character windows
    if len(chunks) <= 1 and len(text) > chunk_size:
        chunks = []
        for i in range(0, len(text), chunk_size - overlap):
            chunk = text[i:i + chunk_size]
            if chunk.strip():
                chunks.append(chunk.strip())
    
    return chunks if chunks else [text.strip()]

# --- DB Access ---
def _get_collection():
    """Get or create the ChromaDB collection."""
    try:
        import chromadb
    except ImportError:
        raise ImportError(
            "chromadb not installed. Run: pip install chromadb sentence-transformers"
        )
    
    os.makedirs(KB_DIR, exist_ok=True)
    client = chromadb.PersistentClient(path=KB_DIR)
    
    # Try sentence-transformers first, fall back to default embeddings.
    # Some environments have runtime ABI/dependency issues (e.g., numpy/TF),
    # so this must catch broad exceptions, not only ImportError.
    embed_fn = None
    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        embed_fn = SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
    except Exception as e:
        print(f"[KNOWLEDGE] sentence-transformers unavailable ({e}); using ChromaDB default embeddings")

    if embed_fn is not None:
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=embed_fn,
            metadata={"hnsw:space": "cosine"}
        )
    else:
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )
    
    return collection

# --- Tools ---

@register_tool(
    "knowledge.ingest",
    category="Knowledge Base",
    description="Ingest a file or text into the local knowledge base for semantic search later",
    example='{"tool": "knowledge.ingest", "args": {"source": "SANDBOX/myfile.txt", "title": "My Document"}}'
)
def ingest_knowledge(source: str, title: str = None, content: str = None) -> dict:
    """
    Ingest content into the knowledge base.
    
    Args:
        source: File path (within SANDBOX) to ingest, or "text" if providing content directly
        title: Optional title/label for this document
        content: Direct text content (used if source is "text")
    
    Returns:
        Dict with ingestion status and chunk count
    """
    try:
        # Determine content source
        if content and source == "text":
            text = content
            doc_id = hashlib.md5(content[:200].encode()).hexdigest()[:12]
            doc_title = title or "Direct Text"
        else:
            # Read from file (resolve within SANDBOX)
            file_path = Path(SANDBOX_DIR) / source.replace("SANDBOX/", "").replace("SANDBOX\\", "")
            if not file_path.exists():
                # Try as absolute/relative path
                file_path = Path(source)
                if not file_path.exists():
                    return {"status": "error", "error": f"File not found: {source}"}
            
            # Security: ensure within SANDBOX
            try:
                file_path.resolve().relative_to(Path(SANDBOX_DIR).resolve())
            except ValueError:
                return {"status": "error", "error": "File must be within SANDBOX directory"}
            
            text = file_path.read_text(encoding="utf-8", errors="replace")
            doc_id = hashlib.md5(str(file_path).encode()).hexdigest()[:12]
            doc_title = title or file_path.name
        
        if not text.strip():
            return {"status": "error", "error": "No content to ingest"}
        
        # Chunk the text
        chunks = _chunk_text(text)
        print(f"[KNOWLEDGE] Ingesting '{doc_title}': {len(chunks)} chunks from {len(text)} chars")
        
        # Get collection and add chunks
        collection = _get_collection()
        
        # Remove existing chunks for this document (re-ingest)
        try:
            existing = collection.get(where={"doc_id": doc_id})
            if existing and existing["ids"]:
                collection.delete(ids=existing["ids"])
                print(f"[KNOWLEDGE] Removed {len(existing['ids'])} existing chunks for '{doc_title}'")
        except Exception:
            pass
        
        # Add chunks with metadata
        ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "doc_id": doc_id,
                "title": doc_title,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "source": str(source)
            }
            for i in range(len(chunks))
        ]
        
        collection.add(
            documents=chunks,
            metadatas=metadatas,
            ids=ids
        )
        
        return {
            "status": "success",
            "message": f"Ingested '{doc_title}' into knowledge base",
            "chunks": len(chunks),
            "doc_id": doc_id,
            "total_docs": len(set(m["doc_id"] for m in collection.get()["metadatas"])) if collection.count() > 0 else 1
        }
        
    except ImportError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"Ingestion failed: {str(e)}"}


@register_tool(
    "knowledge.search",
    category="Knowledge Base",
    description="Search the local knowledge base using semantic similarity",
    example='{"tool": "knowledge.search", "args": {"query": "machine learning basics", "top_k": 5}}'
)
def search_knowledge(query: str, top_k: int = 5) -> dict:
    """
    Semantic search over the knowledge base.
    
    Args:
        query: Search query (natural language)
        top_k: Number of results to return (default: 5)
    
    Returns:
        Dict with matching chunks and their metadata
    """
    try:
        collection = _get_collection()
        
        if collection.count() == 0:
            return {
                "status": "ok",
                "results": [],
                "message": "Knowledge base is empty. Use knowledge.ingest to add documents first."
            }
        
        # Ensure top_k doesn't exceed collection size
        top_k = min(top_k, collection.count())
        
        results = collection.query(
            query_texts=[query],
            n_results=top_k
        )
        
        matches = []
        if results and results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results["distances"] else None
                matches.append({
                    "content": doc,
                    "title": meta.get("title", "Unknown"),
                    "source": meta.get("source", "Unknown"),
                    "chunk_index": meta.get("chunk_index", 0),
                    "similarity": round(1 - distance, 3) if distance is not None else None
                })
        
        print(f"[KNOWLEDGE] Search '{query}': {len(matches)} results")
        return {
            "status": "ok",
            "query": query,
            "results": matches,
            "count": len(matches)
        }
        
    except ImportError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"Search failed: {str(e)}"}


@register_tool(
    "knowledge.list",
    category="Knowledge Base",
    description="List all documents indexed in the knowledge base",
    example='{"tool": "knowledge.list", "args": {}}'
)
def list_knowledge() -> dict:
    """
    List all documents in the knowledge base.
    
    Returns:
        Dict with document list and stats
    """
    try:
        collection = _get_collection()
        
        if collection.count() == 0:
            return {
                "status": "ok",
                "documents": [],
                "total_chunks": 0,
                "message": "Knowledge base is empty."
            }
        
        all_data = collection.get()
        
        # Group by document
        docs = {}
        for meta in all_data["metadatas"]:
            doc_id = meta.get("doc_id", "unknown")
            if doc_id not in docs:
                docs[doc_id] = {
                    "title": meta.get("title", "Unknown"),
                    "source": meta.get("source", "Unknown"),
                    "chunks": 0
                }
            docs[doc_id]["chunks"] += 1
        
        return {
            "status": "ok",
            "documents": list(docs.values()),
            "total_documents": len(docs),
            "total_chunks": collection.count()
        }
        
    except ImportError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"List failed: {str(e)}"}


@register_tool(
    "knowledge.delete",
    category="Knowledge Base",
    description="Delete a document from the knowledge base by title",
    example='{"tool": "knowledge.delete", "args": {"title": "My Document"}}'
)
def delete_knowledge(title: str) -> dict:
    """
    Delete a document from the knowledge base.
    
    Args:
        title: Title of the document to delete
    
    Returns:
        Dict with deletion status
    """
    try:
        collection = _get_collection()
        
        if collection.count() == 0:
            return {"status": "error", "error": "Knowledge base is empty"}
        
        # Find chunks for this document
        all_data = collection.get(where={"title": title})
        
        if not all_data or not all_data["ids"]:
            return {"status": "error", "error": f"Document '{title}' not found in knowledge base"}
        
        chunk_count = len(all_data["ids"])
        collection.delete(ids=all_data["ids"])
        
        print(f"[KNOWLEDGE] Deleted '{title}': {chunk_count} chunks removed")
        return {
            "status": "success",
            "message": f"Deleted '{title}' ({chunk_count} chunks) from knowledge base",
            "remaining_chunks": collection.count()
        }
        
    except ImportError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"Delete failed: {str(e)}"}
