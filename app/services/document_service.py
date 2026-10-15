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
_GAVE_UP = f"處理了 {MAX_ATTEMPTS} 次都沒有成功，已停止自動重試"
# 工單送出後超過這麼久還沒處理完，定期掃描就當作卡住了
# Lambda 單次最多跑 15 分鐘，抓兩倍，避免把還在正常處理的文件誤判成卡住
STUCK_AFTER = timedelta(minutes=30)
# 上傳檔案大小上限：一般論文 1~5 MB、圖多的報告 10~20 MB
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 頁數上限：檔案大小不等於內容量，純文字的 PDF 很小卻可能有上千頁，處理時間會超過 Lambda 的 15 分鐘
MAX_PAGES = 300
# 一次送幾段 chunk 去轉向量（OpenAI 上限 2048，100 段已經能把呼叫次數降到百分之一，單次請求也不會太大）
EMBED_BATCH_SIZE = 100


class DocumentNotFound(Exception):
    pass


class NotDocumentOwner(Exception):
    pass


class InvalidFileType(Exception):
    pass


class FileTooLarge(Exception):
    pass


# PDF 讀得開，但一個字都抓不到（通常是掃描檔，整頁都是圖片）
class NoExtractableText(Exception):
    pass


# OpenAI 暫時連不上、而且還有剩餘次數：這次先不處理，文件維持「處理中」
# 丟給呼叫的人（worker）決定怎麼延後，worker 會叫 SQS 2 分鐘後再把工單送回來
class RetryLater(Exception):
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
    # file.size：收檔時框架已經算好的檔案大小（bytes），在讀進記憶體、存進 S3 之前就先擋掉
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise FileTooLarge

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
        return document_repo.mark_failed(db, document, "排入處理佇列失敗，可以重試")
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
            document_repo.mark_failed(db, document, _GAVE_UP)
        else:
            dispatch_document(db, document)
            requeued += 1
    return requeued


# 抓 PDF 文字 → 切段 → 每段轉向量 → 存進 chunks 表 → 更新 document 狀態
# 整份文件是一個 transaction：全部成功才一起生效，中途任何一步失敗就整批丟掉，不會留下半套 chunk
def process_document(db: Session, document: Document) -> Document:
    # 次數用完就不處理了。工單可能從好幾個地方來（SQS 自動重送、定期掃描），統一在這裡擋
    if document.attempts >= MAX_ATTEMPTS:
        return document_repo.mark_failed(db, document, _GAVE_UP)
    document_repo.increment_attempts(db, document)

    try:
        # document.storage_path 現在存的是 S3 key，不是本機路徑，要先把內容抓下來
        content = s3_client.download_file(document.storage_path)
        # pdf_parser.extract_text 要的是一個「檔案物件」，io.BytesIO 把一包 bytes 包裝成檔案物件，
        # 跟上傳那邊 file.file（UploadFile 的底層）是同一種東西
        text = pdf_parser.extract_text(io.BytesIO(content), max_pages=MAX_PAGES)
        # 沒出錯不代表結果合理：抓不到任何字的話，存 0 段 chunk 然後標「完成」會誤導使用者
        if not text:
            raise NoExtractableText
        pieces = chunker.chunk_text(text)
        # 先清掉這份文件舊的 chunk：同一份文件處理兩次（重試）結果跟處理一次一樣，不會變兩倍
        chunk_repo.delete_for_document(db, document.id)
        # 一批一批送去轉向量：range(0, 總段數, 100) → 0、100、200……，每次切出 100 段
        for start in range(0, len(pieces), EMBED_BATCH_SIZE):
            batch = pieces[start : start + EMBED_BATCH_SIZE]
            embeddings = openai_client.embed_texts(batch)
            # zip：把兩個 list 一對一配對；strict=True 代表兩邊長度不一樣就直接報錯，不會默默少存幾段
            for offset, (piece, embedding) in enumerate(zip(batch, embeddings, strict=True)):
                chunk_repo.create(db, document.id, start + offset, piece, embedding)
    # 認得出來的錯誤：給使用者明確的原因，讓他知道該換檔案還是可以重試
    except pdf_parser.UnreadablePdf:
        return _fail(db, document, "PDF 檔案損壞或無法讀取")
    except pdf_parser.TooManyPages:
        return _fail(db, document, f"超過 {MAX_PAGES} 頁的上限")
    except NoExtractableText:
        return _fail(db, document, "抓不到任何文字，可能是掃描檔（目前不支援）")
    # OpenAI 的錯誤：先在 openai_client 裡分好類，這裡決定業務上怎麼處理
    except openai_client.OpenAIPermanentError:
        return _fail(db, document, "系統設定有誤，請聯絡管理員")
    except openai_client.OpenAITemporaryError:
        return _defer_or_fail(db, document)
    # 其他預料之外的錯誤：只給一般性的訊息，不把內部錯誤細節（可能含系統資訊）直接顯示給使用者
    # 刻意接住所有錯誤（noqa 告訴 ruff 這是故意的）：上面認得的錯誤都分類處理過了，
    # 這裡是最後一道防線，確保不管發生什麼事，文件都會被標成失敗，不會卡在處理中
    except Exception:  # noqa: BLE001
        return _fail(db, document, "處理時發生錯誤，可以稍後重試")

    # mark_processed 裡的 commit 會讓「所有新 chunk + 狀態改成 processed」在同一刻一起生效
    return document_repo.mark_processed(db, document)


# OpenAI 暫時連不上時：雲端而且還有剩餘次數 → 留在「處理中」等下一回合；否則標失敗
def _defer_or_fail(db: Session, document: Document) -> Document:
    db.rollback()
    # 本機沒有 SQS，沒辦法延後，只能直接標失敗讓使用者自己重試
    if settings.sqs_queue_url is None:
        return document_repo.mark_failed(db, document, "AI 服務暫時無法使用，請稍後按重試")
    if document.attempts >= MAX_ATTEMPTS:
        return document_repo.mark_failed(
            db, document, f"AI 服務暫時無法使用，已自動重試 {MAX_ATTEMPTS} 次，請稍後按重試"
        )
    # 給使用者看的提示，狀態還是「處理中」
    document_repo.set_error_message(db, document, "AI 服務暫時無法使用，稍後會自動重試")
    raise RetryLater


# 處理失敗的收尾：rollback 把這次還沒 commit 的東西（刪掉的舊 chunk、新加的 chunk）全部撤回，再標失敗
def _fail(db: Session, document: Document, reason: str) -> Document:
    db.rollback()
    return document_repo.mark_failed(db, document, reason)


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
