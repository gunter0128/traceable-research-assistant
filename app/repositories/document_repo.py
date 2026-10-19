# documents 表的資料庫存取。只跟 DB 講話 不放商業邏輯

from datetime import datetime, timezone

from sqlalchemy import and_, func, or_
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


# 何時呼叫：上傳完成時，確認這個 S3 檔案還沒有建過紀錄
def exists_with_storage_path(db: Session, storage_path: str) -> bool:
    return db.query(Document.id).filter(Document.storage_path == storage_path).first() is not None


def delete(db: Session, document: Document) -> None:
    db.delete(document)
    db.commit()


# 何時呼叫：沒有租約可以對的地方要標失敗（工單送不出去）
# reason：給使用者看的失敗原因
def mark_failed(db: Session, document: Document, reason: str) -> Document:
    document.status = "failed"
    document.error_message = reason
    document.claim_token = None
    db.commit()
    db.refresh(document)
    return document


# 何時呼叫：送工單到 SQS 之前，記下送出的時間（「排隊中」超過 15 分鐘就算逾時，從這裡開始算）
def mark_queued(db: Session, document: Document) -> Document:
    document.queued_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(document)
    return document


# 何時呼叫：使用者手動按重試。使用者明確要求重來，次數歸零，租約、進度清掉
def reset_for_retry(db: Session, document: Document) -> Document:
    document.status = "uploaded"
    document.attempts = 0
    document.error_message = None
    document.claim_token = None
    document.progress_done = None
    document.progress_total = None
    db.commit()
    db.refresh(document)
    return document


# ---------- 租約（docs/decisions/008）----------
# 下面這些都是「有條件的更新」：一句 UPDATE ... WHERE 條件，條件不成立就一列都不會改到
# .update(...) 回傳實際改到幾列，用它判斷成功或失敗。資料庫保證同一列同時只有一個 UPDATE 能改它，
# 所以兩個 Lambda 同時搶同一份文件，只會有一個成功
# synchronize_session=False：直接對資料庫下 UPDATE，不去同步已經載入記憶體的物件（之後要用的話自己 refresh）


