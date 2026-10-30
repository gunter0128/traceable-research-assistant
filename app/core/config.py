# app 的設定檔
# 從 .env / 環境變數讀設定（資料庫網址、JWT 密鑰、token 選項）
# 變成一個 settings 物件 讓其他檔案 import 它來用


from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings): # BaseModel的子類別 不用給他明確的資料 它自己去環境變數（和 .env）把值抓回來
    model_config = SettingsConfigDict(env_file=".env", extra="ignore") # 去讀一個叫 .env 的檔 有任何對不到欄位就忽略(增加容錯)

    database_url: str
    secret_key: str
    access_token_expire_minutes: int = 30
    algorithm: str = "HS256"
    # 企業內部版轉向量、回答都接本機的模型，用不到 OpenAI，所以是選填
    openai_api_key: str | None = None

    # ---------- 轉向量、回答要接哪個服務（docs/decisions/037）----------
    # 不設定 base_url 就用 OpenAI（公開展示版）
    # 企業內部版設成公司內部用 vLLM 架的模型服務：呼叫方式跟 OpenAI 一樣，只是網址不同，資料不會送到外部
    embedding_base_url: str | None = None
    embedding_model: str = "text-embedding-3-small"
    # 向量有幾個數字，資料庫的向量欄位要跟它一樣（text-embedding-3-small 是 1536、Qwen3-Embedding-0.6B 是 1024）
    embedding_dimension: int = 1536
    # 有些模型轉「問題」時要在前面加固定的說明，轉文件段落時不用加（Qwen3-Embedding 要加任務說明，docs/decisions/035）
    embedding_query_prefix: str = ""
    answer_base_url: str | None = None
    answer_model: str = "gpt-4o"
    # 送給回答服務的額外參數（環境變數裡寫 JSON），例如 Qwen 建議的抽字設定、關掉思考模式（docs/decisions/036）
    answer_extra_body: dict | None = None
    # 回答每次最多等幾秒：本機的模型比 OpenAI 慢，長的回答會超過 15 秒
    answer_timeout: float = 15

    # 本機開發用 trace-app-s3 的金鑰（.env 裡有值）；Lambda 上由 AWS 自動放進環境變數（見 core/aws_clients.py）
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    # Lambda 上才會有：臨時憑證的一部分，跟上面兩個一起用（見 core/aws_clients.py）
    aws_session_token: str | None = None
    aws_region: str
    s3_bucket_name: str
    # ---------- 地端版的檔案儲存（docs/decisions/045）----------
    # 不設定就是 AWS 的 S3（公開展示版）。地端版存在自己架、跟 S3 用法一樣的儲存服務（SeaweedFS），只是網址不同
    # s3_endpoint_url：API 連儲存服務用的網址（docker-compose 裡是 http://storage:8333）
    # s3_public_url：瀏覽器上傳時用的網址。瀏覽器連不到容器網路裡的 storage:8333，
    #   改成網頁同一個網址底下的路徑（/storage），由 nginx 轉給儲存服務
    s3_endpoint_url: str | None = None
    s3_public_url: str | None = None
    # 選填：雲端有設定 → 上傳後送工單到 SQS，交給 Lambda 背景處理
    # 本機沒設定 → 上傳後直接在同一個 request 裡處理（見 docs/decisions/003）
    sqs_queue_url: str | None = None


settings = Settings()
