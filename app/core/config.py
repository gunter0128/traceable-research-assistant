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


settings = Settings()
