# document 相關的 API 進出資料形狀

from datetime import datetime

from pydantic import BaseModel, ConfigDict


# 上傳第 1 步：POST /workspaces/{id}/uploads 收這個（docs/decisions/013）
class UploadStart(BaseModel):
    filename: str
    size_bytes: int


# 上傳第 1 步的回應：瀏覽器拿 url + fields 直接把檔案傳到 S3，傳好之後帶著 key 通知 API
class UploadTicketOut(BaseModel):
    url: str
    fields: dict[str, str]
    key: str


# 上傳第 3 步：POST /workspaces/{id}/documents 收這個，告訴 API「key 這個檔案傳好了」
class UploadFinish(BaseModel):
    key: str
    filename: str


class DocumentOut(BaseModel):
    # 回給客戶端的 document 資料
    id: int
    workspace_id: int
    filename: str
    status: str
    uploaded_at: datetime
    processed_at: datetime | None
    error_message: str | None
    progress_done: int | None
    progress_total: int | None

    model_config = ConfigDict(from_attributes=True)
