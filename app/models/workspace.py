# workspaces 表 一個工作區一列 屬於一個 user

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import relationship

from app.core.db import Base


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    # 外鍵是寫在"多對一"關係中的"多"那邊
    # 外鍵 + 拿來查東西的欄位通常都會加索引 之後用它查詢比較快
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True) 
    # server_default=func.now() 建立時由資料庫填入當下時間 (UTC)
    # DateTime(timezone=True) 存帶有時區資訊的絕對時間點 (通常都是顯示時才由前端轉當地時區)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(), # 每次改這列時自動把 updated_at 刷成現在
        nullable=False,
    )

    # workspace.owner 拿到擁有這個 workspace 的 User 物件
    # back_populates="workspaces" = User 那邊對回來的屬性名 (User 有一條 workspaces relationship 指回這裡)
    # "User" 用 class 名 字串寫法避免 import 順序問題
    owner = relationship("User", back_populates="workspaces") 
    documents = relationship(
        "Document", back_populates="workspace", cascade="all, delete-orphan" # 刪掉一個 Workspace 時 它底下的 Document 會一起被刪
    )
