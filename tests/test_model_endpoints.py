# 轉向量、回答可以接 OpenAI 以外的服務（docs/decisions/037）：企業內部版接公司內部用 vLLM 架的模型
# 真的服務換成假的物件，只檢查「送了什麼出去」

from types import SimpleNamespace

from app.core import openai_client
from app.core.config import settings


# 假的服務：記下每次呼叫送了什麼，回傳跟 OpenAI SDK 一樣形狀的結果
class FakeService:
    def __init__(self):
        self.calls = []
        self.embeddings = SimpleNamespace(create=self._embed)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.timeouts = []

    def with_options(self, timeout):
        self.timeouts.append(timeout)
        return self

    def _embed(self, model, input):
        self.calls.append({"model": model, "input": input})
        data = [SimpleNamespace(index=i, embedding=[float(i)]) for i in range(len(input))]
        return SimpleNamespace(data=data, usage=SimpleNamespace(prompt_tokens=10))

    def _chat(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content="答案 [1]。")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


# 測試：轉問題時加上模型要求的說明，轉文件段落時不加（Qwen3-Embedding 的用法，docs/decisions/035）
def test_query_prefix_only_on_questions(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(openai_client, "embedding_client", service)
    monkeypatch.setattr(settings, "embedding_query_prefix", "Instruct: 找出回答問題的段落\nQuery:")

    openai_client.embed_queries(["特休幾天？"])
    openai_client.embed_texts(["第一段", "第二段"])

    assert service.calls[0]["input"] == ["Instruct: 找出回答問題的段落\nQuery:特休幾天？"]
    assert service.calls[1]["input"] == ["第一段", "第二段"]


# 測試：OpenAI 不用加說明，問題原封不動送出
def test_no_prefix_by_default(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(openai_client, "embedding_client", service)
    monkeypatch.setattr(settings, "embedding_query_prefix", "")

    openai_client.embed_queries(["特休幾天？"])

    assert service.calls[0]["input"] == ["特休幾天？"]


# 測試：回答送到設定的服務，帶上額外參數（例如關掉思考模式），等待上限用回答專用的
def test_answer_uses_configured_service(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(openai_client, "answer_client", service)
    monkeypatch.setattr(openai_client, "answer_extra_body", {"chat_template_kwargs": {"enable_thinking": False}})
    monkeypatch.setattr(openai_client, "ANSWER_TIMEOUT", 45)

    answer = openai_client.generate_answer("特休幾天？", "資料")

    assert answer == "答案 [1]。"
    assert service.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert service.timeouts == [45]


# 測試：沒設定網址就用 OpenAI 的 client；有設定就連到那個網址
def test_client_for():
    assert openai_client._client_for(None) is openai_client.client
    local = openai_client._client_for("http://gpu-server:8000/v1")
    assert str(local.base_url) == "http://gpu-server:8000/v1/"
