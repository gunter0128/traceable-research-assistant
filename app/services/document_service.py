# document 的規則：存檔案、透過 workspace 判斷 ownership、CRUD 流程
# 不寫 SQL 不碰 HTTP

import contextlib
import io
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core import chunker, openai_client, pdf_parser, s3_client, sqs_client
from app.core.config import settings
from app.core.db import SessionLocal
from app.models.document import Document
from app.repositories import chunk_repo, document_repo
from app.services import workspace_service

# 一份文件最多被處理幾次，超過就直接標成失敗，不再重試
MAX_ATTEMPTS = 3
_GAVE_UP = f"處理了 {MAX_ATTEMPTS} 次都沒有成功，已停止自動重試"
# 處理中超過這麼久沒報平安，就判定程式死了（docs/decisions/008）
# 正常情況下最長的空檔是一次 OpenAI 嘗試 30 秒 + 一次等待最多 60 秒，約 1.5 分鐘，5 分鐘不會誤判
HEARTBEAT_TIMEOUT = timedelta(minutes=5)
_INTERRUPTED = "處理中斷，請按重試"
# 排隊中超過這麼久還沒人開始處理，就判定出事了（docs/decisions/007）
# 排隊本來就可能很久：同時最多 2 個 Lambda，一次上傳很多份時後面的要等；等下一回合重試也在排隊中
QUEUE_TIMEOUT = timedelta(minutes=15)
_TIMED_OUT = "處理逾時，請按重試"
# 上傳檔案大小上限：一般論文 1~5 MB、圖多的報告 10~20 MB
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 頁數上限：檔案大小不等於內容量，純文字的 PDF 很小卻可能有上千頁，處理時間會超過 Lambda 的 15 分鐘
MAX_PAGES = 300
# 文件在 S3 上的 key 前綴，上傳時加、對帳時用它列出所有文件檔案
STORAGE_PREFIX = "documents/"
# 對帳時，沒有紀錄的 S3 檔案要存在超過這麼久才刪：上傳是先存 S3 再建紀錄，
# 剛好在這兩步之間對帳的話，會看到一個「還沒有紀錄」的新檔案，不能把它誤刪
ORPHAN_GRACE = timedelta(days=1)
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


# OpenAI 暫時連不上、而且還有剩餘次數：這次先不處理，文件回到「排隊中」
# 丟給呼叫的人（worker）決定怎麼延後，worker 會叫 SQS 2 分鐘後再把工單送回來
class RetryLater(Exception):
    pass


# 只有失敗、或使用者自己取消的文件可以手動重試（排隊中、處理中、已完成的不行）
class DocumentNotRetryable(Exception):
    pass


# 只有排隊中、處理中的文件可以取消（已完成、已失敗的沒有東西可以停）
class DocumentNotCancellable(Exception):
    pass


# 處理到一半發現租約已經不是自己的了（被判定中斷、或別人接手了），要放棄這次處理
class LeaseLost(Exception):
    pass


# 一次處理用的租約：記住文件編號和自己的代碼，負責報平安（docs/decisions/008）
# 報平安要「另外寫入、立刻 commit」，不能跟處理用的 session 混在一起：
# 處理用的 session 裡有還沒 commit 的 chunk，要等全部做完才 commit，在那之前報的平安別人都看不到
class _Lease:
    def __init__(self, document_id: int, token: str):
        self.document_id = document_id
        self.token = token

    # done / total：順便更新進度。租約不是自己的了就丟 LeaseLost
    def beat(self, done: int | None = None, total: int | None = None) -> None:
        # with SessionLocal() as db：開一條獨立的 session，區塊結束自動關掉
        with SessionLocal() as db:
            if not document_repo.heartbeat(db, self.document_id, self.token, done, total):
                raise LeaseLost


def list_documents(db: Session, workspace_id: int, owner_id: int) -> list[Document]:
    # 借用 workspace 的 ownership 檢查：先確認這個 workspace 是不是你的
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)
    # 使用者在看的時候就檢查有沒有卡住：前端每 3 秒會查一次，卡住的文件馬上就會顯示出來，不用等每小時的 sweeper
    # 先標再查：標完之後查出來的就是最新的狀態
    _expire_overdue(db, workspace_id=workspace_id)
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
    key = f"{STORAGE_PREFIX}{uuid4().hex}_{file.filename}"
    # file.file.read() 把上傳內容讀出來變成一包 bytes (file 是 UploadFile 這個物件 .file 是它裡面裝真正內容的屬性)
    s3_client.upload_file(key, file.file.read())

    # S3 和資料庫沒辦法包在同一個 transaction 裡（見 docs/decisions/006）
    # 先存 S3 再建紀錄：中途失敗只會留下沒人用的檔案（無害），不會留下指向不存在檔案的紀錄（有害）
    try:
        document = document_repo.create(db, workspace_id, file.filename, key)
    except Exception:
        db.rollback()
        # 補救：紀錄建不起來，把剛存的檔案刪掉。連這步都失敗的話，留給每小時的對帳清
        # contextlib.suppress：把裡面丟出的錯誤吞掉。這裡刪不掉也沒關係，重點是把原本的錯誤往外丟
        with contextlib.suppress(s3_client.StorageError):
            s3_client.delete_file(key)
        raise
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


