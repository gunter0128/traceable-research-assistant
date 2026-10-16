# API 在 Lambda 上的進入點（docs/decisions/013）
# Lambda 收到的是一包 JSON 格式的「事件」，不是一般的 HTTP 請求；
# Mangum 負責把事件轉成 FastAPI 看得懂的請求，再把 FastAPI 的回應轉回 Lambda 要的格式
# 所以 FastAPI 的程式碼完全不用改，本機照樣用 uvicorn 跑，雲端用這個檔案

from app.core import lambda_secrets

# 跟 worker.py 一樣：先把密碼讀進環境變數，下面的 import 才會建立 settings（順序不能換）
lambda_secrets.load_if_in_lambda()

from mangum import Mangum

from app.main import app

# lifespan="off"：我們的 app 沒有「啟動時 / 關閉時要做的事」，不用讓 Mangum 去處理
handler = Mangum(app, lifespan="off")
