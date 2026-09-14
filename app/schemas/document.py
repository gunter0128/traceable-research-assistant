# document 相關的 API 進出資料形狀
# 上傳沒有專屬的輸入 schema：FastAPI 的 UploadFile 直接當參數收檔案，不是走 JSON body

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentOut(BaseModel):
    # 回給客戶端的 document 資料
    id: int
    workspace_id: int
    filename: str
    status: str
    uploaded_at: datetime
    processed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)
