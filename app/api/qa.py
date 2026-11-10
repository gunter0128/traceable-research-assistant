# qa 的 endpoint：對 workspace 裡的文件提問
# 這層只做 HTTP：收請求、呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.api.errors import WORKSPACE_ERRORS, quota_error, workspace_error
from app.core import openai_client
from app.core.db import get_db
from app.models.user import User
from app.schemas.qa import AskRequest, AskResponse
from app.services import qa_service, usage_service

router = APIRouter(tags=["qa"])


@router.post("/workspaces/{workspace_id}/ask", response_model=AskResponse)
def ask(
    workspace_id: int,
    data: AskRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return qa_service.ask(db, workspace_id, current_user.id, data.question)
    except WORKSPACE_ERRORS as exc:
        workspace_error(exc)
    except usage_service.QuotaExceeded as exc:
        quota_error(exc)
    # OpenAI 出問題（qa_service 已經記好 log）：503 Service Unavailable，「服務暫時無法提供」，問題不在使用者的請求本身
    # 跟上傳時 S3 出問題一樣（app/api/documents.py），直接接 client 的錯誤，暫時性、永久性給不同的訊息
    # from None：不把 OpenAI 原本的錯誤接在後面，回應裡只有給使用者看的訊息
    except openai_client.OpenAITemporaryError:
        raise HTTPException(
            status_code=503, detail="AI 服務暫時無法使用，請稍後再試"
        ) from None
    except openai_client.OpenAIPermanentError:
        raise HTTPException(
            status_code=503, detail="系統設定有誤，請聯絡管理員"
        ) from None
