# chunks 表的資料庫存取。只跟 DB 講話 不放商業邏輯

from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document


def create(
    db: Session, document_id: int, chunk_index: int, content: str, embedding: list[float]
) -> Chunk:
    chunk = Chunk(
        document_id=document_id,
        chunk_index=chunk_index,
        content=content,
        embedding=embedding,
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk


# 給一個問題的向量 找出這個 workspace 底下最相近的幾個 chunk
# chunks 表自己沒有 workspace_id 要 join 回 Document 才能用 workspace_id 篩選
# cosine_distance 是 pgvector 提供的距離運算子（對應 SQL 的 <=>） 數字越小代表越相近
def search_similar(
    db: Session, workspace_id: int, query_embedding: list[float], limit: int = 5
) -> list[Chunk]:
    return (
        db.query(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .filter(Document.workspace_id == workspace_id)
        .order_by(Chunk.embedding.cosine_distance(query_embedding))
        .limit(limit)
        .all()
    )
