# 用量的 endpoint：查自己今天用了多少（docs/decisions/012）
# 這層只做 HTTP：呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.user import User
from app.schemas.usage import UsageTodayOut
from app.services import usage_service

router = APIRouter(tags=["usage"])


# 前端在問答完、文件處理完之後查一次，到上限了就馬上顯示提示
@router.get("/usage/today", response_model=UsageTodayOut)
def get_usage_today(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return usage_service.today(db, current_user.id)
