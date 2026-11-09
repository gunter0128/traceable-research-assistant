# document 的規則：上傳（發許可、建紀錄）、背景處理（解析 → 切段 → 轉向量）、重試、刪除、卡住的偵測
# ownership 透過 workspace 判斷
# 不寫 SQL 不碰 HTTP

import contextlib
import hashlib
import io
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.core import chunker, log, openai_client, pdf_parser, s3_client, sqs_client
from app.core.config import settings
from app.core.db import SessionLocal
from app.models.document import Document
from app.repositories import chunk_repo, document_repo
from app.services import usage_service, workspace_service

# 處理中超過這麼久沒報平安，就判定處理的程式已經不在了（docs/decisions/008、046）
# 報平安的時間點：取得租約時、解析切段完、每轉完一批向量。兩次之間最長的正常空檔：
#   下載 + 解析 + 切段：300 頁塞滿文字的 PDF 在筆電上量過約 3 秒（Lambda 比較慢，也在 1 分鐘以內）
#   一批轉向量加上它所有的重試：openai_client 的設定，5 次 × 30 秒 + 4 次等待 × 最多 60 秒 = 390 秒（6.5 分鐘）
# 10 分鐘比最長的一段多 3.5 分鐘。改 openai_client 的重試設定時要一起檢查（有測試擋）
# 不是絕對保證：例如單次請求的資料一直慢慢傳、每次都剛好不到 30 秒的等待上限，一批還是可能更久
HEARTBEAT_TIMEOUT = timedelta(minutes=10)
_INTERRUPTED = "處理中斷，請按重試"
# 排隊中超過這麼久還沒人開始處理，就判定出事了（docs/decisions/007）
# 排隊本來就可能很久：同時最多 2 個 Lambda，一次上傳很多份時後面的要等
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
# 上傳許可的有效時間：拿到許可之後，10 分鐘內要把檔案傳完（20 MB 就算網路很慢也夠了）
UPLOAD_URL_EXPIRES_SECONDS = 600
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


# 前端說「傳好了」，S3 上卻找不到這個檔案（沒傳成功、或上傳許可過期了）
class UploadNotFound(Exception):
    pass


# 前端給的 key 不是發給這個 workspace 的、或已經建過紀錄了
class InvalidUploadKey(Exception):
    pass


# 發給前端的上傳許可：url 和 fields 是瀏覽器直接傳到 S3 要用的，key 是傳好之後通知 API 時要帶回來的
@dataclass
class UploadTicket:
    url: str
    fields: dict
    key: str


# 只有失敗的文件可以手動重試（排隊中、處理中、已完成的不行）
# 已取消（cancelled）的也可以：取消功能拿掉了（docs/decisions/046），但資料庫裡可能還有以前取消的文件
class DocumentNotRetryable(Exception):
    pass


def list_documents(db: Session, workspace_id: int, owner_id: int) -> list[Document]:
    # 借用 workspace 的 ownership 檢查：先確認這個 workspace 是不是你的
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)
    # 使用者在看的時候就檢查有沒有卡住：前端每 3 秒會查一次，卡住的文件馬上就會顯示出來，不用等每小時的 sweeper
    # 先標再查：標完之後查出來的就是最新的狀態
    expire_overdue_documents(db, workspace_id=workspace_id)
    return document_repo.list_for_workspace(db, workspace_id)


# 上傳分三步（docs/decisions/013）：檔案不經過 API，瀏覽器直接傳到 S3
#   1. start_upload：前端說「我要傳一個檔案」，API 檢查完，發一張上傳許可
#   2. 瀏覽器拿許可把檔案直接傳到 S3
#   3. finish_upload：前端說「傳好了」，API 確認檔案真的在 S3 上，才建紀錄、交出去處理
# 為什麼：API 跑在 Lambda 上，Lambda 收的請求最大只有 6 MB，我們的上限是 20 MB
# 順序跟原本一樣是「先有 S3 的檔案、再建紀錄」（docs/decisions/006）：
# 傳了檔案卻沒有說「傳好了」的話，只會留下沒人用的檔案，每小時的對帳會清掉


