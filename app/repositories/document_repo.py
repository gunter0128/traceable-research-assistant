# documents 表的資料庫存取。只跟 DB 講話 不放商業邏輯

from datetime import datetime, timezone

from sqlalchemy import func
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


# 何時呼叫：使用者手動按重試。使用者明確要求重來：回到排隊中，次數歸零，租約、失敗原因清掉
# 有條件的更新：只有失敗、已取消的文件改得到（跟下面租約的寫法一樣，一句 UPDATE ... WHERE）
#   已取消：取消功能拿掉了（docs/decisions/046），但資料庫裡可能還有以前取消的文件，要讓它們能重試
#   兩個重試同時進來（連按兩次），只有一個改得到；第二個不會把已經被 Lambda 拿去處理的文件又改回排隊中
# 回傳 False 代表這份文件現在不能重試（狀態已經不是失敗、已取消）
# in_(...)：SQL 的 IN，「狀態是這幾個其中之一」
def reset_for_retry(db: Session, document_id: int) -> bool:
    updated = (
        db.query(Document)
        .filter(Document.id == document_id, Document.status.in_(("failed", "cancelled")))
        .update(
            {
                Document.status: "uploaded",
                Document.attempts: 0,
                Document.error_message: None,
                Document.claim_token: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


# ---------- 租約（docs/decisions/008）----------
# 下面這些都是「有條件的更新」：一句 UPDATE ... WHERE 條件，條件不成立就一列都不會改到
# .update(...) 回傳實際改到幾列，用它判斷成功或失敗。資料庫保證同一列同時只有一個 UPDATE 能改它，
# 所以兩個 Lambda 同時搶同一份文件，只會有一個成功
# synchronize_session=False：直接對資料庫下 UPDATE，不去同步已經載入記憶體的物件（之後要用的話自己 refresh）


# 何時呼叫：Lambda 開始處理之前。成功才能處理
# 條件：只有排隊中（uploaded）的文件搶得到（docs/decisions/046）
#   處理中的不行，就算上一個 Lambda 已經很久沒報平安也不接手：等 sweeper 或使用者查看時標成失敗，再由使用者按重試
#   已完成、已失敗、已取消的也不行：重複的工單、文件已經不需要處理了
# 取得租約的同時次數 +1（只用來寫 log）、記下開始時間（第一次報平安），跟租約在同一句裡，一定一起成功或一起失敗
def claim(db: Session, document_id: int, token: str) -> bool:
    claimed = (
        db.query(Document)
        .filter(Document.id == document_id, Document.status == "uploaded")
        .update(
            {
                Document.status: "processing",
                Document.claim_token: token,
                Document.heartbeat_at: datetime.now(timezone.utc),
                Document.attempts: Document.attempts + 1,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return claimed == 1


# 何時呼叫：處理過程中報平安（解析切段完、每轉完一批向量），更新「最後一次有進度的時間」（docs/decisions/046）
# 一句 UPDATE、馬上 commit：自己一個很短的交易
# 回傳 False 代表租約已經不是自己的了（被判定中斷、文件被刪掉），呼叫的人要停下來
def update_heartbeat(db: Session, document_id: int, token: str) -> bool:
    updated = (
        db.query(Document)
        .filter(Document.id == document_id, Document.claim_token == token)
        .filter(Document.status == "processing")
        .update({Document.heartbeat_at: datetime.now(timezone.utc)}, synchronize_session=False)
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


# 何時呼叫：全部轉完向量、要存結果的時候。把狀態改成「完成」，但「不 commit」：
#   呼叫的人接著寫 chunks，再一起 commit，chunks 和「完成」才會在同一刻生效
# 條件：租約還是自己的；改不到（回傳 False）代表租約已經不是自己的了，呼叫的人要 rollback、整批放棄
# 先改狀態再寫 chunks：UPDATE 會鎖住這一列，到 commit 之前別人（sweeper、使用者查看時）都改不了它
# content_hash：檔案內容的指紋，處理成功時一起記下（docs/decisions/022）
def mark_processed(db: Session, document_id: int, token: str, content_hash: str) -> bool:
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
    return updated == 1


# 何時呼叫：處理中出錯要標失敗。一樣要租約還是自己的才標，不然會蓋掉別人的狀態（例如已經被判定中斷，使用者按了重試，新的 Lambda 正在處理）
def mark_failed_if_claimed(db: Session, document_id: int, token: str, reason: str) -> bool:
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


# 何時呼叫：使用者查文件時、每小時的 sweeper。把卡住的文件標失敗，回傳標了幾份
#   處理中，最後報平安在 heartbeat_before 之前 → interrupted_reason（程式死了）
#   排隊中，最後送出工單在 queued_before 之前 → timed_out_reason（一直沒人來處理）
# 只動「處理中」「排隊中」的文件：已完成、已失敗的不管多久以前都不會被改到
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
    interrupted = (
        _documents_in_scope(db, document_id, workspace_id)
        .filter(Document.status == "processing")
        .filter(func.coalesce(Document.heartbeat_at, Document.queued_at, Document.uploaded_at) < heartbeat_before)
        .update(
            {Document.status: "failed", Document.error_message: interrupted_reason, Document.claim_token: None},
            synchronize_session=False,
        )
    )
    timed_out = (
        _documents_in_scope(db, document_id, workspace_id)
        .filter(Document.status == "uploaded")
        .filter(func.coalesce(Document.queued_at, Document.uploaded_at) < queued_before)
        .update(
            {Document.status: "failed", Document.error_message: timed_out_reason, Document.claim_token: None},
            synchronize_session=False,
        )
    )
    db.commit()
    return interrupted + timed_out


# expire_overdue 要檢查哪些文件：有給 document_id / workspace_id 就只看那些，都沒給就是全部
def _documents_in_scope(db: Session, document_id: int | None, workspace_id: int | None):
    query = db.query(Document)
    if document_id is not None:
        query = query.filter(Document.id == document_id)
    if workspace_id is not None:
        query = query.filter(Document.workspace_id == workspace_id)
    return query


# 何時呼叫：每小時的對帳，拿到「資料庫裡所有文件記錄的 S3 位置」，跟 S3 上實際的檔案比對
# 只查 storage_path 這一欄，不用把整筆文件都查出來
def list_storage_paths(db: Session) -> set[str]:
    return {path for (path,) in db.query(Document.storage_path).all()}


# 何時呼叫：刪除 workspace 之前，先記下底下所有文件的 S3 位置（紀錄刪掉之後就查不到了）
def list_storage_paths_for_workspace(db: Session, workspace_id: int) -> list[str]:
    rows = db.query(Document.storage_path).filter(Document.workspace_id == workspace_id).all()
    return [path for (path,) in rows]