# 何時呼叫：Lambda 開始處理之前。成功才能處理，失敗代表別人正在處理、或文件已經不需要處理了
# 條件：排隊中，或處理中但上一個 Lambda 在 stale_before 之後就沒報過平安（判定它死了，可以接手）
# 取得租約的同時次數 +1、記下開始時間，跟租約在同一句裡，一定一起成功或一起失敗
def claim(db: Session, document_id: int, token: str, stale_before: datetime) -> bool:
    now = datetime.now(timezone.utc)
    claimed = (
        db.query(Document)
        .filter(Document.id == document_id)
        .filter(
            # or_ / and_：SQL 的 OR / AND，用 Python 的 or / and 會變成 Python 自己判斷，不會變成 SQL
            or_(
                Document.status == "uploaded",
                and_(Document.status == "processing", Document.heartbeat_at < stale_before),
            )
        )
        .update(
            {
                Document.status: "processing",
                Document.claim_token: token,
                Document.heartbeat_at: now,
                Document.attempts: Document.attempts + 1,
                Document.progress_done: None,
                Document.progress_total: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return claimed == 1


# 何時呼叫：處理過程中報平安。done / total 有給的話順便更新進度
# 回傳 False 代表租約已經不是自己的了（被判定中斷、或別人接手了），呼叫的人要放棄這次處理
def heartbeat(
    db: Session, document_id: int, token: str, done: int | None = None, total: int | None = None
) -> bool:
    values = {Document.heartbeat_at: datetime.now(timezone.utc)}
    if done is not None:
        values[Document.progress_done] = done
    if total is not None:
        values[Document.progress_total] = total
    updated = (
        db.query(Document)
        .filter(Document.id == document_id, Document.claim_token == token)
        .filter(Document.status == "processing")
        .update(values, synchronize_session=False)
    )
    db.commit()
    return updated == 1


# 何時呼叫：處理文件時，檢查同一個 workspace 有沒有內容一模一樣、已經處理好的文件（docs/decisions/022）
# 只比對處理成功的：失敗或取消的文件不算數，使用者把它刪掉重傳也不會被擋
def find_duplicate(db: Session, workspace_id: int, content_hash: str, exclude_id: int) -> Document | None:
    return (
        db.query(Document)
        .filter(Document.workspace_id == workspace_id, Document.content_hash == content_hash)
        .filter(Document.status == "processed", Document.id != exclude_id)
        .first()
    )


# 何時呼叫：全部處理完，要把「新的 chunk + 狀態改成完成」一起存進去
# 先下 UPDATE 看租約還是不是自己的，是才 commit（chunk 跟狀態在同一個 transaction，一起生效）；
# 不是就 rollback，這次寫的 chunk 全部丟掉，不會跟接手的 Lambda 存出兩份
# content_hash：檔案內容的指紋，處理成功時一起記下（docs/decisions/022）
def finish_processed(db: Session, document_id: int, token: str, content_hash: str | None = None) -> bool:
    updated = (
        db.query(Document)
        .filter(Document.id == document_id, Document.claim_token == token)
        .filter(Document.status == "processing")
        .update(
            {
                Document.status: "processed",
                Document.processed_at: datetime.now(timezone.utc),
                Document.error_message: None,  # 之前失敗過、重試成功的話，把舊的失敗原因清掉
                Document.claim_token: None,
                Document.content_hash: content_hash,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        return False
    db.commit()
    return True


# 何時呼叫：處理中出錯要標失敗。一樣要租約還是自己的才標，不然會蓋掉接手的 Lambda 的狀態
def fail_with_lease(db: Session, document_id: int, token: str, reason: str) -> bool:
    updated = (
        db.query(Document)
        .filter(Document.id == document_id, Document.claim_token == token)
        .filter(Document.status == "processing")
        .update(
            {Document.status: "failed", Document.error_message: reason, Document.claim_token: None},
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


# 何時呼叫：OpenAI / S3 暫時連不上、等下一回合。放掉租約、回到排隊中，重新計算排隊時間，
# 留一個提示給使用者看（「…稍後會自動重試」）
def defer_with_lease(db: Session, document_id: int, token: str, message: str) -> bool:
    updated = (
        db.query(Document)
        .filter(Document.id == document_id, Document.claim_token == token)
        .filter(Document.status == "processing")
        .update(
            {
                Document.status: "uploaded",
                Document.claim_token: None,
                Document.error_message: message,
                Document.queued_at: datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


# 何時呼叫：使用者按「取消處理」（docs/decisions/009）
# 條件不用對代碼：使用者手上沒有代碼，只要文件還在排隊中或處理中就可以取消
# 收回租約（清掉代碼）就夠了，不用另外通知正在處理的 Lambda：
#   它下次報平安、或要存結果時，代碼對不上，就會自己停下來、把寫到一半的全部丟掉
#   還在排隊的，Lambda 拿到工單時搶不到租約（不是排隊中了），直接跳過
# in_(...)：SQL 的 IN，「狀態是這幾個其中之一」
def cancel(db: Session, document_id: int) -> bool:
    updated = (
        db.query(Document)
        .filter(Document.id == document_id)
        .filter(Document.status.in_(("uploaded", "processing")))
        .update(
            {Document.status: "cancelled", Document.claim_token: None, Document.error_message: None},
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


# 何時呼叫：使用者查文件時、每小時的 sweeper。把卡住的文件標失敗，回傳標了幾份
#   處理中，最後報平安在 heartbeat_before 之前 → interrupted_reason（程式死了）
#   排隊中，最後送出工單在 queued_before 之前 → timed_out_reason（一直沒人來處理）
# document_id / workspace_id 有給就只檢查那些文件，都沒給就全部檢查（sweeper）
# coalesce(a, b, c)：a 是空的就用 b，再空就用 c。時間沒記到的，往前找一個有的
def expire_overdue(
    db: Session,
    *,
    heartbeat_before: datetime,
    queued_before: datetime,
    interrupted_reason: str,
    timed_out_reason: str,
    document_id: int | None = None,
    workspace_id: int | None = None,
) -> int:
    def scoped():
        query = db.query(Document)
        if document_id is not None:
            query = query.filter(Document.id == document_id)
        if workspace_id is not None:
            query = query.filter(Document.workspace_id == workspace_id)
        return query

    interrupted = (
        scoped()
        .filter(Document.status == "processing")
        .filter(func.coalesce(Document.heartbeat_at, Document.queued_at, Document.uploaded_at) < heartbeat_before)
        .update(
            {Document.status: "failed", Document.error_message: interrupted_reason, Document.claim_token: None},
            synchronize_session=False,
        )
    )
    timed_out = (
        scoped()
        .filter(Document.status == "uploaded")
        .filter(func.coalesce(Document.queued_at, Document.uploaded_at) < queued_before)
        .update(
            {Document.status: "failed", Document.error_message: timed_out_reason, Document.claim_token: None},
            synchronize_session=False,
        )
    )
    db.commit()
    return interrupted + timed_out


# 何時呼叫：每小時的對帳，拿到「資料庫裡所有文件記錄的 S3 位置」，跟 S3 上實際的檔案比對
# 只查 storage_path 這一欄，不用把整筆文件都查出來
def list_storage_paths(db: Session) -> set[str]:
    return {path for (path,) in db.query(Document.storage_path).all()}


# 何時呼叫：刪除 workspace 之前，先記下底下所有文件的 S3 位置（紀錄刪掉之後就查不到了）
def list_storage_paths_for_workspace(db: Session, workspace_id: int) -> list[str]:
    rows = db.query(Document.storage_path).filter(Document.workspace_id == workspace_id).all()
    return [path for (path,) in rows]
