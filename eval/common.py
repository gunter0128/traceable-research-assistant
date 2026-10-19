# 兩套知識庫（企業 enterprise、學校 school）共用的題目格式和輸出（docs/decisions/016、017）
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
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 兩套知識庫各自的設定
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
    should_refuse: bool = False
    split: str = "dev"


def money(n: int) -> str:
    return f"{n:,} 元"


TEST_RATIO = 0.3


def assign_splits(questions: list[Question]) -> None:
    # 每種題型各自隨機抽約 30% 當考試題，兩邊的題型比例才一樣、比得了
    # 固定隨機種子：重跑時分法一樣，考試題不會因為重跑而換掉
    rng = random.Random(2026)
    for qtype in sorted({q.type for q in questions}):
        group = [q for q in questions if q.type == qtype]
        for q in rng.sample(group, round(len(group) * TEST_RATIO)):
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
            lines.append("")
    (folder / "questions.md").write_text("\n".join(lines), encoding="utf-8")
    counts = {name: sum(1 for q in questions if q.type == t) for t, name in TYPE_NAMES.items()}
    tests = sum(1 for q in questions if q.split == "test")
    print(f"共 {len(questions)} 題：{counts}；練習題 {len(questions) - tests}、考試題 {tests}")
