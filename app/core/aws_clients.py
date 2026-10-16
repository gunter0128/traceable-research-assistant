# 建 boto3 client 的共用寫法，s3_client.py、sqs_client.py 都用這個，憑證規則只寫一次
# 三種環境拿憑證的方式不一樣：
#   本機開發：.env 裡有 trace-app-s3 的金鑰（沒有 session token），明確交給 boto3
#   Fargate：沒有金鑰環境變數，不傳，boto3 自己去問 Task Role 要臨時憑證
#   Lambda：Lambda 自動把 Role 的臨時憑證放進 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
#           臨時憑證少了 session token 就無效，所以三個要一起傳

import boto3
from botocore.config import Config

from app.core.config import settings


def make_client(service: str):
    options = {"region_name": settings.aws_region}
    if service == "s3":
        # 給瀏覽器直接上傳用的許可（docs/decisions/013）需要這個設定：
        #   signature_version="s3v4"：新版的簽名方式（預設會用已經淘汰的舊版 SigV2）
        #   addressing_style="virtual"：網址是 bucket名稱.s3.ap-southeast-1.amazonaws.com，直接指向新加坡
        #     不設的話會用全球入口 s3.amazonaws.com，瀏覽器會被轉址到新加坡，跨網域的轉址會被瀏覽器擋掉
        options["config"] = Config(signature_version="s3v4", s3={"addressing_style": "virtual"})
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        options.update(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            # 本機是 None，boto3 會當作沒給
            aws_session_token=settings.aws_session_token,
        )
    # **options：把 dict 展開成「名稱=值」的參數傳進去
    return boto3.client(service, **options)
