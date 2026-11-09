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
    # status：uploaded 排隊中、processing 處理中、processed 完成、failed 失敗、cancelled 已取消（以前取消的，可以重試）
    # 以前還有處理進度（progress_done / progress_total），docs/decisions/046 拿掉了：處理中只顯示「處理中」
    id: int
    workspace_id: int
    filename: str
    status: str
    uploaded_at: datetime
    processed_at: datetime | None
    error_message: str | None

    model_config = ConfigDict(from_attributes=True)
