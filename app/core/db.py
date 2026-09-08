# SQLAlchemy 資料庫設定

# 用 settings.database_url 建 engine 和 session 工廠
# 定義所有 model 要繼承的 Base
# 還有 get_db 每個請求給一個 session 結束時關掉


from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.core.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True) # pool_pre_ping 連線前先測 ping
SessionLocal = sessionmaker(bind=engine, autoflush=False) # autoflush 查詢前要不要自動把暫存的變更送進 DB (否 自己用commit())


Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
