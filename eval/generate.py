# 產生評估題目的候選（B3）：每題挑合適的段落交給 gpt-4o 出題，附上證據原文和重點清單
# 用法（專案資料夾）：.venv\Scripts\python -m eval.generate
# 產生的是「候選」，要經過人工檢查（python -m eval.review）才會變成正式的評估資料集
#
# 每題的格式：
#   type            題型：fact / explain / summary / compare / no_answer / numeric
#   language        zh（繁體中文）/ en
#   question        題目
#   evidence        證據：[{paper, quote}]，quote 是論文裡一字不差的原句。不存「第幾段」：之後換切段方式也能比對
#   key_points      好的答案應該提到的重點
#   should_refuse   文件裡沒有答案、系統應該說找不到的題目
#
# 第一版（candidates-v1.jsonl）的問題和這一版的修正：
#   證據比對太嚴格（PDF 換行斷字）→ 改用 match_key 比對
#   題目沒寫論文名稱、問範例資料、問相關研究、太籠統 → 出題規則明確禁止，段落不適合就讓 AI 回 skip、換一段
#   「沒有答案」的題目其實有答案 → 每題實際搜尋驗證

import json
import random
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

from app.core import chunker
from eval.corpus import Paper, load_papers, match_key

MODEL = "gpt-4o"
OUT = Path(__file__).parent / "data" / "candidates.jsonl"
random.seed(42)  # 固定隨機種子：重跑時挑到的段落一樣，結果可以重現

client = OpenAI(api_key=dotenv_values(".env")["OPENAI_API_KEY"])

# 跨篇比較的組合：論文之間有真實的關係（互相引用、同名、同一種方法），答案才有文件根據
COMPARE_GROUPS = [
    (["RobustRAG", "TrustRAG"], "兩種防禦檢索污染的思路有什麼不同"),
    (["PoisonedRAG", "RobustRAG"], "攻擊的方式，和防禦怎麼針對它"),
    (["PoisonedRAG", "TrustRAG"], "攻擊的方式，和防禦怎麼針對它"),
    (["CARE-RepEdit", "CARE-SoftPrompt", "CARE-RAG"], "三個都叫 CARE、都處理知識衝突，但做法不同"),
    (["MAD-Sherlock", "TED", "MADAM-RAG"], "三篇都用多個 AI 辯論，但目的和流程不同"),
    (["RECOMP", "Provence"], "都在精簡檢索結果，方法不同"),
    (["RECOMP", "LongLLMLingua"], "都在壓縮給模型的內容，方法不同"),
    (["RankRAG", "SetR"], "都在決定要用哪些段落，排序 vs 選一組"),
    (["IRCoT", "MultiHop-RAG"], "多跳問題：一個是方法、一個是評估資料集"),
    (["InstructRAG", "CARE-RAG"], "都處理有雜訊的檢索結果，方法不同"),
]

COMMON_RULES = """你在幫一個 RAG 問答系統建立評估資料集。系統的使用者是研究室的研究生，
一個工作區裡放了 21 篇 RAG 相關的論文，使用者用問的了解論文內容。
規則：
- 題目一定要寫出方法或論文的名稱（例如 RobustRAG、IRCoT），不能用「這篇論文」「這個方法」，因為工作區裡有 21 篇
- 只問這篇論文「自己」的問題、方法、設計、發現。不要問：
  * 論文裡舉例用的資料內容（例如資料集裡某個問題的答案、某個人名地名）
  * 論文在「相關研究」裡介紹的別人的方法
  * 跟這篇論文無關的通用定義（例如「RAG 是什麼」）
- 如果提供的段落不適合出題（整段是相關研究、參考文獻、範例資料、只有公式），輸出 {{"skip": true}}
- evidence 的 paper 只能用這些名稱：{paper_names}
- evidence 的 quote 必須從提供的段落裡「一字不差」地複製一個連續的片段，20～300 個字元，不能改寫、不能合併不相鄰的句子
- key_points 是一個好答案應該提到的重點，每點一句話，2～5 點，只能根據提供的段落
- 題目要自然，像真的使用者會問的，不要提到「段落」「根據上文」這種字眼
- 題目的語言：{language}；key_points 用題目的語言寫
- 只輸出 JSON"""

LANGUAGE = {"zh": "繁體中文（台灣用語），專有名詞保留英文", "en": "英文"}

