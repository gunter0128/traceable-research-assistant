# 使用者相關的 API 進出資料形狀（Pydantic schema）

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserCreate(BaseModel):
    # POST /auth/register 收這個
    email: str
    password: str


class UserOut(BaseModel):
    # 回給客戶端的使用者資料（沒有密碼）
    id: int
    email: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)