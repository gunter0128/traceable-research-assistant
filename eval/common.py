# 兩套考卷（企業 enterprise、勞動法規 labor）共用的題目格式和輸出（docs/decisions/016、017、031）
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


# 文字裡的中文數字換成阿拉伯數字：「百分之五十」→「50%」、「三十日」→「30日」
# pad：換出來的數字前後加的字，用來跟旁邊的數字隔開（見 _numbers_in）；不加就是空字串
# re.sub 的第二個參數給函式：每找到一個符合的地方就呼叫一次，回傳要換成的文字
def chinese_to_digits(text: str, pad: str = "") -> str:
    text = re.sub(r"百分之([零〇一二兩三四五六七八九十百]+)", lambda m: f"{cn_number(m.group(1))}%", text)
    return re.sub(r"[零〇一二兩三四五六七八九十百千]+", lambda m: f"{pad}{cn_number(m.group())}{pad}", text)


# 比對用：拿掉所有空白、中文數字換成阿拉伯數字
def normalize(text: str) -> str:
    return chinese_to_digits(re.sub(r"\s", "", text))


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


# ---------- 引用對得上（docs/decisions/038）----------
# 「引用完整」只看該引用的文件有沒有出現在某個 [n] 裡，不管每一句標的對不對
# 這裡逐句檢查：句子裡的數字，要出現在它標的那幾段原文裡。例如寫「30 日內 [5]」，第 5 段要有 30 日
# 只檢查有數字、有標 [n] 的句子；規章的答案大多有數字，沒有數字的句子要靠意思判斷，這裡不管
# 「原文」是模型當時看到的 [n]：那份文件的檔名、文件開頭，加上那一段（版本日期常寫在開頭或檔名）
# 不檢查：條號（第 38 條、第 3 項）——段落從條文中間切開時，條號常在上一段；問題裡本來就有的數字
# 偏寬：只要數字出現在標的段落裡就算，不管是不是同一件事的數字；自己算出來的數字（例如百分比）會被當成對不上
CITATION_MARKER = re.compile(r"[\[［]\s*(\d+(?:\s*[,，、]\s*\d+)*)\s*[\]］]")


# 文字裡的數字：中文數字先轉成阿拉伯數字，千分位的逗號拿掉
# 不能用上面的 normalize：它會拿掉所有空白，表格裡「P4–P5 3,600 元」會黏成 P4–P53600，數字就錯了
def _numbers_in(text: str) -> set[str]:
    text = chinese_to_digits(text, pad=" ")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)  # 千分位：12,000 → 12000
    return numbers(text)


def _claim_numbers(text: str) -> set[str]:
    text = re.sub(r"\S+\.pdf", "", text)  # 檔名裡的數字（2026年版）不是在講內容
    text = re.sub(r"[A-Z]+-\d+", "", text)  # 文件代號（HR-02、L-01）
    text = re.sub(r"(?m)^\s*\d+\s*[.、)）]", "", text)  # 條列的編號（1. 2.）
    # 條號和段落位置（第 38 條、第三項、第 50-1 條、第 4 段）
    text = re.sub(r"第\s*[\d零〇一二兩三四五六七八九十百]+(?:\s*[-之]\s*[\d一二三四五六七八九十]+)?\s*[條項款目點章節篇編段]", "", text)
    return _numbers_in(text)


# 回傳每個「有數字、有標 [n]」的句子：[{sentence, cited, missing}]，missing 是標的原文裡找不到的數字
# passages：模型當時看到的每個 [n]（檔名 + 文件開頭 + 那一段），照編號排（第 0 個是 [1]）
# given：問題、今天日期——裡面本來就有的數字不檢查（例如「年資 23 年」是使用者自己說的，今天幾號是系統給的，都不是從文件來的）
def citation_checks(answer: str, passages: list[str], given: str) -> list[dict]:
    given = _claim_numbers(given)
    checks = []
    # 照句號、驚嘆號、問號、換行切成句子（切的符號留在前一句的結尾）
    for sentence in re.split(r"(?<=[。！？\n])", answer):
        cited = _cited_numbers(sentence, len(passages))
        claimed = _claim_numbers(CITATION_MARKER.sub("", sentence)) - given
        if not cited or not claimed:
            continue
        # 標的那幾段原文裡出現過的數字（原文不拿掉條列編號：法條的「一、」「1.」後面常常就是要找的數字）
        found = set()
        for n in cited:
            found |= _numbers_in(passages[n - 1])
        checks.append({"sentence": sentence.strip(), "cited": sorted(cited), "missing": sorted(claimed - found)})
    return checks


# 這一句標了哪幾段：[1][3]、[1, 3] 都算，超出範圍（給模型看的段數以外）的編號不算
def _cited_numbers(sentence: str, count: int) -> set[int]:
    cited = set()
    for marker in CITATION_MARKER.findall(sentence):
        for n in re.findall(r"\d+", marker):
            if 1 <= int(n) <= count:
                cited.add(int(n))
    return cited


TEST_RATIO = 0.3


def assign_splits(questions: list[Question], previous: Path | None = None) -> None:
    # 每種題型各自隨機抽約 30% 當考試題，兩邊的題型比例才一樣、比得了
    # 固定隨機種子：重跑時分法一樣，考試題不會因為重跑而換掉
    # previous：之前產生的 questions.jsonl。已經分好的題目維持原本的分法，只有新題目抽籤（docs/decisions/031）
    #   不然加題、刪題之後重抽，看過、分析過的練習題可能變成考試題，考試題就不再是「沒看過的」
    kept = {}  # {題號: "dev" 或 "test"}，之前已經分好的
    if previous is not None and previous.exists():
        for line in previous.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item:
                kept[item["id"]] = item["split"]
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
