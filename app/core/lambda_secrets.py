# Lambda 啟動時，從 Secrets Manager 把密碼讀進環境變數（見 docs/decisions/004）
# Fargate 有內建功能自動做這件事（task-definition.json 的 secrets），Lambda 沒有，所以自己做
# 一定要在 import app.core.config 之前執行：settings 一建立就會去讀環境變數，晚了就讀不到

import os

import boto3

# 環境變數名稱 → Secrets Manager 裡的密碼名稱，跟 deploy/task-definition.json 的 secrets 是同一組
# API 和 worker 讀的是同一份密碼，換密碼時只要改 Secrets Manager 一個地方
SECRETS = {
    "DATABASE_URL": "trace-app/database-url",
    "SECRET_KEY": "trace-app/secret-key",
    "OPENAI_API_KEY": "trace-app/openai-api-key",
}


# 何時呼叫：worker.py 最上面，任何其他 app 的 import 之前
def load_if_in_lambda() -> None:
    # AWS_LAMBDA_FUNCTION_NAME 是 Lambda 自動設定的環境變數，有它才代表真的在 Lambda 上
    # 本機開發、跑測試時沒有它，什麼都不做（照舊從 .env 讀）
    if "AWS_LAMBDA_FUNCTION_NAME" not in os.environ:
        return

    # AWS_REGION 也是 Lambda 自動設定的
    client = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
    for env_name, secret_id in SECRETS.items():
        # SecretString：存成純文字的密碼內容
        os.environ[env_name] = client.get_secret_value(SecretId=secret_id)["SecretString"]
