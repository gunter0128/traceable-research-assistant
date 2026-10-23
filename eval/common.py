# 各套知識庫（企業 enterprise、學校 school、勞動法規 labor）共用的題目格式和輸出（docs/decisions/016、017、031）
#
# 每題的欄位：
#   type          general 普通題 / conditional 條件題 / persona 身分題 / version 版本題 /
#                 scope 範圍陷阱 / cross_doc 跨文件題 / no_answer 沒有答案
#   question      使用者的問題（身分寫在問題裡，跟真實使用時一樣）
#   answer        標準答案（給人看、也給評審參考）
#   must_include  好的答案一定要提到的事實
#   must_not      出現就算錯的說法（例如把正職的規定套到實習生身上）
#   sources       應該引用的文件和條文
#   should_refuse 文件裡沒有答案，應該說找不到
#   split         dev 練習題：改進時可以看、拿來分析錯在哪 / test 考試題：改進過程中不看，只在最後確認時跑
#                 防止過度擬合：只在練習題上進步、考試題沒進步，代表是在背考卷

import json
import random
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 各套知識庫的設定
CORPORA = {
    "enterprise": {
        "title": "青嶼科技",
        "domain": "公司內部規章",
        "footer": "本文件為虛構公司「青嶼科技」之測試資料，僅供系統評估使用，非真實公司規章。",
        "workspace": "青嶼科技規章",
    },
    "school": {
        "title": "澄溪國小",
        "domain": "學校規章與家長須知",
        "footer": "本文件為虛構學校「澄溪國小」之測試資料，僅供系統評估使用，非真實學校規章。",
        "workspace": "澄溪國小規章",
    },
    # 真實的長文件（docs/decisions/031）：從全國法規資料庫下載的勞動法規
    "labor": {
        "title": "勞動法規",
        "domain": "台灣的勞動法規",
        "footer": "資料來源：全國法規資料庫（法務部），2026 年 10 月 24 日下載。",
        "workspace": "勞動法規",
    },
}

TYPE_NAMES = {"general": "普通題", "conditional": "條件題", "persona": "身分題", "version": "版本題",
              "scope": "範圍陷阱", "cross_doc": "跨文件題", "no_answer": "沒有答案"}


@dataclass
class Question:
    id: str
    type: str
    question: str
    answer: str
    must_include: list[str]
    sources: list[str]
    must_not: list[str] = field(default_factory=list)
    # 加分項：有提到更好，但少了也不會讓使用者做錯事，不影響「全對」（docs/decisions/030）
    nice_to_have: list[str] = field(default_factory=list)
    should_refuse: bool = False
    split: str = "dev"


def money(n: int) -> str:
    return f"{n:,} 元"


# ---------- 中文數字轉阿拉伯數字（只用來比對，兩邊用同一種轉法）----------
DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
UNITS = {"十": 10, "百": 100, "千": 1000}


def cn_number(text: str) -> int:
    total, current = 0, 0
    for ch in text:
        if ch in DIGITS:
            current = DIGITS[ch]
        else:
            total += (current or 1) * UNITS[ch]
            current = 0
    return total + current


def normalize(text: str) -> str:
    text = re.sub(r"\s", "", text)
    text = re.sub(r"百分之([零〇一二兩三四五六七八九十百]+)", lambda m: f"{cn_number(m.group(1))}%", text)
    return re.sub(r"[零〇一二兩三四五六七八九十百千]+", lambda m: str(cn_number(m.group())), text)


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


# ---------- 依據的原文有沒有被找到（docs/decisions/031）----------
# 長文件上，「找到那份文件」不代表「找到答案的那一條」：勞基法有 98 條，找到勞基法不一定找到第 38 條
# 題目有附依據的原文時（勞動法規的 EVIDENCE），檢查系統找到的段落裡有沒有這段原文
# 原文剛好被切在兩段交界的話，兩段都找到也算：原文的開頭和結尾分別出現在找到的段落裡
def quote_found(quote: str, texts: list[str]) -> bool:
    target = normalize(quote)
    found = [normalize(text) for text in texts]
    if any(target in text for text in found):
        return True
    head, tail = target[:12], target[-12:]
    return any(head in text for text in found) and any(tail in text for text in found)


# 這題依據的原文，找到了幾成（0～1）；沒有附原文的題目回傳 None
def evidence_rate(quotes: list[str], texts: list[str]) -> float | None:
    if not quotes:
        return None
    return sum(quote_found(quote, texts) for quote in quotes) / len(quotes)


TEST_RATIO = 0.3


def assign_splits(questions: list[Question], previous: Path | None = None) -> None:
    # 每種題型各自隨機抽約 30% 當考試題，兩邊的題型比例才一樣、比得了
    # 固定隨機種子：重跑時分法一樣，考試題不會因為重跑而換掉
    # previous：之前產生的 questions.jsonl。已經分好的題目維持原本的分法，只有新題目抽籤（docs/decisions/031）
    #   不然加題、刪題之後重抽，看過、分析過的練習題可能變成考試題，考試題就不再是「沒看過的」
    kept = {}
    if previous is not None and previous.exists():
        kept = {item["id"]: item["split"] for item in map(json.loads, previous.read_text(encoding="utf-8").splitlines()) if item}
    rng = random.Random(2026)
    for qtype in sorted({q.type for q in questions}):
        group = [q for q in questions if q.type == qtype]
        for q in group:
            q.split = kept.get(q.id, "dev")
        new = [q for q in group if q.id not in kept]
        # 原本的考試題全部保留；不夠 30% 的，從新題目裡抽來補
        missing = round(len(group) * TEST_RATIO) - sum(q.split == "test" for q in group)
        for q in rng.sample(new, max(0, min(missing, len(new)))):
            q.split = "test"


def write_question_files(questions: list[Question], folder: Path, title: str, command: str) -> None:
    # 產生 questions.jsonl（給評分程式用）和 questions.md（給人看，考試題只列題號、不列內容）
    ids = [q.id for q in questions]
    assert len(ids) == len(set(ids)), "題號重複"
    (folder / "questions.jsonl").write_text(
        "\n".join(json.dumps(asdict(q), ensure_ascii=False) for q in questions) + "\n", encoding="utf-8")
    lines = [f"# {title}", "", f"由 `{command}` 產生，標準答案的數字來自 `rules.py`。", ""]
    for qtype, name in TYPE_NAMES.items():
        group = [q for q in questions if q.type == qtype]
        hidden = [q.id for q in group if q.split == "test"]
        note = f"，其中考試題 {len(hidden)} 題：{'、'.join(hidden)}，內容不列出" if hidden else ""
        lines += [f"## {name}（{len(group)} 題{note}）", ""]
        for q in group:
            if q.split == "test":
                continue
            lines.append(f"**{q.id}　{q.question}**")
            lines.append(f"- 標準答案：{q.answer}")
            if q.sources:
                lines.append(f"- 應該引用：{'、'.join(q.sources)}")
            if q.must_not:
                lines.append(f"- 不能說：{'；'.join(q.must_not)}")
            if q.nice_to_have:
                lines.append(f"- 加分：{'；'.join(q.nice_to_have)}")
            lines.append("")
    (folder / "questions.md").write_text("\n".join(lines), encoding="utf-8")
    counts = {name: sum(1 for q in questions if q.type == t) for t, name in TYPE_NAMES.items()}
    tests = sum(1 for q in questions if q.split == "test")
    print(f"共 {len(questions)} 題：{counts}；練習題 {len(questions) - tests}、考試題 {tests}")
