# 004：Lambda 的部署方式：共用 image、自己讀密碼、最多同時 2 個

日期：2026-10-14

003 決定了 worker 跑在 Lambda 上，實際部署時又碰到三個選擇。

## 跟 API 共用同一個 Docker image

Lambda 跑容器的方式跟 Fargate 不一樣。Fargate 啟動 uvicorn 後就在 8000 port 等請求；Lambda 不會打 HTTP 進來，容器裡要有一個程式（Runtime Interface Client，套件名 `awslambdaric`）主動去跟 Lambda 拿工作、呼叫我的函式、回報結果。

可以另外寫一個用 AWS 官方 Lambda 基礎 image 的 Dockerfile，這是官方範例的做法，也能在本機模擬 Lambda。但這樣會有兩個 image，每次改程式碼都要 build、push 兩次，只要有一次忘了其中一個，API 和 worker 跑的就是不同版本的程式碼，API 送出的工單舊版 worker 可能看不懂，這種錯很難查。

我選共用：原本的 Dockerfile 多裝 `awslambdaric`，同一個 image 在 Fargate 上照舊跑 uvicorn，在 Lambda 上由設定改成啟動 `awslambdaric`、交給 `app.worker.handle_process_message`。同一個產出物，不同的進入點。代價是 API 的 image 多了一個用不到的小套件。

`awslambdaric` 只能裝在 Linux 上，所以在 `requirements.txt` 加了 `sys_platform == "linux"` 的條件，Windows 本機開發時會跳過它。

順便發現 `.dockerignore` 沒有排除前端，158 MB 的 `node_modules` 一直被打包進後端的 image，一起修掉了。

## Lambda 啟動時自己去 Secrets Manager 讀密碼

Fargate 可以在 task definition 裡寫 `secrets`，啟動時自動把 Secrets Manager 的值放進環境變數，Lambda 沒有這個功能。

最簡單的做法是把值複製一份，直接設成 Lambda 的環境變數。但這樣同一個密碼會存在兩個地方，換密碼時兩邊都要改，有權限看 Lambda 設定的人也能直接看到明文。

我選讓 Lambda 啟動時自己去讀（`app/core/lambda_secrets.py`），讀的是跟 API 同一組密碼，密碼只存在 Secrets Manager 一個地方。只有在 Lambda 上才會執行（看 `AWS_LAMBDA_FUNCTION_NAME` 這個 Lambda 自動設定的環境變數），本機和測試照舊從 `.env` 讀。代價是多了約 20 行程式碼，每次冷啟動多呼叫三次 Secrets Manager，費用可以忽略。

## 最多同時處理 2 張工單

Lambda 預設會依工單數量同時啟動很多個。一次上傳 20 份文件，就可能有 20 個 Lambda 同時呼叫 OpenAI、同時連 Neon，前者可能撞到速率上限一起失敗，後者連線數暴增。

我在 SQS 觸發 Lambda 的設定上，把同時處理數上限設成 2（這個設定能設的最小值）。使用者只有兩三個人，大量上傳很少見；多出來的工單只是在 SQS 裡排隊，不會遺失。這正是佇列的用途：把突然湧進來的工作，攤平成系統承受得了的速度。代價是大量上傳時，全部處理完會比較慢。
