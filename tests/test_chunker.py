# 切段的測試（docs/decisions/032）：純函式，不需要資料庫

import re

from app.core import chunker

LAW = """勞動基準法
修正日期：民國 113 年 07 月 31 日
第 一 章 總則
第 1 條
為規定勞動條件最低標準，特制定本法。
第 2 條
本法用詞，定義如下：
一、勞工：指受雇主僱用從事工作獲致工資者。
二、雇主：指僱用勞工之事業主。
第 二 章 休假
第 38 條
勞工繼續工作滿一定期間者，應依下列規定給予特別休假：
一、六個月以上一年未滿者，三日。
二、一年以上二年未滿者，七日。
三、二年以上三年未滿者，十日。
前項之特別休假期日，由勞工排定之。
"""


def _tokens(text: str) -> int:
    return len(chunker.ENCODING.encode(text))


def _body(chunk: str) -> str:
    # 去掉開頭的位置前綴【……】
    return re.sub(r"^【[^】]*】\n", "", chunk)


# 測試：放得下的條文不會被切斷；完整的小條文會合在一起
def test_short_articles_stay_whole():
    chunks = chunker.chunk_structured(LAW, chunk_size=500)

    assert len(chunks) == 1
    assert "第 38 條" in chunks[0] and "三、二年以上三年未滿者，十日。" in chunks[0]


# 測試：一條太長、要拆開時，拆在「一、二、」這種結構上，不從一句話中間切；
# 而且不會跟上一條混在同一段，拆出來的後半段開頭附上它屬於哪一章、哪一條
def test_long_article_splits_at_items_with_position():
    long_item = "前項之特別休假期日，由勞工排定之。" * 12
    text = LAW.replace("前項之特別休假期日，由勞工排定之。", long_item)

    chunks = chunker.chunk_structured(text, chunk_size=260)

    article = [c for c in chunks if "特別休假" in c]
    assert article[0].startswith("【勞動基準法 > 第 二 章 休假】\n第 38 條")  # 從第 38 條開始，沒有混進第 2 條
    assert "第 2 條" not in article[0]
    assert all(c.startswith("【勞動基準法 > 第 二 章 休假 > 第 38 條】") for c in article[1:])
    # 每一款都完整地在某一段裡
    for item in ("一、六個月以上一年未滿者，三日。", "二、一年以上二年未滿者，七日。", "三、二年以上三年未滿者，十日。"):
        assert any(item in c for c in chunks)


# 測試：每一段都不超過上限（含前綴）
def test_chunks_respect_the_limit():
    text = LAW * 6

    chunks = chunker.chunk_structured(text, chunk_size=200)

    assert all(_tokens(c) <= 200 for c in chunks)


# 測試：沒有任何結構的長文字，退到句號切，不會從一句話中間切斷；內容一個字都不少
def test_plain_text_falls_back_to_sentences():
    sentences = [f"這是第{i}句，用來測試沒有標題也沒有編號的長文章。" for i in range(60)]
    text = "標題\n" + "".join(sentences)

    chunks = chunker.chunk_structured(text, chunk_size=150)

    assert len(chunks) > 1
    joined = "".join(_body(c).replace("\n", "") for c in chunks)
    assert joined == text.replace("\n", "")
    for c in chunks:
        assert _body(c).rstrip().endswith("。")


# 測試：預設還是固定長度（線上的行為不變），換成 structured 才照結構切
def test_default_strategy_is_fixed(monkeypatch):
    assert chunker.STRATEGY == "fixed"
    assert chunker.chunk(LAW) == chunker.chunk_text(LAW, chunker.CHUNK_SIZE)

    monkeypatch.setattr(chunker, "STRATEGY", "structured")
    assert chunker.chunk(LAW) == chunker.chunk_structured(LAW, chunker.CHUNK_SIZE)
