# 包 boto3 的 SQS 呼叫。純計算，不碰 DB / HTTP，跟 s3_client.py 同一種角色
# SQS 是 API 跟 worker（Lambda）之間傳話的管道：API 放工單進去，Lambda 被自動叫醒來拿

import json

import boto3

from app.core.config import settings

# 憑證的取得方式跟 s3_client.py 一樣：本機用 .env 的金鑰，雲端靠 Role 自動取得
if settings.aws_access_key_id and settings.aws_secret_access_key:
    client = boto3.client(
        "sqs",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
else:
    client = boto3.client("sqs", region_name=settings.aws_region)


# 何時呼叫：document_service 把文件交出去處理時（上傳、手動重試、定期掃描重送）
# 工單內容只放 document_id，worker 拿到後自己去資料庫查其他資料
def send_process_message(document_id: int) -> None:
    # json.dumps：把 dict 轉成 JSON 字串，SQS 的訊息內容只能是字串
    client.send_message(
        QueueUrl=settings.sqs_queue_url,
        MessageBody=json.dumps({"document_id": document_id}),
    )
