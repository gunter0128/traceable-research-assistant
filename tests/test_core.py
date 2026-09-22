# core/ 的純函式測試：chunker、pdf_parser。不碰資料庫、不碰 HTTP，不需要 client/db_session

import pytest

from app.core import chunker, pdf_parser
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
