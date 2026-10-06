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
    openai_api_key: str
    # 這兩個改成選填：本機開發用 trace-app-s3 的金鑰（.env 裡有值）；
    # 部署在 Fargate 上時不會有這兩個環境變數，改靠 Task Role 自動取得憑證
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str
    s3_bucket_name: str


settings = Settings()