# 第 1 步：發上傳許可。size_bytes 是前端說的檔案大小，先擋掉明顯太大的；真正的大小由 S3 依許可上的條件檢查
def start_upload(
    db: Session, workspace_id: int, owner_id: int, filename: str, size_bytes: int
) -> UploadTicket:
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)
    # 今天的用量到上限了就不收（docs/decisions/012）：收下來也沒辦法處理
    # 還沒到的話，這份會完整處理完，就算處理完之後超過了也一樣
    usage_service.ensure_within_quota(db, owner_id)
    _check_filename(filename)
    if size_bytes > MAX_UPLOAD_BYTES:
        raise FileTooLarge

    # key 的格式：documents/<workspace 編號>/<隨機碼>.pdf
    #   workspace 編號：第 3 步用它確認這個檔案是傳到這個 workspace 的，別人拿不到你的 key 來用
    #   uuid4().hex：隨機、幾乎不可能重複的識別碼，同名檔案不會互相覆蓋，也猜不到別人的 key
    #   不放檔名：檔名可能透露研究內容，S3 的 key 會出現在 log 和主控台裡（檔名存在資料庫）
    key = f"{STORAGE_PREFIX}{workspace_id}/{uuid4().hex}.pdf"
    form = s3_client.create_upload_form(key, MAX_UPLOAD_BYTES, UPLOAD_URL_EXPIRES_SECONDS)
    log.event("upload_started", size_bytes=size_bytes)
    return UploadTicket(url=form["url"], fields=form["fields"], key=key)


# 第 3 步：前端說「傳好了」。確認檔案真的在 S3 上、是這個 workspace 的，才建紀錄、交出去處理
def finish_upload(db: Session, workspace_id: int, owner_id: int, key: str, filename: str) -> Document:
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)
    _check_filename(filename)
    # 只接受發給這個 workspace 的 key：不然有人可以拿別人的 key，把別人的檔案「認領」到自己的 workspace
    if not key.startswith(f"{STORAGE_PREFIX}{workspace_id}/"):
        raise InvalidUploadKey
    # 同一個檔案只能建一筆紀錄：兩筆紀錄指向同一個檔案的話，刪掉其中一筆，另一筆的檔案也跟著不見了
    if document_repo.exists_with_storage_path(db, key):
        raise InvalidUploadKey
    try:
        size = s3_client.file_size(key)
    # 前端說傳好了，S3 上卻沒有：沒傳成功、或許可過期了
    except s3_client.FileMissing:
        raise UploadNotFound from None

    try:
        document = document_repo.create(db, workspace_id, filename, key)
    except Exception:
        db.rollback()
        # 補救：紀錄建不起來，把剛傳的檔案刪掉。連這步都失敗的話，留給每小時的對帳清
        # contextlib.suppress：把裡面丟出的錯誤吞掉。這裡刪不掉也沒關係，重點是把原本的錯誤往外丟
        with contextlib.suppress(s3_client.StorageError):
            s3_client.delete_file(key)
        raise
    log.bind_document(document.id)
    # 只記大小，不記檔名：檔名可能就透露研究內容（docs/decisions/010）
    log.event("uploaded", size_bytes=size)
    return dispatch_document(db, document)


# 只能上傳 PDF
def _check_filename(filename: str) -> None:
    if not filename or not filename.lower().endswith(".pdf"):
        raise InvalidFileType


# 把文件交出去處理（上傳、手動重試都走這裡）
# 雲端（有設定 SQS）：送工單，交給 Lambda 背景處理，立刻回傳（status 還是 uploaded）
# 本機（沒設定 SQS）：直接在這裡處理完才回傳
def dispatch_document(db: Session, document: Document) -> Document:
    if settings.sqs_queue_url is None:
        log.event("dispatched", mode="inline")
        processed = process_document(db, document)
        # 處理到一半，文件被刪掉了（例如使用者在別的分頁按了刪除）：API 回 404
        if processed is None:
            raise DocumentNotFound
        return processed

    # 先記時間再送：送出去之後 Lambda 可能馬上就處理完了，順序反過來的話時間會記在處理完之後
    document_repo.mark_queued(db, document)
    try:
        sqs_client.send_process_message(document.id)
    # BotoCoreError：連不上 AWS（網路、憑證）；ClientError：AWS 回錯誤（沒權限、佇列不存在）
    except (BotoCoreError, ClientError):
        log.event("dispatch_failed", logging.ERROR, exc_info=True)
        # 工單送不出去 = 沒有人會處理這份文件，直接標失敗讓使用者知道，可以手動重試
        return document_repo.mark_failed(db, document, "排入處理佇列失敗，可以重試")
    log.event("dispatched", mode="queue")
    return document