# 使用者手動按重試：失敗或已取消的文件可以，次數歸零後重新交出去
def retry_document(db: Session, document_id: int, owner_id: int) -> Document:
    document = get_owned_document(db, document_id, owner_id)
    if document.status not in ("failed", "cancelled"):
        raise DocumentNotRetryable
    document_repo.reset_for_retry(db, document)
    return dispatch_document(db, document)


# 使用者按「取消處理」（docs/decisions/009）：不想等了，先停下來，文件留著之後可以重試或刪掉
# 跟刪除不一樣：刪除是整份文件不要了；取消是先停下來，等一下再說
def cancel_document(db: Session, document_id: int, owner_id: int) -> Document:
    get_owned_document(db, document_id, owner_id)
    # 判斷「能不能取消」交給資料庫的有條件更新，不在這裡先查狀態再改：
    # 查和改之間，Lambda 可能剛好處理完，先查再改會把「已完成」的文件誤改成「已取消」
    if not document_repo.cancel(db, document_id):
        raise DocumentNotCancellable
    document = document_repo.get_by_id(db, document_id)
    db.refresh(document)
    return document


# 定期掃描（每小時一次，由排程叫起來）：把卡住的文件標失敗
# 給沒有人在看的文件用（有人在看的，list_documents / get_owned_document 已經當場處理了）
# 回傳標了幾份，方便在 log 裡看
def expire_overdue_documents(db: Session) -> int:
    return _expire_overdue(db)


# 卡住的兩種情況（docs/decisions/008）：
#   處理中，超過 5 分鐘沒報平安 → 程式死了 → 「處理中斷，請按重試」
#   排隊中，送出工單超過 15 分鐘還沒人開始處理 → 「處理逾時，請按重試」
def _expire_overdue(db: Session, document_id: int | None = None, workspace_id: int | None = None) -> int:
    now = datetime.now(timezone.utc)
    return document_repo.expire_overdue(
        db,
        heartbeat_before=now - HEARTBEAT_TIMEOUT,
        queued_before=now - QUEUE_TIMEOUT,
        interrupted_reason=_INTERRUPTED,
        timed_out_reason=_TIMED_OUT,
        document_id=document_id,
        workspace_id=workspace_id,
    )


# 抓 PDF 文字 → 切段 → 每段轉向量 → 存進 chunks 表 → 更新 document 狀態
# 整份文件是一個 transaction：全部成功才一起生效，中途任何一步失敗就整批丟掉，不會留下半套 chunk
# 開始前要先取得租約（docs/decisions/008）：同一份文件同時只能有一個 Lambda 在處理，處理中一直報平安
def process_document(db: Session, document: Document) -> Document:
    token = uuid4().hex
    stale_before = datetime.now(timezone.utc) - HEARTBEAT_TIMEOUT
    # 搶不到租約：別人正在處理、文件已經處理完 / 失敗了（重複的工單），什麼都不做
    if not document_repo.claim(db, document.id, token, stale_before):
        db.refresh(document)
        return document
    db.refresh(document)  # 拿到租約的同時次數 +1 了，重新讀一次
    lease = _Lease(document.id, token)

    # 次數用完就不處理了。工單可能從好幾個地方來（SQS 自動重送、使用者重試），統一在這裡擋
    if document.attempts > MAX_ATTEMPTS:
        return _fail(db, document, lease, _GAVE_UP)

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
        lease.beat(done=0, total=len(pieces))
        # 先清掉這份文件舊的 chunk：同一份文件處理兩次（重試）結果跟處理一次一樣，不會變兩倍
        chunk_repo.delete_for_document(db, document.id)
        # 一批一批送去轉向量：range(0, 總段數, 100) → 0、100、200……，每次切出 100 段
        for start in range(0, len(pieces), EMBED_BATCH_SIZE):
            batch = pieces[start : start + EMBED_BATCH_SIZE]
            # on_attempt=lease.beat：每次呼叫 OpenAI 之前（包括重試）都報一次平安
            embeddings = openai_client.embed_texts(batch, on_attempt=lease.beat)
            # zip：把兩個 list 一對一配對；strict=True 代表兩邊長度不一樣就直接報錯，不會默默少存幾段
            for offset, (piece, embedding) in enumerate(zip(batch, embeddings, strict=True)):
                chunk_repo.create(db, document.id, start + offset, piece, embedding)
            lease.beat(done=start + len(batch))
    # 租約已經不是自己的了：別人接手了，或被判定中斷。這次寫的全部丟掉，不碰文件狀態（不是自己的了）
    except LeaseLost:
        db.rollback()
        db.refresh(document)
        return document
    # 認得出來的錯誤：給使用者明確的原因，讓他知道該換檔案還是可以重試
    except pdf_parser.UnreadablePdf:
        return _fail(db, document, lease, "PDF 檔案損壞或無法讀取")
    except pdf_parser.TooManyPages:
        return _fail(db, document, lease, f"超過 {MAX_PAGES} 頁的上限")
    except NoExtractableText:
        return _fail(db, document, lease, "抓不到任何文字，可能是掃描檔（目前不支援）")
    # OpenAI 的錯誤：先在 openai_client 裡分好類，這裡決定業務上怎麼處理
    # S3 的錯誤：一樣在 s3_client 裡分好類。檔案不見了重試也沒用，訊息要說清楚，不能叫使用者「稍後重試」
    except s3_client.FileMissing:
        return _fail(db, document, lease, "找不到上傳的檔案，請刪除後重新上傳")
    except (openai_client.OpenAIPermanentError, s3_client.StoragePermanentError):
        return _fail(db, document, lease, "系統設定有誤，請聯絡管理員")
    except openai_client.OpenAITemporaryError:
        return _defer_or_fail(db, document, lease, "AI 服務")
    except s3_client.StorageTemporaryError:
        return _defer_or_fail(db, document, lease, "儲存服務")
    # 其他預料之外的錯誤：只給一般性的訊息，不把內部錯誤細節（可能含系統資訊）直接顯示給使用者
    # 刻意接住所有錯誤（noqa 告訴 ruff 這是故意的）：上面認得的錯誤都分類處理過了，
    # 這裡是最後一道防線，確保不管發生什麼事，文件都會被標成失敗，不會卡在處理中
    except Exception:  # noqa: BLE001
        return _fail(db, document, lease, "處理時發生錯誤，可以稍後重試")

    # 「所有新 chunk + 狀態改成完成」在同一刻一起生效；租約已經不是自己的了，就整批丟掉
    document_repo.finish_processed(db, document.id, token)
    db.refresh(document)
    return document


