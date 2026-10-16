# Lambda 的進入點：雲端上背景處理文件的程式（worker）從這裡開始跑
# 這裡只負責「把 Lambda 給的資料拆開、開關資料庫連線」，真正的處理邏輯全在 document_service
# 跟 api/ 底下的檔案是同一種角色，只是 api/ 接的是 HTTP 請求，這裡接的是 Lambda 的觸發

import json

from app.core import lambda_secrets

# 先把密碼讀進環境變數，下面的 import 才會建立 settings（順序不能換：import 寫在程式碼後面是刻意的）
lambda_secrets.load_if_in_lambda()

import logging
import time

from app.core import log, sqs_client
from app.core.db import SessionLocal
from app.repositories import document_repo
from app.services import document_service

# 寫 log 的設定（docs/decisions/010），跟 API 用同一套
log.setup()

# OpenAI 暫時連不上時，工單延後多久（秒）再處理（見 docs/decisions/005）
RETRY_DELAY_SECONDS = 120


# SQS 有新工單時，Lambda 會呼叫這個函式
# event：Lambda 傳進來的資料，SQS 觸發時長這樣 {"Records": [{"body": "工單內容"}, ...]}
# context：Lambda 的執行資訊，這裡用它的 aws_request_id（Lambda 給這次執行的編號，對得上 Lambda 自己的 START/END 紀錄）
def handle_process_message(event, context):
    for record in event["Records"]:
        # json.loads：跟 sqs_client 裡的 json.dumps 相反，把 JSON 字串轉回 dict
        body = json.loads(record["body"])
        document_id = body["document_id"]
        # 沿用 API 那邊的請求編號，這張工單之後的 log 都會帶著它和文件編號
        # .get：舊版的工單沒有 request_id，拿不到就是 None
        log.bind(request_id=body.get("request_id"), document_id=document_id)
        # receive_count：這張工單第幾次被拿出來處理，到 3 次還沒成功就會進 DLQ
        log.event(
            "message_received",
            lambda_request_id=getattr(context, "aws_request_id", None),
            receive_count=int(record.get("attributes", {}).get("ApproximateReceiveCount", 0)),
        )
        # 不是 HTTP 請求，沒有 Depends(get_db) 可以用，自己開 session、自己關
        db = SessionLocal()
        try:
            document = document_repo.get_by_id(db, document_id)
            # 工單送出後文件被刪掉了 → 什麼都不做
            # 重複的工單（已經處理完、已經失敗、別人正在處理）不用在這裡擋：process_document 搶不到租約就會跳過
            if document is None:
                log.event("message_skipped", reason="document_deleted")
                continue
            document_service.process_document(db, document)
        except document_service.RetryLater:
            # 叫 SQS 2 分鐘後再把這張工單送回來，然後把錯誤往外丟，讓這次執行算失敗
            # Lambda 執行失敗時不會刪掉工單，工單才會在 2 分鐘後重新出現
            sqs_client.delay_message(record["receiptHandle"], RETRY_DELAY_SECONDS)
            raise
        # 其他沒接住的錯誤（process_document 自己會接住處理中的錯誤，會走到這裡的通常是一開始就連不上資料庫）
        # 記下錯誤堆疊再往外丟：工單會被 SQS 重送，3 次都這樣就進 DLQ
        except Exception:
            log.event("message_crashed", logging.ERROR, exc_info=True)
            raise
        finally:
            db.close()


# 排程每小時呼叫一次：把超過處理期限的文件標逾時，再對帳清掉 S3 上的孤兒檔案
# 標逾時放前面：對帳就算出錯，也不影響使用者看得到的文件狀態
def handle_sweep(event, context):
    log.bind()
    start = time.perf_counter()
    db = SessionLocal()
    try:
        expired = document_service.expire_overdue_documents(db)
        orphans_deleted = document_service.delete_orphan_files(db)
    except Exception:
        log.event("sweep_crashed", logging.ERROR, exc_info=True)
        raise
    finally:
        db.close()
    log.event("sweep", expired=expired, orphans_deleted=orphans_deleted, duration_ms=log.elapsed_ms(start))
    # 回傳值會出現在 Lambda 的執行紀錄裡，方便事後查這次掃描做了什麼
    return {"expired": expired, "orphans_deleted": orphans_deleted}
