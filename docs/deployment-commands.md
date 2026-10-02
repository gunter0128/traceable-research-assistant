# 部署指令小抄

V2(部署到 AWS)過程中用到的指令,整理起來備查。指令不像程式碼,下過之後不會留在專案裡,容易忘記在幹嘛、哪些要重複做、哪些只做一次。

## 先搞懂語法結構

這份裡大部分指令都是同一種結構:

```
工具名稱 子指令 參數...
```

例如 `docker tag ...`、`docker push ...`、`aws ecr create-repository ...`——`docker`/`aws` 是工具本身,`tag`/`push`/`ecr create-repository` 是子指令(告訴工具這次要做哪種動作),後面才是參數。這個結構不是 Docker、AWS 特有的,`git commit -m "..."` 也是同一套邏輯(`git` 工具、`commit` 子指令)。認出這個結構,以後看到新的 CLI 工具也比較看得懂在幹嘛。

---

## Docker:本機 build + 測試

### 建立 image

```bash
docker build -t traceable-research-assistant .
```
照 [Dockerfile](../Dockerfile) 的步驟組出一個 image。`-t` 後面是取的名字,沒指定標籤(`:版本`)時,Docker 自動補 `:latest`。

**`:latest` 不是「保證最新版」的機制,只是沒指定標籤時的預設名字**。每次沒指定標籤重新 build,新的 image 會把這個名字蓋過去,感覺上「latest 一直是最新的」,但這是因為你一直用同一個預設名字,不是 Docker 在幫你追蹤版本。

**什麼時候要重做**:改了程式碼、想測新版本的時候。

### 啟動容器測試

```bash
docker run -d -p 8000:8000 --env-file .env --name trace-test traceable-research-assistant
```
- `-d`——背景執行(detached)
- `-p 8000:8000`——port mapping,格式是 `本機埠:容器埠`,沒有這個外面連不進容器
- `--env-file .env`——把 `.env` 裡每一行當環境變數注入容器（**`.env` 格式要是 `KEY=值`，等號兩邊不能有空格，Docker 的解析器比 Python 那邊嚴格，空格會被當成值的一部分**）
- `--name trace-test`——幫容器取名，方便之後用 `docker logs trace-test`、`docker rm trace-test` 指名操作

### 檢查、收尾

```bash
docker ps              # 看目前「正在跑」的容器
docker ps -a            # 連「已經停止」的也列出來（logs 要看已停止的容器，用這個才找得到）
docker logs trace-test  # 看這個容器印出來的東西（程式的 print、錯誤訊息都在這）
docker rm -f trace-test # 強制刪掉這個容器（-f 代表不管它是不是還在跑，直接刪）
```

---

## AWS CLI:連上帳號

```powershell
aws configure
```
互動式輸入 Access Key ID、Secret Access Key、預設區域、輸出格式。**這組金鑰要用 `chun-admin`（自己的管理帳號），不是 `trace-app-s3`（那組權限被鎖死只能碰 S3，做不了 ECR 這些事）**。

```bash
aws sts get-caller-identity
```
只問「我現在是誰」,不會建立或修改任何東西,純驗證身份用。

---

## ECR:image 的雲端倉庫

### 建倉庫（只做一次）

```bash
aws ecr create-repository --repository-name traceable-research-assistant --region ap-southeast-2
```
建立之後會一直存在，**不用每次推 image 都重建**。

### 登入（有效期 12 小時，過期要重做）

```bash
aws ecr get-login-password --region ap-southeast-2 | docker login --username AWS --password-stdin 416113247224.dkr.ecr.ap-southeast-2.amazonaws.com
```
- `aws ecr get-login-password`——跟 AWS 要一組臨時通行證（12 小時後失效）
- `|`——管線，把左邊指令的輸出，直接餵給右邊指令當輸入
- `docker login --username AWS --password-stdin <網址>`——用這組通行證登入 ECR。`--username AWS` 的 `AWS` 是**固定寫死的字串**，不是你的帳號名稱；`--password-stdin` 代表密碼從管線接，不用手動打

**12 小時內要推好幾次，只要登入一次；隔久了再推，要重新跑這行拿新的通行證。**

### Tag + Push（每次要發布新版本都要做）

```bash
docker tag traceable-research-assistant:latest 416113247224.dkr.ecr.ap-southeast-2.amazonaws.com/traceable-research-assistant:latest
docker push 416113247224.dkr.ecr.ap-southeast-2.amazonaws.com/traceable-research-assistant:latest
```

ECR 位址的結構(跟 GitHub 網址是同一種邏輯):
```
416113247224.dkr.ecr.ap-southeast-2.amazonaws.com / traceable-research-assistant : latest
└──────────伺服器網址（你帳號+區域專屬）──────────┘   └──倉庫名稱（對應 create-repository 時取的名字）──┘  └標籤┘
```

- **`docker tag`**——幫本機已經存在的 image，多貼一個新名字（這個新名字裡面包含 ECR 的位址），本體還是同一份 image，不會複製出第二份
- **`docker push`**——讀這個新名字裡的位址，把對應的 image 內容上傳過去。**`push` 本身不能指定「要送去哪」，完全靠讀 image 的名字來判斷，所以一定要先 `tag` 才能 `push`**

跟 `git add → commit → push` 一樣，**這是以後每次要發布新版本都要重複的日常流程**，不是一次性動作。

### 確認真的推上去了

```bash
aws ecr describe-images --repository-name traceable-research-assistant --region ap-southeast-2
```
列出這個倉庫裡現有的 image，`imageStatus: "ACTIVE"` 代表確實存在且可用。
