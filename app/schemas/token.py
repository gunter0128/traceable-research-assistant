# 登入相關的 API 進出資料形狀

from pydantic import BaseModel


class LoginRequest(BaseModel):
    # POST /auth/login 收這個
    email: str
    password: str


class TokenOut(BaseModel):
    # 登入成功後回這個
    access_token: str
    token_type: str = "bearer"  # 持有這個 token 就給過 不用額外證明