TYPE_RULES = {
    "fact": "出一題「事實查找」題：答案明確寫在段落裡（例如用了什麼資料集、元件叫什麼、做了哪幾步）。",
    "explain": "出一題「理解解釋」題：問「為什麼」或「怎麼做到的」，好的答案要用自己的話解釋原理，不是照抄一句話。",
    "summary": "出一題「總結」題：問這篇論文的主要問題、方法、貢獻。key_points 要涵蓋問題、方法、主要發現。",
    "compare": "出一題「跨篇比較」題，比較的面向：{hint}。題目要用論文的方法名稱，答案必須用到每一篇的內容，evidence 每一篇至少一句。",
    "numeric": "出一題「數字」題：問這篇論文自己的方法在實驗裡的某個結果數字（準確率、提升幾個百分點、壓縮比例等），題目要寫清楚方法、資料集、比較的條件。key_points 寫出正確的數字和它的條件。只能用段落裡真的出現的數字。",
}

SCHEMA = '{"question": "...", "evidence": [{"paper": "論文簡稱", "quote": "原句"}], "key_points": ["..."]}'


def ask(prompt: str) -> dict:
    response = client.chat.completions.create(
        model=MODEL,
        temperature=0.7,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(response.choices[0].message.content)


def passages_block(items: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"【{name}】\n{text}" for name, text in items)


def verify_quotes(item: dict, papers: dict[str, Paper]) -> list[str]:
    # 每一句證據都回論文全文比對，找不到的列出來（出題的 AI 可能改寫或捏造）
    # 用 match_key 比對：忽略標點、空白、PDF 換行造成的斷字
    problems = []
    if not item.get("evidence"):
        problems.append("沒有附證據")
    for ev in item.get("evidence", []):
        paper = papers.get(ev.get("paper"))
        if paper is None:
            problems.append(f"沒有這篇論文：{ev.get('paper')}")
        elif match_key(ev.get("quote", "")) not in match_key(paper.text):
            problems.append(f"{ev['paper']} 裡找不到這句：{ev.get('quote', '')[:60]}")
    return problems


def generate_one(qtype, language, sources, papers, hint=None):
    # 回傳一題；段落不適合出題時回傳 None
    names = "、".join(sorted({name for name, _ in sources}))
    rules = COMMON_RULES.format(language=LANGUAGE[language], paper_names=names)
    task = TYPE_RULES[qtype].format(hint=hint)
    prompt = f"{rules}\n\n{task}\n\n輸出格式：{SCHEMA}\n\n段落：\n\n{passages_block(sources)}"
    item = ask(prompt)
    if item.get("skip"):
        return None
    # 驗證失敗就重出一次（大多是 AI 把句子改寫了一點點）
    problems = verify_quotes(item, papers)
    if problems:
        item = ask(prompt + "\n\n注意：上一次的 quote 跟原文對不上，這次請逐字複製一個連續的片段。")
        if item.get("skip"):
            return None
        problems = verify_quotes(item, papers)
    return {
        "type": qtype, "language": language, "should_refuse": False,
        "question": item["question"], "evidence": item["evidence"], "key_points": item["key_points"],
        "source_papers": sorted({name for name, _ in sources}), "problems": problems,
    }


def numeric_chunks(paper: Paper) -> list[str]:
    # 數字多的段落（實驗結果）。表格解析後順序會亂，所以數字題另外標記、分開看分數
    return [c for c in paper.body_chunks if sum(ch.isdigit() for ch in c) > 80]


def embed(texts: list[str]) -> list[list[float]]:
    vectors = []
    for i in range(0, len(texts), 100):
        response = client.embeddings.create(model="text-embedding-3-small", input=texts[i : i + 100])
        vectors += [d.embedding for d in sorted(response.data, key=lambda d: d.index)]
    return vectors


def no_answer_questions(papers: list[Paper], want: int = 10) -> list[dict]:
    # 文件裡沒有答案的題目。每題都實際驗證：用跟系統一樣的方式（轉向量、找最像的段落）
    # 搜整個論文集的「所有段落」（含參考文獻），取最像的 8 段，再請 gpt-4o 判斷能不能回答，確定答不出來才保留
    all_chunks = [(p.name, c) for p in papers for c in chunker.chunk_text(p.text)]
    chunk_vectors = embed([c for _, c in all_chunks])
    abstracts = passages_block([(p.name, p.body_chunks[0][:1200]) for p in papers])
    prompt = f"""你在幫一個 RAG 問答系統建立評估資料集。下面是知識庫裡 21 篇論文的開頭。
請出 20 題「這些論文都沒有答案」的問題，用來測系統會不會老實說找不到：
- 10 題英文、10 題繁體中文（台灣用語，專有名詞保留英文）
- 要看起來跟這些論文相關，使用者真的可能會問；題目要寫出方法或論文名稱
- 好的方向：問某篇論文沒有做的實驗、沒有測的情境、沒有報告的細節（例如部署成本、在某個沒用到的語言或領域的表現、跟某個沒比較過的方法的比較）
- 不能是靠常識就能判斷沒答案的
輸出 JSON：{{"questions": [{{"question": "...", "language": "en 或 zh"}}]}}

{abstracts}"""
    candidates = ask(prompt)["questions"]
    question_vectors = embed([q["question"] for q in candidates])
    kept = []
    for q, qv in zip(candidates, question_vectors, strict=True):
        # 餘弦相似度：OpenAI 的向量長度都是 1，直接內積就是相似度
        scored = sorted(((sum(a * b for a, b in zip(qv, cv, strict=True)), i) for i, cv in enumerate(chunk_vectors)), reverse=True)
        top = [all_chunks[i] for _, i in scored[:8]]
        verdict = ask(f"""問題：{q["question"]}

下面是從知識庫搜尋到最相關的 8 段。判斷這些段落能不能回答這個問題（完整或部分回答都算能）。
輸出 JSON：{{"answerable": true 或 false, "reason": "一句話說明"}}

{passages_block(top)}""")
        status = "保留" if not verdict["answerable"] else "丟掉（有答案）"
        print(f"   [沒有答案的候選] {status}：{q['question'][:60]}｜{verdict['reason'][:60]}")
        if not verdict["answerable"]:
            kept.append({
                "type": "no_answer", "language": q["language"], "should_refuse": True, "question": q["question"],
                "evidence": [], "key_points": [f"應該說明文件中找不到。驗證時的判斷：{verdict['reason']}"],
                "source_papers": [], "problems": [],
            })
        if len(kept) >= want:
            break
    return kept


def main():
    papers = load_papers()
    by_name = {p.name: p for p in papers}
    items = []
    # 大約三分之一用中文問：輪流分配
    languages = iter(["zh", "en", "en"] * 50)

    def add(item):
        item["id"] = f"q{len(items) + 1:03d}"
        items.append(item)
        flag = " ⚠ " + "；".join(item["problems"]) if item["problems"] else ""
        print(f"{item['id']} {item['type']:9} {item['language']} {item['question'][:70]}{flag}")

    def try_chunks(qtype, paper, pick, tries=4):
        # 段落不適合出題時（AI 回 skip），換一段再試
        language = next(languages)
        for _ in range(tries):
            item = generate_one(qtype, language, [(paper.name, pick())], by_name)
            if item is not None:
                add(item)
                return
        print(f"   {paper.name} 試了 {tries} 段都不適合出 {qtype} 題，跳過")

    # 每篇一題事實題（從正文挑，跳過第一段標題作者）
    for p in papers:
        try_chunks("fact", p, lambda p=p: random.choice(p.body_chunks[1:]))
    # 18 篇各一題解釋題（方法部分：正文的前半段）
    for p in random.sample(papers, 18):
        try_chunks("explain", p, lambda p=p: random.choice(p.body_chunks[1 : len(p.body_chunks) // 2 + 2]))
    # 8 篇總結題：開頭 + 前言 + 結尾的段落
    for p in random.sample(papers, 8):
        sources = [(p.name, p.body_chunks[0]), (p.name, p.body_chunks[1]), (p.name, p.body_chunks[-1])]
        item = generate_one("summary", next(languages), sources, by_name)
        if item:
            add(item)
    # 跨篇比較：每篇給開頭和一段方法
    for names, hint in COMPARE_GROUPS:
        sources = []
        for name in names:
            body = by_name[name].body_chunks
            sources += [(name, body[0]), (name, random.choice(body[1 : len(body) // 2 + 2]))]
        item = generate_one("compare", next(languages), sources, by_name, hint)
        if item:
            add(item)
    # 6 題數字題
    numeric_papers = [p for p in papers if numeric_chunks(p)]
    for p in random.sample(numeric_papers, 6):
        try_chunks("numeric", p, lambda p=p: random.choice(numeric_chunks(p)))
    # 10 題沒有答案的（驗證過）
    for item in no_answer_questions(papers):
        add(item)

    OUT.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n", encoding="utf-8")
    print(f"\n共 {len(items)} 題，存到 {OUT}；證據對不上原文的：{sum(1 for i in items if i['problems'])} 題")


if __name__ == "__main__":
    main()
