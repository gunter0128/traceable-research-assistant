# 用量的 endpoint：查自己今天用了多少（docs/decisions/012）
# 這層只做 HTTP：呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends, HTTPException
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


# 問答、上傳、重試共用：用量到上限時回 429 Too Many Requests（HTTP 專門表示「你用太多了」的狀態碼）
# 自己的上限跟全系統的上限分開說：全系統到了不是使用者的問題
def quota_error(exc: usage_service.QuotaExceeded):
    if exc.scope == "system":
        raise HTTPException(status_code=429, detail="系統今日用量已達上限，明天再試") from None
    raise HTTPException(status_code=429, detail="今日用量已達上限，明天再試") from None
