# 包 boto3（AWS SDK）的呼叫。純計算，不碰 DB / HTTP，跟 openai_client.py 同一種角色
# 取代原本 document_service.py 直接寫本機硬碟（storage/documents/）那段

import boto3

from app.core.config import settings

# client 只需要建立一次，跟 openai_client.py 的 client 是同樣的道理
# 本機開發：.env 裡有 trace-app-s3 的金鑰，明確傳給 boto3 用
# 部署在 Fargate：沒有這兩個環境變數，不傳，boto3 會自動去問 Task Role 要臨時憑證
if settings.aws_access_key_id and settings.aws_secret_access_key:
    client = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
else:
    client = boto3.client("s3", region_name=settings.aws_region)


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
