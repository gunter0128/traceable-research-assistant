# 包 OpenAI SDK 的呼叫。純計算，不碰 DB / HTTP，跟 security.py 同一種角色

from openai import OpenAI

from app.core.config import settings

EMBEDDING_MODEL = "text-embedding-3-small"
# 負責生成答案用的模型 跟上面 embedding 用的是不同任務 分開一個常數
GENERATION_MODEL = "gpt-4o-mini"

client = OpenAI(api_key=settings.openai_api_key)


# 何時呼叫：document_service 把 PDF 切好段之後 每一段呼叫一次 把文字變成向量存進 chunks 表
def embed_text(text: str) -> list[float]:
    # response 是整包回應物件(可能有其他呼叫資訊) data 是"每一筆輸入各自的結果"列表（因為 input 理論上可以一次丟很多段文字）
    # 我們這次只丟一段文字 所以只有一筆結果 [0] 拿到那一筆 .embedding 才是真正的向量本身
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


# 何時呼叫：qa_service 把問題跟搜到的 chunk 內容組成 context 之後，丟給 LLM 生成答案
# system 那句話是強制「只能照資料回答，答不出來要老實說」防止 LLM 亂編答案
def generate_answer(question: str, context: str) -> str:
    response = client.chat.completions.create( # 對話生成的功能
        model=GENERATION_MODEL,
        messages=[
            {
                "role": "system", # 開發者給 LLM 的規則
                "content": (
                    "你是一個只根據提供的資料回答問題的助手。"
                    "只能使用下面「資料」裡的內容回答，不能使用你自己的知識。"
                    "如果資料裡沒有足夠的資訊回答這個問題，"
                    "就明確說「文件中找不到相關資訊」，不要編造答案。"
                ),
            },
            {"role": "user", "content": f"資料：\n{context}\n\n問題：{question}"}, # 真正的使用者輸入
        ],
    )
    return response.choices[0].message.content # 選擇第一個候選回應(一個問題可能有好幾種回應)
