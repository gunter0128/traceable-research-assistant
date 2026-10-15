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


def delete(db: Session, document: Document) -> None:
    db.delete(document)
    db.commit()


# 何時呼叫：document_service.process_document() 把文字都切段、轉完向量、存進 chunks 表之後
def mark_processed(db: Session, document: Document) -> Document:
    document.status = "processed"
    document.processed_at = datetime.now(timezone.utc)
    document.error_message = None  # 之前失敗過、重試成功的話，把舊的失敗原因清掉
    db.commit()
    db.refresh(document)
    return document


# 何時呼叫：document_service.process_document() 中途出錯時（例如 PDF 解析失敗），讓 status 反映真實結果
# 也用在：工單送不出去、重試次數用完
# reason：給使用者看的失敗原因
def mark_failed(db: Session, document: Document, reason: str) -> Document:
    document.status = "failed"
    document.error_message = reason
    db.commit()
    db.refresh(document)
    return document


# 何時呼叫：OpenAI 暫時連不上、文件等下一回合再處理時，給使用者一個提示（狀態不變，還是處理中）
def set_error_message(db: Session, document: Document, message: str) -> Document:
    document.error_message = message
    db.commit()
    db.refresh(document)
    return document


# 何時呼叫：送工單到 SQS 之前，記下送出的時間（定期掃描用它判斷有沒有卡住）
def mark_queued(db: Session, document: Document) -> Document:
    document.queued_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(document)
    return document


# 何時呼叫：process_document() 一開始。這裡馬上 commit，不跟後面的 chunk 綁在一起：
# 就算處理到一半程式整個當掉（來不及 rollback），這一次也要被算進去
def increment_attempts(db: Session, document: Document) -> Document:
    document.attempts += 1
    db.commit()
    db.refresh(document)
    return document


# 何時呼叫：使用者手動按重試。使用者明確要求重來，次數歸零
def reset_for_retry(db: Session, document: Document) -> Document:
    document.status = "uploaded"
    document.attempts = 0
    document.error_message = None
    db.commit()
    db.refresh(document)
    return document


# 找出卡住的文件：還沒處理完，而且工單在 cutoff 之前就送出去了
# coalesce(a, b)：a 是空的就用 b。工單時間沒記到的（例如記時間那步就失敗了），改看上傳時間
def list_stuck(db: Session, cutoff: datetime) -> list[Document]:
    return (
        db.query(Document)
        .filter(Document.status == "uploaded")
        .filter(func.coalesce(Document.queued_at, Document.uploaded_at) < cutoff)
        .all()
    )


# 何時呼叫：每小時的對帳，拿到「資料庫裡所有文件記錄的 S3 位置」，跟 S3 上實際的檔案比對
# 只查 storage_path 這一欄，不用把整筆文件都查出來
def list_storage_paths(db: Session) -> set[str]:
    return {path for (path,) in db.query(Document.storage_path).all()}


# 何時呼叫：刪除 workspace 之前，先記下底下所有文件的 S3 位置（紀錄刪掉之後就查不到了）
def list_storage_paths_for_workspace(db: Session, workspace_id: int) -> list[str]:
    rows = db.query(Document.storage_path).filter(Document.workspace_id == workspace_id).all()
    return [path for (path,) in rows]
