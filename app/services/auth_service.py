# 註冊 / 登入的流程和規則。呼叫 user_repo 和 security 兜起來
# 不寫 SQL 不碰 HTTP

from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.repositories import user_repo


# 自己造了一個錯誤標籤 可以知道/處理特定錯誤類型(單純 Exception 無法分辨)
class EmailAlreadyRegistered(Exception):
    pass


class InvalidCredentials(Exception):
    pass


def register_user(db: Session, email: str, password: str) -> User:
    # 規則：email 不能重複
    if user_repo.get_user_by_email(db, email) is not None:
        raise EmailAlreadyRegistered
    hashed = hash_password(password)
    return user_repo.create_user(db, email, hashed)


def login(db: Session, email: str, password: str) -> str:
    # 規則：email 要存在 且 密碼要對，回傳一個 access token
    user = user_repo.get_user_by_email(db, email)
    if user is None or not verify_password(password, user.password_hash):
        raise InvalidCredentials
    return create_access_token(str(user.id))
