# auth 的 endpoint：register / login / me
# 這層只做 HTTP：驗 body(schema)、呼叫 service、把結果 / 錯誤翻成 HTTP 回應

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import decode_access_token
from app.models.user import User
from app.repositories import user_repo
from app.schemas.token import LoginRequest, TokenOut
from app.schemas.user import UserCreate, UserOut
from app.services import auth_service

# router = 一組 endpoint。main.py 用 app.include_router(router) 把它插進 app
# 所以到時候 uvicorn app.main:app 伺服器就可以透過 app 查表決定要去跑哪一個 endpoint
# prefix="/auth" → 這檔裡每條路由前面自動加 /auth（/register 實際是 /auth/register）
# tags=["auth"] → /docs 頁面把這些歸在 "auth" 標題下
router = APIRouter(prefix="/auth", tags=["auth"])

# bearer 知道怎麼從 Authorization: Bearer xxx 這個 header 把 token 挖出來
bearer = HTTPBearer()


@router.post("/register", response_model=UserOut, status_code=201)
def register(data: UserCreate, db: Session = Depends(get_db)):
    try:
        return auth_service.register_user(db, data.email, data.password)
    except auth_service.EmailAlreadyRegistered:
        raise HTTPException(
            status_code=409, detail="email already registered"
        ) from None


@router.post("/login", response_model=TokenOut)
def login(data: LoginRequest, db: Session = Depends(get_db)):
    try:
        token = auth_service.login(db, data.email, data.password)
    except auth_service.InvalidCredentials:
        raise HTTPException(
            status_code=401, detail="invalid email or password"
        ) from None
    return {"access_token": token, "token_type": "bearer"}


# FastAPI dependency：從 Authorization header 取 token → 解出 user id → 撈出 User
# 需要登入的 endpoint 加 Depends(get_current_user) 就會自動先跑這個
def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    user_id = decode_access_token(creds.credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid token")
    user = user_repo.get_user_by_id(db, int(user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")
    return user


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user