# 使用者手動按重試：失敗（或以前取消）的文件可以，回到排隊中、次數歸零，重新交出去
# 處理中斷、處理逾時的文件也走這裡：get_owned_document 會先把卡住的標成失敗，就能重試
def retry_document(db: Session, document_id: int, owner_id: int) -> Document:
    document = get_owned_document(db, document_id, owner_id)
    if document.status not in ("failed", "cancelled"):
        raise DocumentNotRetryable
    # 重試也會呼叫 OpenAI，跟上傳一樣先檢查用量
    usage_service.ensure_within_quota(db, owner_id)
    log.bind_document(document_id)
    log.event("retry_requested", previous_status=document.status)
    # 有條件地改回排隊中（commit）：上面檢查完到這裡之間，狀態可能已經被另一個重試請求改了
    # 改不到就當作不能重試，不會把已經被 Lambda 拿去處理的文件又改回排隊中
    if not document_repo.reset_for_retry(db, document_id):
        raise DocumentNotRetryable
    db.refresh(document)
    return dispatch_document(db, document)


# 把卡住的文件標失敗，回傳標了幾份（方便在 log 裡看）。卡住的兩種情況（docs/decisions/008、046）：
#   處理中，超過 10 分鐘沒報平安 → 處理的程式已經不在了 → 「處理中斷，請按重試」
#   排隊中，送出工單超過 15 分鐘還沒人開始處理 → 「處理逾時，請按重試」
# 誰呼叫：
#   每小時的 sweeper：不給範圍，全部檢查（沒有人在看的文件）
#   使用者查看時（list_documents / get_owned_document）：只檢查他在看的。地端版沒有 sweeper，就靠這裡
def expire_overdue_documents(db: Session, document_id: int | None = None, workspace_id: int | None = None) -> int:
    now = datetime.now(timezone.utc)
    expired = document_repo.expire_overdue(
        db,
        heartbeat_before=now - HEARTBEAT_TIMEOUT,
        queued_before=now - QUEUE_TIMEOUT,
        interrupted_reason=_INTERRUPTED,
        timed_out_reason=_TIMED_OUT,
        document_id=document_id,
        workspace_id=workspace_id,
    )
    # 有文件被判定卡住，代表某個 Lambda 死掉了或工單不見了，是要注意的事（WARNING）
    if expired:
        log.event("expired_overdue", logging.WARNING, count=expired, workspace_id=workspace_id)
    return expired


