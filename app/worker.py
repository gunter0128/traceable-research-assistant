# Lambda 的進入點：雲端上背景處理文件的程式（worker）從這裡開始跑
# 這裡只負責「把 Lambda 給的資料拆開、開關資料庫連線」，真正的處理邏輯全在 document_service
# 跟 api/ 底下的檔案是同一種角色，只是 api/ 接的是 HTTP 請求，這裡接的是 Lambda 的觸發

import json

from app.core import lambda_secrets

# 先把密碼讀進環境變數，下面的 import 才會建立 settings（順序不能換：import 寫在程式碼後面是刻意的）
lambda_secrets.load_if_in_lambda()

from app.core import sqs_client
from app.core.db import SessionLocal
from app.repositories import document_repo
from app.services import document_service

# OpenAI 暫時連不上時，工單延後多久（秒）再處理（見 docs/decisions/005）
RETRY_DELAY_SECONDS = 120


# SQS 有新工單時，Lambda 會呼叫這個函式
# event：Lambda 傳進來的資料，SQS 觸發時長這樣 {"Records": [{"body": "工單內容"}, ...]}
# context：Lambda 的執行資訊（剩多少時間等），這裡用不到，但 Lambda 規定函式要收這兩個參數
def handle_process_message(event, context):
    for record in event["Records"]:
        # json.loads：跟 sqs_client 裡的 json.dumps 相反，把 JSON 字串轉回 dict
        document_id = json.loads(record["body"])["document_id"]
        # 不是 HTTP 請求，沒有 Depends(get_db) 可以用，自己開 session、自己關
        db = SessionLocal()
        try:
            document = document_repo.get_by_id(db, document_id)
            # 工單送出後文件被刪掉了，或是重複的工單（已經處理完、已經失敗）→ 什麼都不做
            if document is None or document.status != "uploaded":
                continue
            document_service.process_document(db, document)
        except document_service.RetryLater:
            # 叫 SQS 2 分鐘後再把這張工單送回來，然後把錯誤往外丟，讓這次執行算失敗
            # Lambda 執行失敗時不會刪掉工單，工單才會在 2 分鐘後重新出現
            sqs_client.delay_message(record["receiptHandle"], RETRY_DELAY_SECONDS)
            raise
        finally:
            db.close()


# 排程每小時呼叫一次：把超過處理期限的文件標逾時，再對帳清掉 S3 上的孤兒檔案
# 標逾時放前面：對帳就算出錯，也不影響使用者看得到的文件狀態
def handle_sweep(event, context):
    db = SessionLocal()
    try:
        expired = document_service.expire_overdue_documents(db)
        orphans_deleted = document_service.delete_orphan_files(db)
    finally:
        db.close()
    # 回傳值會出現在 Lambda 的執行紀錄裡，方便事後查這次掃描做了什麼
    return {"expired": expired, "orphans_deleted": orphans_deleted}
