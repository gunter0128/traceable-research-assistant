# 建 boto3 client 的共用寫法，s3_client.py、sqs_client.py 都用這個，憑證規則只寫一次
# 三種環境拿憑證的方式不一樣：
#   本機開發：.env 裡有 trace-app-s3 的金鑰（沒有 session token），明確交給 boto3
#   Fargate：沒有金鑰環境變數，不傳，boto3 自己去問 Task Role 要臨時憑證
#   Lambda：Lambda 自動把 Role 的臨時憑證放進 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
#           臨時憑證少了 session token 就無效，所以三個要一起傳

import boto3

from app.core.config import settings


def make_client(service: str):
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        return boto3.client(
            service,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            # 本機是 None，boto3 會當作沒給
            aws_session_token=settings.aws_session_token,
            region_name=settings.aws_region,
        )
    return boto3.client(service, region_name=settings.aws_region)
