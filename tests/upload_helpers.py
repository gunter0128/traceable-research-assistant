# 測試共用：照前端的方式上傳一份文件（docs/decisions/013）
#   1. 跟 API 要上傳許可  2. 「直接傳到 S3」（測試裡是放進假的 bucket）  3. 告訴 API 傳好了
# 回傳第 3 步的回應；第 1 步就被擋下來的話（例如不是 PDF、太大、用量到上限），回傳第 1 步的回應

from app.core import s3_client


def upload_pdf(client, headers, workspace_id, content, filename="doc.pdf"):
    started = client.post(
        f"/workspaces/{workspace_id}/uploads",
        json={"filename": filename, "size_bytes": len(content)},
        headers=headers,
    )
    if started.status_code != 200:
        return started
    key = started.json()["key"]
    s3_client.upload_file(key, content)
    return client.post(
        f"/workspaces/{workspace_id}/documents",
        json={"key": key, "filename": filename},
        headers=headers,
    )
