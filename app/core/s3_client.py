# 包 boto3（AWS SDK）的呼叫。純計算，不碰 DB / HTTP，跟 openai_client.py 同一種角色
# 取代原本 document_service.py 直接寫本機硬碟（storage/documents/）那段
# 跟 openai_client.py 一樣，也負責把 S3 的錯誤分類，分類完怎麼處理由呼叫的 service 決定

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

from app.core.aws_clients import make_client
from app.core.config import settings

# client 只需要建立一次，跟 openai_client.py 的 client 是同樣的道理
# 憑證怎麼拿（本機 / Fargate / Lambda 各不同）統一在 aws_clients.py 處理
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


# 何時用：包在每個 S3 呼叫外面，把 boto3 的錯誤翻譯成上面三種
# @contextmanager：讓這個函式可以寫成 with _translate_errors(): 的形式，
# yield 的位置就是 with 區塊裡的程式碼執行的地方，那裡丟出來的錯誤會被下面的 except 接住
@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    # 有收到 S3 的回應，看錯誤代碼和狀態碼
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        status = exc.response["ResponseMetadata"]["HTTPStatusCode"]
        if code == "NoSuchKey":
            raise FileMissing(code) from exc
        # SlowDown：S3 叫你慢一點（跟 OpenAI 的速率限制一樣）；5xx：S3 自己的問題
        if code in ("SlowDown", "RequestTimeout") or status >= 500:
            raise StorageTemporaryError(code) from exc
        raise StoragePermanentError(code) from exc
    # 根本沒收到回應：連不上、超時
    except BotoCoreError as exc:
        raise StorageTemporaryError(str(exc)) from exc


# 何時呼叫：document_service 上傳 PDF 時，把檔案內容存進 S3（取代寫本機硬碟）
def upload_file(key: str, content: bytes) -> None:
    with _translate_errors():
        client.put_object(Bucket=settings.s3_bucket_name, Key=key, Body=content)


# 何時呼叫：document_service 要解析 PDF 文字之前，把檔案內容從 S3 抓回來
def download_file(key: str) -> bytes:
    with _translate_errors():
        response = client.get_object(Bucket=settings.s3_bucket_name, Key=key)
        return response["Body"].read()


# 何時呼叫：使用者刪除文件時，把 S3 上對應的檔案一併刪除
# 刪一個本來就不存在的檔案，S3 不會報錯，所以這裡不會丟 FileMissing
def delete_file(key: str) -> None:
    with _translate_errors():
        client.delete_object(Bucket=settings.s3_bucket_name, Key=key)


# 何時呼叫：每小時的對帳，列出 S3 上某個前綴底下的所有檔案，拿去跟資料庫的紀錄比對
# 回傳 (key, 最後修改時間) 的 list。一次最多回 1000 個，paginator 會自動一頁一頁接著拿完
def list_files(prefix: str) -> list[tuple[str, datetime]]:
    files = []
    with _translate_errors():
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=settings.s3_bucket_name, Prefix=prefix
        ):
            # 一個檔案都沒有的時候，回應裡不會有 Contents 這個欄位，所以用 .get 給預設值
            for item in page.get("Contents", []):
                files.append((item["Key"], item["LastModified"]))
    return files
