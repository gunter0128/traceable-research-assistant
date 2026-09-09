# 密碼雜湊 + JWT 產生 / 驗證。純函式 不碰 DB 不碰 FastAPI

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings


# 何時呼叫：使用者註冊時
# 因為 bcrypt 只吃 bytes 所以 encode() 把 str 轉成 bytes
# gensalt() 給每組密碼隨機鹽 讓大家雜湊出來都不一樣(哪怕密碼一樣)
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


# 何時呼叫：使用者登入時
# 把輸入的密碼 根據 hashed 裡的鹽(完整雜湊的前段)重算一次 看結果跟 hashed 一不一樣
def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# 何時呼叫：登入成功後
# token 有三段： base64(header) . base64(payload) . signature
# signature = HMAC-SHA256( base64(header).base64(payload), 密鑰 )
# header: 用什麼演算法簽的 / payload: token 攜帶的實際資訊(一個小 dict)
def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )  # 現在 + 設定的分鐘數 = 到期時間
    payload = {"sub": subject, "exp": expire}  # sub: 這是誰的 token / exp: 何時過期
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


# 何時呼叫：使用者帶著 token 來打「需要登入」的 API 時
# jwt.decode 用密鑰重算 signature 看 token 有沒有被竄改 順便檢查 exp 過期沒
# 壞掉 / 過期會丟 PyJWTError 我們接住回 None
def decode_access_token(token: str) -> str | None:
    """驗證 token，回傳裡面的 subject（user id）；壞掉或過期就回 None。"""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
        return payload.get("sub")
    except jwt.PyJWTError:
        return None
