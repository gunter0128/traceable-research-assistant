# documents 表的資料庫存取。只跟 DB 講話 不放商業邏輯

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.document import Document


def list_for_workspace(db: Session, workspace_id: int) -> list[Document]:
    return db.query(Document).filter(Document.workspace_id == workspace_id).all()


def get_by_id(db: Session, document_id: int) -> Document | None:
    return db.get(Document, document_id)


def create(
    db: Session, workspace_id: int, filename: str, storage_path: str
) -> Document:
    document = Document(
        workspace_id=workspace_id, filename=filename, storage_path=storage_path
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def delete(db: Session, document: Document) -> None:
    db.delete(document)
    db.commit()


# 何時呼叫：document_service.process_document() 把文字都切段、轉完向量、存進 chunks 表之後
def mark_processed(db: Session, document: Document) -> Document:
    document.status = "processed"
    document.processed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(document)
    return document


# 何時呼叫：document_service.process_document() 中途出錯時（例如 PDF 解析失敗），讓 status 反映真實結果
def mark_failed(db: Session, document: Document) -> Document:
    document.status = "failed"
    db.commit()
    db.refresh(document)
    return document
