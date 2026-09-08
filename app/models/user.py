# users 表 一個帳號一列

from sqlalchemy import Column, DateTime, Integer, String, func
from sqlalchemy.orm import relationship

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False) 
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    workspaces = relationship(
        "Workspace", back_populates="owner", cascade="all, delete-orphan"
    )
