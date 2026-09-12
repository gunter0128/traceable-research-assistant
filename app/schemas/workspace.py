# workspace 相關的 API 進出資料形狀

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WorkspaceCreate(BaseModel):
    # POST /workspaces 收這個
    name: str


class WorkspaceUpdate(BaseModel):
    # PUT /workspaces/{id} 收這個
    name: str


class WorkspaceOut(BaseModel):
    # 回給客戶端的 workspace 資料
    id: int
    name: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
