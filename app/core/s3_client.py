# 包 boto3（AWS SDK）的 S3 呼叫，不碰資料庫，跟 openai_client.py 同一種角色
# PDF 不經過 API：瀏覽器拿這裡簽的上傳許可直接傳到 S3（docs/decisions/013），worker 處理時再從 S3 下載
# 跟 openai_client.py 一樣，也負責把 S3 的錯誤分類，分類完怎麼處理由呼叫的 service 決定
# 每個函式的寫法也一樣：try 呼叫 boto3 → except 把 boto3 的錯誤翻譯成下面幾種（_classify）

from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

from app.core.aws_clients import make_client
from app.core.config import settings

# client 只需要建立一次，跟 openai_client.py 的 client 是同樣的道理
# 憑證怎麼拿（本機 / Lambda 不同）統一在 aws_clients.py 處理
client = make_client("s3")


# 所有 S3 錯誤的共同父類別：只想「不管哪種 S3 錯誤都接住」的地方（例如刪檔案失敗也沒關係）接這個就好
class StorageError(Exception):
    pass


# 要找的檔案不存在：重試永遠不會成功
class FileMissing(StorageError):
    pass


# 等一下再試通常會好：網路斷線、S3 忙不過來、S3 伺服器錯誤
class StorageTemporaryError(StorageError):
    pass


# 等多久都不會好、需要人處理：沒權限、bucket 不存在、憑證錯誤
class StoragePermanentError(StorageError):
    pass


# 把 boto3 丟出來的錯誤，翻譯成上面三種之一。每個函式都這樣用：
#   except (ClientError, BotoCoreError) as exc:
#       raise _classify(exc) from exc
# 錯誤訊息只放 S3 的錯誤代碼（例如 AccessDenied），不放檔案內容
def _classify(exc: ClientError | BotoCoreError) -> StorageError:
    # 根本沒收到回應：連不上、超時
    if isinstance(exc, BotoCoreError):
        return StorageTemporaryError(str(exc))
    # 有收到 S3 的回應，看錯誤代碼和狀態碼
    code = exc.response["Error"]["Code"]
    status = exc.response["ResponseMetadata"]["HTTPStatusCode"]
    # 檔案不存在：get_object 回 NoSuchKey；head_object（file_size）的回應沒有內容，錯誤代碼只有 "404"
    if code in ("NoSuchKey", "404"):
        return FileMissing(code)
    # SlowDown：S3 叫你慢一點（跟 OpenAI 的速率限制一樣）；5xx：S3 自己的問題
    if code in ("SlowDown", "RequestTimeout") or status >= 500:
        return StorageTemporaryError(code)
    # 其他（沒權限、bucket 不存在、憑證錯誤……）
    return StoragePermanentError(code)


# 何時呼叫：document_service 要解析 PDF 文字之前，把檔案內容從 S3 抓回來
def download_file(key: str) -> bytes:
    try:
        response = client.get_object(Bucket=settings.s3_bucket_name, Key=key)
        return response["Body"].read()  # 讀內容時也可能斷線，所以也放在 try 裡面
    except (ClientError, BotoCoreError) as exc:
        raise _classify(exc) from exc


# 何時呼叫：使用者刪除文件時，把 S3 上對應的檔案一併刪除
# 刪一個本來就不存在的檔案，S3 不會報錯，所以這裡不會丟 FileMissing
def delete_file(key: str) -> None:
    try:
        client.delete_object(Bucket=settings.s3_bucket_name, Key=key)
    except (ClientError, BotoCoreError) as exc:
        raise _classify(exc) from exc


# 何時呼叫：每小時的對帳，列出 S3 上某個前綴底下的所有檔案，拿去跟資料庫的紀錄比對
# 回傳 (key, 最後修改時間) 的 list。一次最多回 1000 個，paginator 會自動一頁一頁接著拿完
def list_files(prefix: str) -> list[tuple[str, datetime]]:
    files = []
    try:
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=settings.s3_bucket_name, Prefix=prefix
        ):
            # 一個檔案都沒有的時候，回應裡不會有 Contents 這個欄位，所以用 .get 給預設值
            for item in page.get("Contents", []):
                files.append((item["Key"], item["LastModified"]))
    except (ClientError, BotoCoreError) as exc:
        raise _classify(exc) from exc
    return files


# 何時呼叫：使用者要上傳檔案時，給瀏覽器一張「上傳許可」，讓它直接把檔案傳到 S3，不經過 API（docs/decisions/013）
# 回傳 {"url": 要傳去的網址, "fields": 要一起送出的表單欄位}，瀏覽器用 multipart 表單 POST 過去
# generate_presigned_post：用我們的權限「預先簽好」一張許可，只在本機計算，不會連到 S3
#   許可裡寫死了：只能存到 key 這個位置、檔案大小在 1 ~ max_bytes 之間、expires_seconds 秒內有效
#   條件是 S3 自己檢查的，就算有人改了前端程式，也傳不了超過上限的檔案、或存到別的位置
def create_upload_form(key: str, max_bytes: int, expires_seconds: int) -> dict:
    try:
        form = client.generate_presigned_post(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Conditions=[["content-length-range", 1, max_bytes]],
            ExpiresIn=expires_seconds,
        )
    # 只在本機計算，不會連到 S3；會出錯的情況是拿不到憑證（例如設定漏了），一樣翻譯成我們的錯誤
    except (ClientError, BotoCoreError) as exc:
        raise _classify(exc) from exc
    # 地端版（docs/decisions/045）：上傳網址換成瀏覽器連得到的路徑，由 nginx 轉給儲存服務
    # 許可的簽名只簽表單裡的條件（policy），沒有簽網址，所以換掉網址不影響儲存服務的檢查
    if settings.s3_public_url:
        form["url"] = f"{settings.s3_public_url}/{settings.s3_bucket_name}"
    return form


# 何時呼叫：瀏覽器說「傳好了」之後，確認檔案真的在 S3 上，順便拿到實際大小
# head_object：只問檔案的資訊（大小、時間），不下載內容。檔案不存在時錯誤代碼是 "404"（不是 NoSuchKey），_classify 會認成 FileMissing
def file_size(key: str) -> int:
    try:
        return client.head_object(Bucket=settings.s3_bucket_name, Key=key)["ContentLength"]
    except (ClientError, BotoCoreError) as exc:
        raise _classify(exc) from exc
