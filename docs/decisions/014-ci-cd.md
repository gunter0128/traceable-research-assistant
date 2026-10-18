# 014：CI/CD 用 GitHub Actions

日期：2026-10-18

## 背景

到 C4 為止，每次發布都是手動打一長串指令：登入 ECR、build、tag、push、更新三個 Lambda；改了前端還要 build、上傳 S3、清 CloudFront 快取。這幾天因為手動部署出過好幾次問題：

- push 失敗了沒發現，Lambda 和 ECS 更新到的是舊版
- 漏跑 ECS 的 `force-new-deployment`，API 一直是舊版
- A5 演練時以為 API 是新版，其實跑的是兩天前的 image
- 本機跑一次測試要 10 分鐘，很容易就跳過不跑

而且我入伍之後沒辦法坐在電腦前打這些指令。系統要能在我不在的時候修小 bug：在任何地方改好、push，剩下的自動完成。

## 決定

用 GitHub Actions，設定檔是 `.github/workflows/ci-cd.yml`。

```
每次 push
  ├─ 後端測試：ruff 檢查 + pytest
  ├─ 前端檢查：型別檢查 + build
  └─ 兩個都通過，而且是 main 分支 →
       後端有改：build image → 推到 ECR → migration → 更新 3 個 Lambda → 熱身 + 確認 API 能用
       前端有改：build → 上傳 S3 → 清 CloudFront 快取
```

CI 擋的是「上線」，不是「push」：測試沒過，程式碼一樣會在 GitHub 上，但不會部署，雲端還是上一個通過的版本。

### 1. 測試用 GitHub 臨時開的 PostgreSQL，不連 Neon

GitHub Actions 可以在那台臨時機器上，跟測試一起開一個資料庫容器（service container，用 `pgvector/pgvector:pg16`），跑完就丟掉。

| | 臨時資料庫 | Neon 測試分支 |
|---|---|---|
| 速度 | 資料庫就在旁邊，本機模擬實測 **1 分 45 秒** | GitHub 的機器在美國，連到新加坡每次都很慢；本機（台灣）就要 10 分鐘 |
| 密碼 | 不用 | 要把 Neon 的網址放在 GitHub |
| 同時跑 | 每次都是全新的，互不干擾 | 兩次 push 同時跑會互相刪資料表 |

為了讓同一份測試在兩邊都能跑，`tests/conftest.py` 改成：有 `.env.test` 就讀（本機），沒有就用環境變數（CI）；並且在開始前啟用 pgvector（全新的資料庫還沒啟用）。

### 2. AWS 權限用 OIDC，GitHub 上不存金鑰

在 AWS 設定「信任 GitHub 發的證明」，部署時 GitHub 拿一張「我是 gunter0128/traceable-research-assistant 的 main 分支」的證明，跟 AWS 換一組幾分鐘就過期的臨時憑證。

不把 AWS 金鑰存在 GitHub：長期有效的金鑰多放一個地方，就多一個外流的機會。這跟 Lambda 用角色、不放金鑰是同一個原則。

部署用的角色 `trace-app-deploy-role` 只給部署需要的權限：推 image 到這一個 ECR 倉庫、更新和叫醒這三個 Lambda、讀資料庫網址（migration 用）、上傳前端到這一個 bucket、清這一個 CloudFront 的快取。而且只信任 main 分支，別的分支就算改了設定檔也拿不到權限。

### 3. migration 自動跑

部署後端時，在更新 Lambda 之前跑 `alembic upgrade head`。資料庫網址從 Secrets Manager 讀，並遮蔽，不會出現在 GitHub 的執行紀錄裡。

資料庫已經是最新版的話，它什麼都不做，所以每次部署都跑是安全的；上次部署中途失敗、migration 沒跑到，下次也會自動補上。

要遵守的規則：**migration 要讓舊版程式也能繼續跑**（新增欄位可以，直接刪欄位或改名不行）。因為 migration 跑完到 Lambda 換好之間的幾十秒，還是舊程式在用新的資料表。要刪欄位的話，分兩次部署：先讓程式不用那個欄位，下一次再刪。

### 4. 只部署有改到的部分

比對這次 push 改了哪些檔案：

| 改到 | 做什麼 |
|---|---|
| `app/`、`alembic/`、`requirements.txt`、`Dockerfile` | 部署後端 |
| `frontend/` | 部署前端 |
| 只有文件、測試 | 只跑測試 |

不全部都部署：每次更新 Lambda，新 image 的第一次啟動都會超過 10 秒，改一個字的文件不值得。

### 其他

- **image 用 commit 編號當標籤**（同時也標 `latest`），看標籤就知道雲端跑的是哪一版。要退回上一版：`aws lambda update-function-code` 指回上一個 commit 編號的 image
- **部署完熱身**：用測量模式叫醒 worker、經過 CloudFront 打一次 API。新 image 第一次啟動要下載，超過 10 秒，熱身之後使用者就不會是第一個。同時當作檢查：API 沒有回 401 就算部署失敗，GitHub 會寄信通知
- **同一時間只跑一個**：連續 push 兩次，第二次會等第一次跑完，不會兩個部署同時改 Lambda
- 順便修掉 ruff 累積的 5 個問題（之前沒有人在檢查）

## 代價

- 多了一個要維護的設定檔，部署流程改了要記得一起改
- 部署失敗時要去 GitHub 網頁看紀錄，不是在自己的終端機
- 測試在 CI 用的是全新的 PostgreSQL，不是 Neon，兩者有極小的機會行為不同
- 手動部署的指令還留在 `docs/deployment-commands.md`，GitHub Actions 出問題時可以手動部署
