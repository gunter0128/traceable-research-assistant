# core/ 的純函式測試：chunker、pdf_parser。不碰資料庫、不碰 HTTP，不需要 client/db_session

import io
import os

import pytest

from app.core import aws_clients, chunker, lambda_secrets, pdf_parser
from app.core.config import settings
from tests.pdf_fixtures import valid_pdf_bytes


# 測試：長文字會被切成不只一段
def test_chunk_text_splits_long_text():
    long_text = "測試內容。" * 500
    pieces = chunker.chunk_text(long_text, chunk_size=100, overlap=10)

    assert len(pieces) > 1
    for piece in pieces:
        assert isinstance(piece, str)
        assert piece


# 測試：短文字（token 數不到 chunk_size）只會切出一段
def test_chunk_text_short_text_single_chunk():
    pieces = chunker.chunk_text("很短的一段話", chunk_size=500, overlap=50)
    assert len(pieces) == 1


# 測試：overlap 越大，前進的步伐（chunk_size - overlap）越小 切出來的段數不會變少
def test_chunk_text_larger_overlap_means_more_or_equal_chunks():
    long_text = "測試內容。" * 300
    few_pieces = chunker.chunk_text(long_text, chunk_size=100, overlap=0)
    more_pieces = chunker.chunk_text(long_text, chunk_size=100, overlap=80)

    assert len(more_pieces) >= len(few_pieces)


# 測試：正常 PDF 抓得到文字
def test_extract_text_from_valid_pdf(tmp_path):
    pdf_path = tmp_path / "test.pdf" # tmp_path 是 pytest 給的一個全新空資料夾 然後自己取一個檔案名字
    pdf_path.write_bytes(valid_pdf_bytes("Hello World")) # 呼叫 fixture 函式把字寫進去

    text = pdf_parser.extract_text(pdf_path)

    assert "Hello World" in text


# 測試：壞掉的 PDF 丟出我們自己定義的 UnreadablePdf 不是讓 pypdf 的錯誤直接爆出來
def test_extract_text_from_corrupt_pdf_raises(tmp_path):
    pdf_path = tmp_path / "broken.pdf"
    pdf_path.write_bytes(b"not a real pdf")

    with pytest.raises(pdf_parser.UnreadablePdf):
        pdf_parser.extract_text(pdf_path)


# 測試：頁數超過上限 → 丟 TooManyPages（valid_pdf_bytes 產生的是 1 頁，上限設 0 就超過）
def test_extract_text_too_many_pages_raises():
    with pytest.raises(pdf_parser.TooManyPages):
        pdf_parser.extract_text(io.BytesIO(valid_pdf_bytes("hello")), max_pages=0)


# 假的 Secrets Manager：依密碼名稱回傳固定內容，不連到真的 AWS
class _FakeSecretsManager:
    def get_secret_value(self, SecretId):
        return {"SecretString": f"value-of-{SecretId}"}


# 測試：在 Lambda 上（有 AWS_LAMBDA_FUNCTION_NAME）→ 從 Secrets Manager 把三個密碼讀進環境變數
def test_lambda_secrets_loaded_in_lambda(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "trace-worker")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    # 先用 monkeypatch 設一次，測試結束後這三個環境變數才會被還原，不影響其他測試
    for name in lambda_secrets.SECRETS:
        monkeypatch.setenv(name, "before")
    monkeypatch.setattr(lambda_secrets.boto3, "client", lambda *args, **kwargs: _FakeSecretsManager())

    lambda_secrets.load_if_in_lambda()

    assert os.environ["DATABASE_URL"] == "value-of-trace-app/database-url"
    assert os.environ["OPENAI_API_KEY"] == "value-of-trace-app/openai-api-key"


# 測試：不在 Lambda 上（本機、測試）→ 什麼都不做，也不會去連 AWS
def test_lambda_secrets_skipped_outside_lambda(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("不在 Lambda 上不該連 Secrets Manager")

    monkeypatch.setattr(lambda_secrets.boto3, "client", must_not_be_called)

    lambda_secrets.load_if_in_lambda()  # 不丟例外就算通過


# 測試：有金鑰時（本機、Lambda），session token 要跟金鑰一起交給 boto3
# Lambda 的臨時憑證少了 token 就無效；本機沒有 token 時是 None，boto3 會當作沒給
def test_make_client_passes_session_token(monkeypatch):
    monkeypatch.setattr(settings, "aws_access_key_id", "key")
    monkeypatch.setattr(settings, "aws_secret_access_key", "secret")
    monkeypatch.setattr(settings, "aws_session_token", "token")
    captured = {}
    monkeypatch.setattr(aws_clients.boto3, "client", lambda service, **kwargs: captured.update(kwargs))

    aws_clients.make_client("s3")

    assert captured["aws_session_token"] == "token"
