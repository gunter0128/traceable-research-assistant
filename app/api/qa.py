# qa 的 endpoint：對 workspace 裡的文件提問
# 這層只做 HTTP：收請求、呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.user import User
from app.schemas.qa import AskRequest, AskResponse
from app.services import qa_service, workspace_service

router = APIRouter(tags=["qa"])


def _workspace_error(exc: Exception):
    if isinstance(exc, workspace_service.WorkspaceNotFound):
        raise HTTPException(status_code=404, detail="workspace not found") from None
    raise HTTPException(status_code=403, detail="not your workspace") from None


@router.post("/workspaces/{workspace_id}/ask", response_model=AskResponse)
def ask(
    workspace_id: int,
    data: AskRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return qa_service.ask(db, workspace_id, current_user.id, data.question)
    except (
        workspace_service.WorkspaceNotFound,
        workspace_service.NotWorkspaceOwner,
    ) as exc:
        _workspace_error(exc)
