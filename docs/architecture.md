# 架構筆記

> 目前只涵蓋「已完成的部分」：地基（core / models / Alembic）+ auth 功能。
> V0 後續（Workspace / Document）做完再補。

---

## 分工圖

```
              Client（瀏覽器 / /docs）
                    │ HTTP 請求
                    ▼
                 main.py ── 開機時把各功能的 router 組進 app
                    │
                    ▼
        ┌───────────────────────────────┐
        │ api/auth.py                    │  收 HTTP、回 HTTP
        │ register / login / me         │
        └───────┬───────────────┬───────┘
                │ 呼叫          │ 驗進出格式
                ▼               ▼
        ┌────────────────┐   schemas/
        │ auth_service.py│   UserCreate / UserOut / LoginRequest / TokenOut
        │ 規則 + 步驟     │
        └───────┬────────┘
                │ 呼叫
                ▼
        ┌────────────────┐
        │ user_repo.py   │   跟 DB 講話（db.query(User)...，SQL 只出現在這）
        └───────┬────────┘
                │ 用
                ▼
             models/       資料表定義（User / Workspace / Document）
                │
                ▼
             Neon（PostgreSQL）


  core/（放旁邊，誰都能用）
     ├ config.py    把 .env 讀成 settings 物件
     ├ db.py        建連線、發 session、提供 Base
     └ security.py  雜湊密碼、產生 / 驗證 JWT（純計算）

  額外連線：
   • auth_service.py     ─呼叫→  security.py（雜湊、發 token）
   • api/auth.py         ─呼叫→  security.py + user_repo（get_current_user 驗 token）
   • db.py / security.py ─要設定值→  config.py  ─讀→  .env

  alembic/ ── 讀 models/ 的表定義 → 生 migration → 套用到 Neon
```

**怎麼讀**

- 實線箭頭 = 「呼叫 / 用到」。主流向 `api → service → repository → model → db → Neon` 往下單向。
- `core/` 三個檔在旁邊，是共用工具，任何層都可能用。
- `schemas/` 掛在 api 旁邊（驗進出格式），`alembic/` 掛在 models 旁邊（管表結構版本），都不在主流程裡。
- 方向是單向的：`repository` 不會回頭呼叫 `service`，`service` 不會呼叫 `api`。

---

## 分工表

| 檔 | 職責 |
|---|---|
| `main.py` | 開機時把各功能的 router 組進 `app`；`app` = 執行時的路由分派表 |
| `api/auth.py` | HTTP 這一層：收請求、用 schema 驗 body、呼叫 service、把結果 / 錯誤翻成 HTTP 狀態碼 |
| `services/auth_service.py` | 業務規則和流程：email 不能重複、密碼先雜湊、登入成功發 token。呼叫 repo + security，自己不寫 SQL、不碰 HTTP |
| `repositories/user_repo.py` | 跟 DB 講話：`get_user_by_email` / `get_user_by_id` / `create_user`。`db.query(...)` 只出現在這 |
| `models/` | 資料表長怎樣：欄位、型別、外鍵、索引（`User` / `Workspace` / `Document`） |
| `schemas/` | API 進出的 JSON 長怎樣（跟 model 分開，因為 DB 的形狀 ≠ API 的形狀，例如密碼進得來出不去） |
| `core/config.py` | 把 `.env` 讀成一個有型別的 `settings` 物件；全 app 唯一碰 `.env` 的地方 |
| `core/db.py` | `engine`（連線）、`SessionLocal`（發 session）、`Base`（model 的共同父類別）、`get_db`（每個請求給一個 session） |
| `core/security.py` | 純計算，不碰 DB / HTTP：`hash_password` / `verify_password` / `create_access_token` / `decode_access_token` |
| `alembic/` | schema 的版本控制：model 改了 → `alembic revision --autogenerate` 生 migration → `alembic upgrade head` 套用到 Neon |

---

## 一個請求怎麼流過去（`POST /auth/register`）

```
client  POST /auth/register {email, password}
  │
  ▼  api/auth.py  register()
  │    FastAPI 用 UserCreate 驗 body
  │
  ▼  auth_service.register_user(db, email, password)
  │    1. user_repo.get_user_by_email(email)  ──→ Neon: SELECT ... WHERE email=?
  │       └─ 有重複 → raise EmailAlreadyRegistered ──→ register() except → HTTP 409
  │    2. security.hash_password(password)      ──→ 回 hash 字串
  │    3. user_repo.create_user(email, hash)    ──→ Neon: INSERT INTO users
  │       └─ 回新的 User
  │
  ▼  register() 拿到 User → FastAPI 套 response_model=UserOut（去掉密碼）
  │
  ▼  201  { "id": ..., "email": ..., "created_at": ... }
```
