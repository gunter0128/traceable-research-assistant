# documents 表的資料庫存取。只跟 DB 講話 不放商業邏輯

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