# 外部服務（OpenAI、S3）暫時連不上時：雲端而且還有剩餘次數 → 回到排隊中等下一回合；否則標失敗
# service：哪個服務出問題，放進給使用者看的訊息裡（「AI 服務」/「儲存服務」）
def _defer_or_fail(db: Session, document: Document, lease: _Lease, service: str) -> Document:
    # 本機沒有 SQS，沒辦法延後，只能直接標失敗讓使用者自己重試
    if settings.sqs_queue_url is None:
        return _fail(db, document, lease, f"{service}暫時無法使用，請稍後按重試")
    if document.attempts >= MAX_ATTEMPTS:
        return _fail(
            db, document, lease, f"{service}暫時無法使用，已自動重試 {MAX_ATTEMPTS} 次，請稍後按重試"
        )
    db.rollback()
    # 放掉租約、回到排隊中，留一個提示給使用者。租約已經不是自己的了就不延後（別人在處理了）
    if document_repo.defer_with_lease(
        db, document.id, lease.token, f"{service}暫時無法使用，稍後會自動重試"
    ):
        raise RetryLater
    db.refresh(document)
    return document


# 處理失敗的收尾：rollback 把這次還沒 commit 的東西（刪掉的舊 chunk、新加的 chunk）全部撤回，再標失敗
# 一樣要租約還是自己的才標，不然會蓋掉接手的 Lambda 的狀態
def _fail(db: Session, document: Document, lease: _Lease, reason: str) -> Document:
    db.rollback()
    document_repo.fail_with_lease(db, document.id, lease.token, reason)
    db.refresh(document)
    return document


def get_owned_document(db: Session, document_id: int, owner_id: int) -> Document:
    document = document_repo.get_by_id(db, document_id)
    if document is None:
        raise DocumentNotFound
    # Document 沒有自己的 owner_id，透過它的 workspace 判斷
    if document.workspace.owner_id != owner_id:
        raise NotDocumentOwner
    # 查單一份文件時也檢查有沒有卡住（重試也會經過這裡，卡住的文件就變成可以重試的「失敗」）
    if _expire_overdue(db, document_id=document_id):
        db.refresh(document)
    return document


def delete_document(db: Session, document_id: int, owner_id: int) -> None:
    document = get_owned_document(db, document_id, owner_id)
    key = document.storage_path
    # 順序刻意是「先刪紀錄、再刪檔案」，跟上傳反過來，理由一樣：
    # 刪檔案失敗只會留下沒人用的檔案（對帳會清）；反過來的話，紀錄還在但檔案沒了，使用者看得到卻再也處理不了
    document_repo.delete(db, document)
    with contextlib.suppress(s3_client.StorageError):
        s3_client.delete_file(key)


# 每小時的對帳（跟卡住文件的掃描一起跑）：S3 上有檔案、資料庫卻沒有紀錄的，就是孤兒檔案，刪掉
# 回傳刪了幾個，方便在 log 裡看
def delete_orphan_files(db: Session) -> int:
    known = document_repo.list_storage_paths(db)
    cutoff = datetime.now(timezone.utc) - ORPHAN_GRACE
    deleted = 0
    for key, last_modified in s3_client.list_files(STORAGE_PREFIX):
        if key not in known and last_modified < cutoff:
            s3_client.delete_file(key)
            deleted += 1
    return deleted
