# document 的規則：存檔案、透過 workspace 判斷 ownership、CRUD 流程
# 不寫 SQL 不碰 HTTP

import io
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core import chunker, openai_client, pdf_parser, s3_client, sqs_client
from app.core.config import settings
from app.models.document import Document
from app.repositories import chunk_repo, document_repo
from app.services import workspace_service

# 一份文件最多被處理幾次，超過就直接標成失敗，不再重試
MAX_ATTEMPTS = 3
# 工單送出後超過這麼久還沒處理完，定期掃描就當作卡住了
# Lambda 單次最多跑 15 分鐘，抓兩倍，避免把還在正常處理的文件誤判成卡住
STUCK_AFTER = timedelta(minutes=30)


class DocumentNotFound(Exception):
    pass


class NotDocumentOwner(Exception):
    pass


class InvalidFileType(Exception):
    pass


# 只有失敗的文件可以手動重試（處理中、已完成的不行）
class DocumentNotRetryable(Exception):
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
    return dispatch_document(db, document)


# 把文件交出去處理（上傳、手動重試、定期掃描重送都走這裡）
# 雲端（有設定 SQS）：送工單，交給 Lambda 背景處理，立刻回傳（status 還是 uploaded）
# 本機（沒設定 SQS）：直接在這裡處理完才回傳
def dispatch_document(db: Session, document: Document) -> Document:
    if settings.sqs_queue_url is None:
        return process_document(db, document)

    # 先記時間再送：送出去之後 Lambda 可能馬上就處理完了，順序反過來的話時間會記在處理完之後
    document_repo.mark_queued(db, document)
    try:
        sqs_client.send_process_message(document.id)
    # BotoCoreError：連不上 AWS（網路、憑證）；ClientError：AWS 回錯誤（沒權限、佇列不存在）
    except (BotoCoreError, ClientError):
        # 工單送不出去 = 沒有人會處理這份文件，直接標失敗讓使用者知道，可以手動重試
        return document_repo.mark_failed(db, document)
    return document


# 使用者手動按重試：只有失敗的文件可以，次數歸零後重新交出去
def retry_document(db: Session, document_id: int, owner_id: int) -> Document:
    document = get_owned_document(db, document_id, owner_id)
    if document.status != "failed":
        raise DocumentNotRetryable
    document_repo.reset_for_retry(db, document)
    return dispatch_document(db, document)


# 定期掃描（每小時一次，由排程叫起來）：把卡住的文件重送，次數用完的標失敗
# 回傳重送了幾份，方便在 log 裡看
def requeue_stuck_documents(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - STUCK_AFTER
    requeued = 0
    for document in document_repo.list_stuck(db, cutoff):
        if document.attempts >= MAX_ATTEMPTS:
            document_repo.mark_failed(db, document)
        else:
            dispatch_document(db, document)
            requeued += 1
    return requeued


# 抓 PDF 文字 → 切段 → 每段轉向量 → 存進 chunks 表 → 更新 document 狀態
# 整份文件是一個 transaction：全部成功才一起生效，中途任何一步失敗就整批丟掉，不會留下半套 chunk
def process_document(db: Session, document: Document) -> Document:
    # 次數用完就不處理了。工單可能從好幾個地方來（SQS 自動重送、定期掃描），統一在這裡擋
    if document.attempts >= MAX_ATTEMPTS:
        return document_repo.mark_failed(db, document)
    document_repo.increment_attempts(db, document)

    try:
        # document.storage_path 現在存的是 S3 key，不是本機路徑，要先把內容抓下來
        content = s3_client.download_file(document.storage_path)
        # pdf_parser.extract_text 要的是一個「檔案物件」，io.BytesIO 把一包 bytes 包裝成檔案物件，
        # 跟上傳那邊 file.file（UploadFile 的底層）是同一種東西
        text = pdf_parser.extract_text(io.BytesIO(content))
        pieces = chunker.chunk_text(text)
        # 先清掉這份文件舊的 chunk：同一份文件處理兩次（重試）結果跟處理一次一樣，不會變兩倍
        chunk_repo.delete_for_document(db, document.id)
        for index, piece in enumerate(pieces):
            embedding = openai_client.embed_text(piece)
            chunk_repo.create(db, document.id, index, piece, embedding)
    except Exception:
        # rollback：把這次還沒 commit 的東西（刪掉的舊 chunk、新加的 chunk）全部撤回
        db.rollback()
        return document_repo.mark_failed(db, document)

    # mark_processed 裡的 commit 會讓「所有新 chunk + 狀態改成 processed」在同一刻一起生效
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
