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


# 何時呼叫：worker 處理時 OpenAI 暫時連不上，要這張工單過一陣子再出現
# receipt_handle：Lambda 拿到工單時 SQS 給的「收據」，憑它才能改這一張工單
# visibility timeout 改成 seconds 秒：這段時間工單隱形，時間到了重新出現，再被 Lambda 拿去處理
def delay_message(receipt_handle: str, seconds: int) -> None:
    client.change_message_visibility(
        QueueUrl=settings.sqs_queue_url,
        ReceiptHandle=receipt_handle,
        VisibilityTimeout=seconds,
    )
