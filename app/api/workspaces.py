# workspace 的 endpoint：CRUD，全部要登入，全部檢查 ownership
# 這層只做 HTTP：呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.user import User
from app.schemas.workspace import WorkspaceCreate, WorkspaceOut, WorkspaceUpdate
from app.services import workspace_service

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _not_found_or_forbidden(exc: Exception):
    # get/update/delete 三個 endpoint 共用同一組錯誤翻譯，抽出來避免重複三次
    # isinstance(物件, 類別) 問「這個物件是不是這個類別做出來的」回傳 True/False
    # exc 可能是 WorkspaceNotFound 或 NotWorkspaceOwner 其中一個 這裡問清楚是哪一個
    # 才知道要回 404 (根本沒這個 workspace) 還是 403 (有 但不是你的)
    if isinstance(exc, workspace_service.WorkspaceNotFound):
        raise HTTPException(status_code=404, detail="workspace not found") from None
    raise HTTPException(status_code=403, detail="not your workspace") from None


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
    # except (A, B) as exc 語法：一次接住兩種例外類型 不管丟出來的是哪一個 都綁到同一個變數 exc
    # 這裡不像 auth.py 分開兩個 except 是因為這三個 endpoint 都要接同一組錯誤 分開寫要複製三次
    # 統一接住後丟給 _not_found_or_forbidden 在裡面用 isinstance 判斷到底是哪一種
    except (workspace_service.WorkspaceNotFound, workspace_service.NotWorkspaceOwner) as exc:
        _not_found_or_forbidden(exc)


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
    # 同上（get_workspace）：一次接兩種例外，丟給共用函式判斷是哪一種
    except (workspace_service.WorkspaceNotFound, workspace_service.NotWorkspaceOwner) as exc:
        _not_found_or_forbidden(exc)


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        workspace_service.delete_workspace(db, workspace_id, current_user.id)
    # 同上（get_workspace）：一次接兩種例外，丟給共用函式判斷是哪一種
    except (workspace_service.WorkspaceNotFound, workspace_service.NotWorkspaceOwner) as exc:
        _not_found_or_forbidden(exc)
