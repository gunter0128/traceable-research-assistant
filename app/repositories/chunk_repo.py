# chunks 表的資料庫存取。只跟 DB 講話 不放商業邏輯

from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document


# 不 commit：一份文件的 chunk 要嘛全部存進去、要嘛一段都不留
# 由 document_service.process_document() 在整份處理完之後一次 commit
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
    return chunk


# 刪掉某份文件的所有 chunk（重新處理前清空舊的用）。一樣不 commit，理由同上
def delete_for_document(db: Session, document_id: int) -> None:
    # .delete() 直接對篩出來的整批發一句 DELETE，不用先一筆一筆查出來再刪
    db.query(Chunk).filter(Chunk.document_id == document_id).delete()


# 每份文件的第一段（chunk_index = 0），回傳 {document_id: 內容}
# 問答時拿它的開頭當文件的「身分」：規章、公告、合約的標題和日期通常寫在最前面（docs/decisions/018）
def first_chunks(db: Session, document_ids: list[int]) -> dict[int, str]:
    rows = (
        db.query(Chunk.document_id, Chunk.content)
        .filter(Chunk.document_id.in_(document_ids), Chunk.chunk_index == 0)
        .all()
    )
    return {document_id: content for document_id, content in rows}


# 給一個問題的向量 找出這個 workspace 底下最相近的幾個 chunk
# chunks 表自己沒有 workspace_id 要 join 回 Document 才能用 workspace_id 篩選
# cosine_distance 是 pgvector 提供的距離運算子（對應 SQL 的 <=>） 數字越小代表越相近
# 只搜 status = processed 的文件：處理失敗、還在處理中的文件不能出現在答案裡
def search_similar(
    db: Session, workspace_id: int, query_embedding: list[float], limit: int = 5
) -> list[Chunk]:
    return (
        db.query(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .filter(Document.workspace_id == workspace_id)
        .filter(Document.status == "processed")
        .order_by(Chunk.embedding.cosine_distance(query_embedding))
        .limit(limit)
        .all()
    )
