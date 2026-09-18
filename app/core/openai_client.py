# 包 OpenAI SDK 的呼叫。純計算，不碰 DB / HTTP，跟 security.py 同一種角色

from openai import OpenAI

from app.core.config import settings

# 跟 chunker.py 切段用的模型是同一個 這樣切出來的 token 數量限制才會跟真正拿去 embedding 的模型對得上
EMBEDDING_MODEL = "text-embedding-3-small"

# client 只需要建立一次 跟 chunker.py 的 ENCODING 是同樣的道理
client = OpenAI(api_key=settings.openai_api_key)


# 何時呼叫：document_service 把 PDF 切好段之後 每一段呼叫一次 把文字變成向量存進 chunks 表
def embed_text(text: str) -> list[float]:
    # response 是整包回應物件(可能有其他呼叫資訊) data 是"每一筆輸入各自的結果"列表（因為 input 理論上可以一次丟很多段文字）
    # 我們這次只丟一段文字 所以只有一筆結果 [0] 拿到那一筆 .embedding 才是真正的向量本身
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return response.data[0].embedding