# 處理一份文件：取得租約 → 下載 → 查重複 → 解析、切段 → 轉向量 → 存 chunks 並標完成（docs/decisions/046）
# 輸入：db（呼叫的人的 session）、document（排隊中的文件）
# 回傳：處理完之後，資料庫裡這份文件最新的樣子（processed / failed；沒搶到租約、或處理到一半租約沒了，就是當時的狀態）
#   處理期間文件被刪掉了，回傳 None
#
# 資料庫的交易都很短，下載 PDF、呼叫 OpenAI 的時候，db 不持有交易：
#   取得租約、讀資料、查重複、報平安：各自是一個短交易，做完馬上 commit
#   用量：另開一個 session 寫、馬上 commit，跟 chunks 完全分開
#   存結果：chunks 和「完成」在同一個交易裡一起 commit
# 注意：commit 之後 document 物件的欄位會過期（expire_on_commit），再讀會重新查資料庫、又開一個交易，
# 所以要用的欄位在第 2 步先讀進一般變數，之後只用這些變數
def process_document(db: Session, document: Document) -> Document | None:
    document_id = document.id
    log.bind_document(document_id)
    started = time.perf_counter()

    # 1. 取得租約（commit）：只有排隊中的文件搶得到，兩個 Lambda 同時搶只會有一個成功
    #    搶不到：別人正在處理、文件已經處理完 / 失敗了（重複的工單、Lambda 死掉後 SQS 重送的工單）、或已經被刪掉
    token = uuid4().hex
    if not document_repo.claim(db, document_id, token):
        current = document_repo.get_by_id(db, document_id)  # 被刪掉了就是 None
        log.event("claim_skipped", status=current.status if current else "deleted")
        return current

    # 2. 把這次要用的資料讀進一般變數，然後結束這個讀取用的交易（沒有要存的東西）
    storage_path = document.storage_path
    workspace_id = document.workspace_id
    owner_id = document.workspace.owner_id  # 用量算在上傳的人（workspace 的主人）身上
    attempt = document.attempts  # 第幾次處理（使用者按重試會歸零），只用來寫 log
    db.commit()
    log.event("claimed", attempt=attempt)

    try:
        # 3. 從 S3 下載 PDF
        content = s3_client.download_file(storage_path)                          # ← 呼叫 S3
        log.event("downloaded", size_bytes=len(content))

        # 4. 重複文件：同一個 workspace 已經有內容一樣、處理好的文件（docs/decisions/022）
        #    放在解析、轉向量之前：重複的文件不花轉向量的錢，也不會在搜尋時佔掉名額
        content_hash = hashlib.sha256(content).hexdigest()
        duplicate = document_repo.find_duplicate(db, workspace_id, content_hash, document_id)
        if duplicate is not None:
            return _fail(db, document_id, token, f"跟「{duplicate.filename}」的內容完全相同，不需要重複上傳")
        db.commit()  # 查完了，結束這個讀取用的交易（沒有要存的東西）

        # 5. 抽文字、切段（檔案壞掉、超過頁數會丟出錯誤，在下面接）
        #    抓不到任何字（通常是掃描檔）：存 0 段 chunk 然後標「完成」會誤導使用者，所以標失敗
        text = pdf_parser.extract_text(io.BytesIO(content), max_pages=MAX_PAGES)
        if not text:
            return _fail(db, document_id, token, "抓不到任何文字，可能是掃描檔（目前不支援）")
        pieces = chunker.chunk_text(text)
        log.event("chunked", chunks=len(pieces))

        # 報平安（commit）：下載、解析算一段，第一批轉向量另外算一段，兩段的時間不會加在一起
        if not document_repo.update_heartbeat(db, document_id, token):
            return _stop_after_losing_claim(db, document_id, started)

        # 6. 轉向量：一次送 100 段，向量先放在記憶體裡，最後才寫進資料庫
        vectors = []
        for start in range(0, len(pieces), EMBED_BATCH_SIZE):
            batch = pieces[start : start + EMBED_BATCH_SIZE]
            batch_vectors, usage = openai_client.embed_texts(batch)            # ← 呼叫 OpenAI
            vectors.extend(batch_vectors)

            # 記用量：另開一個 session、馬上 commit（usage_service.record 裡面會 commit）
            # 錢已經花掉了：之後處理失敗、db rollback，這筆紀錄也不能跟著消失；chunks 不在這個 session 裡，不會被帶出去
            with SessionLocal() as usage_db:
                usage_service.record(usage_db, owner_id, "document", usage, document_id=document_id)

            # 報平安（commit）。租約已經不是自己的了（被判定中斷、文件被刪掉）就停下來，不再花錢
            if not document_repo.update_heartbeat(db, document_id, token):
                return _stop_after_losing_claim(db, document_id, started)

        # 7. 存結果：chunks 和「完成」在同一個交易
        #    先有條件地改狀態（不 commit）：租約已經不是自己的就改不到，整批放棄
        if not document_repo.mark_processed(db, document_id, token, content_hash):
            return _stop_after_losing_claim(db, document_id, started)
        chunk_repo.delete_for_document(db, document_id)  # 舊的先清掉：同一份文件處理兩次，結果跟處理一次一樣
        for index, piece in enumerate(pieces):
            chunk_repo.create(db, document_id, index, piece, vectors[index])
        db.commit()                                                              # ← chunks 和「完成」一起生效

    # 8. 錯誤處理：認得出來的錯誤給使用者明確的原因，讓他知道該換檔案還是可以重試；全部用 _fail 收尾
    # OpenAI、S3 的錯誤先在 openai_client、s3_client 裡分好類，這裡決定業務上怎麼處理
    except pdf_parser.UnreadablePdf:
        return _fail(db, document_id, token, "PDF 檔案損壞或無法讀取")
    except pdf_parser.TooManyPages:
        return _fail(db, document_id, token, f"超過 {MAX_PAGES} 頁的上限")
    # 檔案不見了重試也沒用，訊息要說清楚，不能叫使用者「稍後重試」
    except s3_client.FileMissing:
        return _fail(db, document_id, token, "找不到上傳的檔案，請刪除後重新上傳")
    except (openai_client.OpenAIPermanentError, s3_client.StoragePermanentError):
        return _fail(db, document_id, token, "系統設定有誤，請聯絡管理員")
    # 暫時連不上：OpenAI 由 openai_client 自己寫的重試、S3 由 boto3 內建的重試先試過幾次了，走到這裡代表重試也沒用
    # 直接標失敗，讓使用者稍後按重試（docs/decisions/046）。不延後、不叫 SQS 晚點再送
    except openai_client.OpenAITemporaryError:
        return _fail(db, document_id, token, "AI 服務暫時無法使用，請稍後按重試")
    except s3_client.StorageTemporaryError:
        return _fail(db, document_id, token, "儲存服務暫時無法使用，請稍後按重試")
    # 其他預料之外的錯誤：只給一般性的訊息，不把內部錯誤細節（可能含系統資訊）直接顯示給使用者
    # 刻意接住所有錯誤（noqa 告訴 ruff 這是故意的）：這裡是最後一道防線，確保不管發生什麼事，文件都不會卡在處理中
    except Exception:  # noqa: BLE001
        # 給使用者的訊息很籠統，所以 log 裡一定要留完整的錯誤堆疊，不然事後完全不知道發生什麼事
        log.event("unexpected_error", logging.ERROR, exc_info=True)
        return _fail(db, document_id, token, "處理時發生錯誤，可以稍後重試")

    log.event("processed", chunks=len(pieces), total_ms=log.elapsed_ms(started))
    return document_repo.get_by_id(db, document_id)


