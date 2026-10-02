# document 的規則：存檔案、透過 workspace 判斷 ownership、CRUD 流程
# 不寫 SQL 不碰 HTTP

import io
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core import chunker, openai_client, pdf_parser, s3_client
from app.models.document import Document
from app.repositories import chunk_repo, document_repo
from app.services import workspace_service


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

    # uuid4()： 產生一個隨機的、幾乎不可能重複的識別碼
    # .hex： 把它變成一串十六進位字串
    # 加上隨機前綴 避免同名檔案在 S3 裡互相覆蓋掉
    # "documents/" 這個前綴只是 S3 key 裡的字串，不是真的資料夾，純粹方便之後找東西
    key = f"documents/{uuid4().hex}_{file.filename}"
    # file.file.read() 把上傳內容讀出來變成一包 bytes (file 是 UploadFile 這個物件 .file 是它裡面裝真正內容的屬性)
    s3_client.upload_file(key, file.file.read())

    document = document_repo.create(db, workspace_id, file.filename, key)
    return process_document(db, document)


# 抓 PDF 文字 → 切段 → 每段轉向量 → 存進 chunks 表 → 更新 document 狀態
def process_document(db: Session, document: Document) -> Document:
    try:
        # document.storage_path 現在存的是 S3 key，不是本機路徑，要先把內容抓下來
        content = s3_client.download_file(document.storage_path)
        # pdf_parser.extract_text 要的是一個「檔案物件」，io.BytesIO 把一包 bytes 包裝成檔案物件，
        # 跟上傳那邊 file.file（UploadFile 的底層）是同一種東西
        text = pdf_parser.extract_text(io.BytesIO(content))
        pieces = chunker.chunk_text(text)
        for index, piece in enumerate(pieces):
            embedding = openai_client.embed_text(piece)
            chunk_repo.create(db, document.id, index, piece, embedding)
    except Exception:
        return document_repo.mark_failed(db, document)

    return document_repo.mark_processed(db, document)


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
    s3_client.delete_file(document.storage_path)  # 順便刪 S3 上的檔案
    document_repo.delete(db, document)
