# 用官方現成的 Python 3.11 環境當起點，slim 是精簡版，體積比完整版小
FROM python:3.11-slim

# 容器裡新建一個叫 app 的工作目錄（這個名字跟專案裡的 app/ 資料夾只是剛好同名，彼此無關）
WORKDIR /app

# 故意先只複製 requirements.txt、裝好套件，才複製其他程式碼：
# 只要 requirements.txt 沒變，Docker 之後重新 build 時會跳過這步（用快取），不用每次都重裝套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 把專案其他東西複製進去（.dockerignore 裡列的會被跳過，例如 .venv/、.env、tests/）
COPY . .

# 宣告這個容器會用 8000 對外溝通，真正打開通道是之後 docker run 時另外設定
EXPOSE 8000

# 容器啟動時真正執行的指令
# --host 0.0.0.0 不能省：本機開發預設只聽 127.0.0.1（只有自己連得到），
# 但容器裡的 request 是從外面進來的，要聽 0.0.0.0 才會接受外部連線
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
