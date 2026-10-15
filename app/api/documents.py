# document 的 endpoint：上傳 / 列 / 查 / 刪，全部要登入，全部檢查 ownership（透過 workspace）
# 這層只做 HTTP：呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core import s3_client
from app.core.db import get_db
from app.models.user import User
from app.schemas.document import DocumentOut
from app.services import document_service, workspace_service

router = APIRouter(tags=["documents"])


def _workspace_error(exc: Exception):
    if isinstance(exc, workspace_service.WorkspaceNotFound):
        raise HTTPException(status_code=404, detail="workspace not found") from None
    raise HTTPException(status_code=403, detail="not your workspace") from None


def _document_error(exc: Exception):
    if isinstance(exc, document_service.DocumentNotFound):
        raise HTTPException(status_code=404, detail="document not found") from None
    raise HTTPException(status_code=403, detail="not your document") from None


@router.post(
    "/workspaces/{workspace_id}/documents", response_model=DocumentOut, status_code=201
)
def upload_document(
    workspace_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return document_service.upload_document(
            db, workspace_id, current_user.id, file
        )
    except (
        workspace_service.WorkspaceNotFound,
        workspace_service.NotWorkspaceOwner,
    ) as exc:
        _workspace_error(exc)
    except document_service.InvalidFileType:
        raise HTTPException(
            status_code=400, detail="only PDF files are allowed"
        ) from None
    # 存檔案時 S3 出問題：問題不在使用者的請求，回 503，跟問答 AI 出問題時一致
    except s3_client.StorageTemporaryError:
        raise HTTPException(
            status_code=503, detail="儲存服務暫時無法使用，請稍後再試"
        ) from None
    except s3_client.StoragePermanentError:
        raise HTTPException(
            status_code=503, detail="系統設定有誤，請聯絡管理員"
        ) from None
    # 413 Content Too Large：HTTP 專門用來表示「你送來的內容太大」的狀態碼
    except document_service.FileTooLarge:
        raise HTTPException(
            status_code=413, detail="file is larger than 20 MB"
        ) from None


@router.get("/workspaces/{workspace_id}/documents", response_model=list[DocumentOut])
def list_documents(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return document_service.list_documents(db, workspace_id, current_user.id)
    except (
        workspace_service.WorkspaceNotFound,
        workspace_service.NotWorkspaceOwner,
    ) as exc:
        _workspace_error(exc)


@router.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return document_service.get_owned_document(db, document_id, current_user.id)
    except (
        document_service.DocumentNotFound,
        document_service.NotDocumentOwner,
    ) as exc:
        _document_error(exc)


# 手動重試處理失敗的文件。不是失敗狀態（處理中 / 已完成）的回 409：這個請求本身沒錯，是跟文件目前的狀態衝突
@router.post("/documents/{document_id}/retry", response_model=DocumentOut)
def retry_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return document_service.retry_document(db, document_id, current_user.id)
    except (
        document_service.DocumentNotFound,
        document_service.NotDocumentOwner,
    ) as exc:
        _document_error(exc)
    except document_service.DocumentNotRetryable:
        raise HTTPException(
            status_code=409, detail="only failed documents can be retried"
        ) from None


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        document_service.delete_document(db, document_id, current_user.id)
    except (
        document_service.DocumentNotFound,
        document_service.NotDocumentOwner,
    ) as exc:
        _document_error(exc)
