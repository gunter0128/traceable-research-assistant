# workspace 的 endpoint：CRUD，全部要登入，全部檢查 ownership
# 這層只做 HTTP：呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.api.errors import WORKSPACE_ERRORS, workspace_error
from app.core.db import get_db
from app.models.user import User
from app.schemas.workspace import WorkspaceCreate, WorkspaceOut, WorkspaceUpdate
from app.services import workspace_service

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceOut, status_code=201)
def create_workspace(
    data: WorkspaceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return workspace_service.create_workspace(db, current_user.id, data.name)


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return workspace_service.list_workspaces(db, current_user.id)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return workspace_service.get_owned_workspace(db, workspace_id, current_user.id)
    # WORKSPACE_ERRORS 是兩種錯誤組成的 tuple：一次接住兩種，不管丟出來的是哪一個，都綁到同一個變數 exc
    # 再交給 workspace_error 用 isinstance 判斷是哪一種，回 404 還是 403（api/errors.py）
    except WORKSPACE_ERRORS as exc:
        workspace_error(exc)


@router.put("/{workspace_id}", response_model=WorkspaceOut)
def update_workspace(
    workspace_id: int,
    data: WorkspaceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return workspace_service.update_workspace(
            db, workspace_id, current_user.id, data.name
        )
    except WORKSPACE_ERRORS as exc:
        workspace_error(exc)


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        workspace_service.delete_workspace(db, workspace_id, current_user.id)
    except WORKSPACE_ERRORS as exc:
        workspace_error(exc)
