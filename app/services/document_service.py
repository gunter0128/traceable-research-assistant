# document 的規則：存檔案、透過 workspace 判斷 ownership、CRUD 流程
# 不寫 SQL 不碰 HTTP

from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.models.document import Document
from app.repositories import document_repo
from app.services import workspace_service

STORAGE_DIR = Path("storage/documents")


class DocumentNotFound(Exception):
    pass


class NotDocumentOwner(Exception):
    pass


class InvalidFileType(Exception):
    pass


def list_documents(db: Session, workspace_id: int, owner_id: int) -> list[Document]:
    # 借用 workspace 的 ownership 檢查：先確認這個 workspace 是不是你的
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)
    return document_repo.list_for_workspace(db, workspace_id)


def upload_document(
    db: Session, workspace_id: int, owner_id: int, file: UploadFile
) -> Document:
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)
    # 只能上傳 PDF
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise InvalidFileType

    # 連上層缺的資料夾也一起建（storage/ 沒有的話 先建 storage/ 再建裡面的 documents/）
    # 資料夾如果已經存在不要報錯
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    # uuid4()： 產生一個隨機的、幾乎不可能重複的識別碼
    # .hex： 把它變成一串十六進位字串
    # 加上隨機前綴 避免同名檔案被覆蓋掉
    stored_name = f"{uuid4().hex}_{file.filename}"
    storage_path = STORAGE_DIR / stored_name
    # file.file.read() 把上傳內容讀出來變成一包 bytes (file 是 UploadFile 這個物件 .file 是它裡面裝真正內容的屬性)
    # storage_path.write_bytes(...) 把這包 bytes 寫進硬碟的這個路徑，等於開檔、寫入、關檔一次做完
    storage_path.write_bytes(file.file.read())

    return document_repo.create(db, workspace_id, file.filename, str(storage_path))


def get_owned_document(db: Session, document_id: int, owner_id: int) -> Document:
    document = document_repo.get_by_id(db, document_id)
    if document is None:
        raise DocumentNotFound
    # Document 沒有自己的 owner_id，透過它的 workspace 判斷
    if document.workspace.owner_id != owner_id:
        raise NotDocumentOwner
    return document


def delete_document(db: Session, document_id: int, owner_id: int) -> None:
    document = get_owned_document(db, document_id, owner_id)
    Path(document.storage_path).unlink(missing_ok=True)  # 順便刪硬碟上的檔案
    document_repo.delete(db, document)
