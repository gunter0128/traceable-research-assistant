# document 的 endpoint：上傳 / 列 / 查 / 刪，全部要登入，全部檢查 ownership（透過 workspace）
# 這層只做 HTTP：呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
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
