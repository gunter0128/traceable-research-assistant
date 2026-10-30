# 測試共用：假的 S3，和照前端的方式上傳一份文件（docs/decisions/013）

# 假的 S3：一個 dict，key → 檔案內容
# conftest.py 的 fake_s3 每個測試開始前把它清空，再把 s3_client 的呼叫都換成讀寫這個 dict
bucket: dict[str, bytes] = {}


# 照前端的方式上傳：1. 跟 API 要上傳許可  2. 「直接傳到 S3」（這裡是放進假的 bucket）  3. 告訴 API 傳好了
# 回傳第 3 步的回應；第 1 步就被擋下來的話（例如不是 PDF、太大、用量到上限），回傳第 1 步的回應
def upload_pdf(client, headers, workspace_id, content, filename="doc.pdf"):
    started = client.post(
        f"/workspaces/{workspace_id}/uploads",
        json={"filename": filename, "size_bytes": len(content)},
        headers=headers,
    )
    if started.status_code != 200:
        return started
    key = started.json()["key"]
    bucket[key] = content
    return client.post(
        f"/workspaces/{workspace_id}/documents",
        json={"key": key, "filename": filename},
        headers=headers,
    )