# 租約已經不是自己的了（被判定中斷、文件被刪掉）：
# 停下來，撤回這次還沒 commit 的東西，也不碰文件的狀態（現在是別人的）
# 回傳資料庫裡最新的樣子；文件被刪掉了就是 None（不用 db.refresh：讀不到已經刪掉的文件會丟出錯誤）
def _stop_after_losing_claim(db: Session, document_id: int, started: float) -> Document | None:
    db.rollback()
    current = document_repo.get_by_id(db, document_id)
    log.event(
        "lease_lost", logging.WARNING, status=current.status if current else "deleted", total_ms=log.elapsed_ms(started)
    )
    return current


# 處理失敗的收尾：先 rollback 撤回這次還沒 commit 的東西，再有條件地把文件標失敗
# 條件：租約還是自己的才標（claim_token = token），不然會蓋掉別人的狀態；文件被刪掉了就改不到任何東西
# 回傳資料庫裡最新的樣子；文件被刪掉了就是 None
def _fail(db: Session, document_id: int, token: str, reason: str) -> Document | None:
    # sys.exc_info()[1]：目前正在處理的例外（_fail 大多是在 except 裡被呼叫的），記下是哪一種錯誤造成的
    # reason 是給使用者看的中文訊息，error_type 是給自己查的，告警也靠它（例如 OpenAIPermanentError）
    error = sys.exc_info()[1]
    log.event("failed", logging.WARNING, reason=reason, error_type=type(error).__name__ if error else None)
    # 先 rollback 才能再用 db：如果是資料庫出錯走到這裡，session 要先 rollback
    db.rollback()
    document_repo.mark_failed_if_claimed(db, document_id, token, reason)          # ← commit
    return document_repo.get_by_id(db, document_id)


def get_owned_document(db: Session, document_id: int, owner_id: int) -> Document:
    document = document_repo.get_by_id(db, document_id)
    if document is None:
        raise DocumentNotFound
    # Document 沒有自己的 owner_id，透過它的 workspace 判斷
    if document.workspace.owner_id != owner_id:
        raise NotDocumentOwner
    # 查單一份文件時也檢查有沒有卡住（重試也會經過這裡，卡住的文件就變成可以重試的「失敗」）
    if expire_overdue_documents(db, document_id=document_id):
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
