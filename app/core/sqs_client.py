# 包 boto3 的 SQS 呼叫。純計算，不碰 DB / HTTP，跟 s3_client.py 同一種角色
# SQS 是 API 跟 worker（Lambda）之間傳話的管道：API 放工單進去，Lambda 被自動叫醒來拿

import json

from app.core.aws_clients import make_client
from app.core.config import settings

# 憑證怎麼拿統一在 aws_clients.py 處理，跟 s3_client.py 一樣
client = make_client("sqs")


# 何時呼叫：document_service 把文件交出去處理時（上傳、手動重試、定期掃描重送）
# 工單內容只放 document_id，worker 拿到後自己去資料庫查其他資料
def send_process_message(document_id: int) -> None:
    # json.dumps：把 dict 轉成 JSON 字串，SQS 的訊息內容只能是字串
    client.send_message(
        QueueUrl=settings.sqs_queue_url,
        MessageBody=json.dumps({"document_id": document_id}),
    )
