# 包 boto3（AWS SDK）的呼叫。純計算，不碰 DB / HTTP，跟 openai_client.py 同一種角色
# 取代原本 document_service.py 直接寫本機硬碟（storage/documents/）那段

from datetime import datetime

from app.core.aws_clients import make_client
from app.core.config import settings

# client 只需要建立一次，跟 openai_client.py 的 client 是同樣的道理
# 憑證怎麼拿（本機 / Fargate / Lambda 各不同）統一在 aws_clients.py 處理
client = make_client("s3")


# 何時呼叫：document_service 上傳 PDF 時，把檔案內容存進 S3（取代寫本機硬碟）
def upload_file(key: str, content: bytes) -> None:
    client.put_object(Bucket=settings.s3_bucket_name, Key=key, Body=content)


# 何時呼叫：document_service 要解析 PDF 文字之前，把檔案內容從 S3 抓回來
def download_file(key: str) -> bytes:
    response = client.get_object(Bucket=settings.s3_bucket_name, Key=key)
    return response["Body"].read()


# 何時呼叫：使用者刪除文件時，把 S3 上對應的檔案一併刪除
def delete_file(key: str) -> None:
    client.delete_object(Bucket=settings.s3_bucket_name, Key=key)


# 何時呼叫：每小時的對帳，列出 S3 上某個前綴底下的所有檔案，拿去跟資料庫的紀錄比對
# 回傳 (key, 最後修改時間) 的 list。一次最多回 1000 個，paginator 會自動一頁一頁接著拿完
def list_files(prefix: str) -> list[tuple[str, datetime]]:
    files = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=settings.s3_bucket_name, Prefix=prefix
    ):
        # 一個檔案都沒有的時候，回應裡不會有 Contents 這個欄位，所以用 .get 給預設值
        for item in page.get("Contents", []):
            files.append((item["Key"], item["LastModified"]))
    return files
