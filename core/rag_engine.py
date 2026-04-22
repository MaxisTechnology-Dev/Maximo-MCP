"""
core/rag_engine.py — ChromaDB vector search over Maximo documentation.
Used by tools/ai_intelligence.py for semantic knowledge search.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Document types supported
DOC_TYPES = {"procedures", "api_docs", "failure_codes", "all"}


class RAGEngine:
    """
    Semantic search engine backed by ChromaDB + sentence-transformers.
    Documents are embedded on first load and persisted to disk.
    """

    def __init__(self):
        from core.settings import get_settings
        self.settings = get_settings()
        self._client: Any = None
        self._collection: Any = None
        self._embedder: Any = None
        self._ready = False

    async def initialize(self) -> bool:
        """
        Load or create the ChromaDB collection and embedding model.
        Returns True if initialised successfully.
        """
        try:
            import chromadb  # type: ignore
            from sentence_transformers import SentenceTransformer  # type: ignore

            persist_dir = Path(self.settings.CHROMA_PERSIST_DIR)
            persist_dir.mkdir(parents=True, exist_ok=True)

            self._client = chromadb.PersistentClient(path=str(persist_dir))
            self._collection = self._client.get_or_create_collection(
                name="maximo_knowledge",
                metadata={"hnsw:space": "cosine"},
            )
            self._embedder = SentenceTransformer(self.settings.EMBEDDING_MODEL)
            self._ready = True
            logger.info(
                "RAG engine ready. Collection has %d documents.",
                self._collection.count(),
            )
            return True
        except ImportError as exc:
            logger.warning("RAG dependencies missing (%s). Knowledge search disabled.", exc)
            return False
        except Exception as exc:
            logger.error("RAG engine initialisation failed: %s", exc)
            return False

    async def add_documents(
        self, documents: List[str], metadatas: List[Dict], ids: List[str]
    ) -> int:
        """
        Embed and store documents in the vector store.
        Returns the number of documents added.
        """
        if not self._ready:
            await self.initialize()
        if not self._ready:
            return 0

        embeddings = self._embedder.encode(documents).tolist()
        self._collection.add(
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids,
        )
        return len(documents)

    async def search(
        self,
        query: str,
        doc_type: str = "all",
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search over Maximo knowledge base.

        Args:
            query:    Natural language search query
            doc_type: Filter by document type ("procedures"|"api_docs"|"failure_codes"|"all")
            top_k:    Number of results to return

        Returns:
            List of dicts with: text, source, doc_type, relevance_score
        """
        if not self._ready:
            await self.initialize()
        if not self._ready:
            return [{"error": "RAG engine not available. Install chromadb and sentence-transformers."}]

        query_embedding = self._embedder.encode([query]).tolist()

        where_filter: Optional[Dict] = None
        if doc_type != "all" and doc_type in DOC_TYPES:
            where_filter = {"doc_type": doc_type}

        try:
            results = self._collection.query(
                query_embeddings=query_embedding,
                n_results=min(top_k, self._collection.count() or 1),
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            logger.error("ChromaDB query failed: %s", exc)
            return []

        hits = []
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for doc, meta, dist in zip(documents, metadatas, distances):
            hits.append({
                "text": doc,
                "source": meta.get("source", "unknown"),
                "doc_type": meta.get("doc_type", "unknown"),
                "relevance_score": round(1.0 - float(dist), 4),  # cosine: 1=identical
            })

        return sorted(hits, key=lambda x: x["relevance_score"], reverse=True)

    async def seed_sample_documents(self):
        """
        Seed the vector store with a minimal set of Maximo reference documents.
        Call this once on first startup to populate the knowledge base.
        """
        sample_docs = [
            {
                "id": "proc_wo_create",
                "text": "To create a work order in Maximo: navigate to Work Order Tracking, click New, "
                        "enter description, asset number, site, priority, and work type. Set status to WAPPR "
                        "for waiting approval or APPR for approved. Assign crafts and labor before scheduling.",
                "meta": {"source": "Maximo Work Order Guide v7.6", "doc_type": "procedures"},
            },
            {
                "id": "proc_pm_setup",
                "text": "Preventive Maintenance records define scheduled maintenance intervals. "
                        "Create a PM record specifying asset, frequency (days/hours/meters), "
                        "job plan, and lead time. Run Generate PM Work Orders to create WOs automatically.",
                "meta": {"source": "Maximo PM Guide v7.6", "doc_type": "procedures"},
            },
            {
                "id": "api_mxasset",
                "text": "The mxasset object structure provides REST access to asset records. "
                        "GET /maximo/oslc/os/mxasset?oslc.where=siteid=%22BEDFORD%22&lean=1 "
                        "returns assets for site BEDFORD. Supports POST to create, PATCH to update.",
                "meta": {"source": "Maximo REST API Reference", "doc_type": "api_docs"},
            },
            {
                "id": "api_mxwo",
                "text": "The mxwo object structure is the primary API for work order management. "
                        "Key fields: wonum, description, assetnum, siteid, status, priority, worktype. "
                        "Use action=wsmethod:changeStatus to change work order status.",
                "meta": {"source": "Maximo REST API Reference", "doc_type": "api_docs"},
            },
            {
                "id": "fc_electrical",
                "text": "Failure code ELEC-001: Electrical failure - motor windings. "
                        "Probable causes: overheating, insulation breakdown, voltage spike. "
                        "Corrective action: replace motor, check power supply quality, install surge protector.",
                "meta": {"source": "Failure Code Library", "doc_type": "failure_codes"},
            },
            {
                "id": "fc_mechanical",
                "text": "Failure code MECH-003: Bearing failure. "
                        "Probable causes: lack of lubrication, misalignment, contamination, overloading. "
                        "Corrective action: replace bearing, realign shaft, improve lubrication schedule.",
                "meta": {"source": "Failure Code Library", "doc_type": "failure_codes"},
            },
            {
                "id": "proc_inventory",
                "text": "To manage inventory in Maximo: use Item Master to define items, "
                        "Storeroom to define storage locations, and Inventory to track stock levels. "
                        "Set reorder point and economic order quantity for automatic replenishment alerts.",
                "meta": {"source": "Maximo Inventory Guide v7.6", "doc_type": "procedures"},
            },
        ]

        docs = [d["text"] for d in sample_docs]
        metas = [d["meta"] for d in sample_docs]
        ids = [d["id"] for d in sample_docs]

        added = await self.add_documents(docs, metas, ids)
        logger.info("Seeded %d sample documents into RAG engine.", added)
        return added


# Singleton
_rag_instance: Optional[RAGEngine] = None


def get_rag_engine() -> RAGEngine:
    global _rag_instance
    if _rag_instance is None:
        _rag_instance = RAGEngine()
    return _rag_instance
