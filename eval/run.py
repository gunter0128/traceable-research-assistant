# 用虛構的知識庫評估問答系統（B4，docs/decisions/017）
#
# 兩套知識庫：enterprise（青嶼科技，平常改進用）、school（澄溪國小，只拿來驗證改進能不能用到別的領域）
#
# 用法（專案資料夾）：
#   1. 開一個臨時資料庫（跟 CI 一樣，用完就丟）：
#        docker run -d --rm --name eval-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=eval -p 55433:5432 pgvector/pgvector:pg16
#   2. 產生 PDF（第一次才需要）：.venv\Scripts\python -m eval.build_pdfs --corpus enterprise
#   3. 跑評估：.venv\Scripts\python -m eval.run --corpus enterprise --runs 2 --note "基準"
#        企業題目預設只跑練習題（--split dev）；最後確認時才跑 --split test
#        學校題目整套都是考試題，預設全部跑
#   4. 用完關掉資料庫：docker stop eval-pg
#
# 流程：
#   建空的資料表 → 每份 PDF 走系統正常的處理流程（解析、切段、轉向量、存資料庫）
#   → 逐題呼叫 qa_service.ask（跟線上同一份程式碼）→ 評分 → 報表
# 評分：
#   檢索（程式判斷）：該引用的文件有沒有在系統搜到的段落裡
#   引用（程式判斷）：該引用的文件有沒有在答案標的 [1][2] 裡（逐句引用，docs/decisions/021）
#   回答（gpt-4o 對清單）：一定要提到的有沒有提到、不能說的有沒有出現、該拒答的有沒有拒答
# 花費：企業 58 題每跑一輪，系統回答約 0.04 美元、評審約 0.3 美元；學校 21 題約三分之一

import os

# 一定要在 import app 之前換掉資料庫：.env 裡是正式資料庫，評估絕對不能碰它
os.environ["DATABASE_URL"] = os.environ.get(
    "EVAL_DATABASE_URL", "postgresql+psycopg://postgres:test@localhost:55433/eval"
)

import argparse
import importlib
import json
import random
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from openai import OpenAI
from sqlalchemy import text

# 載入所有資料表的定義，create_all 才建得出來
from app import models  # noqa: F401
from app.core import chunker, openai_client, s3_client
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.repositories import chunk_repo, document_repo, workspace_repo
from app.services import auth_service, document_service, qa_service, usage_service
from eval.common import CORPORA, TYPE_NAMES, citation_checks, evidence_rate

EVAL = Path(__file__).parent
RESULTS = EVAL / "data" / "results"
REPORT = EVAL.parent / "docs" / "eval-results.md"
JUDGE_MODEL = "gpt-4o"
TAIWAN = timezone(timedelta(hours=8))

def doc_code(name: str) -> str:
    # 「HR-02 第 6 條」或「HR-02_員工手冊_2026年版.pdf」→ HR-02；學校的是 S-01
    return re.match(r"[A-Z]+-\d+", name).group()


# ---------- 準備：建表、放進知識庫的文件 ----------
def setup_knowledge_base(corpus: str):
    folder = EVAL / corpus
    pdfs = {pdf.name: pdf.read_bytes() for pdf in sorted((folder / "pdfs").glob("*.pdf"))}
    expected = len(list((folder / "docs").glob("*.md")))
    if len(pdfs) != expected:
        raise SystemExit(f"{corpus}/pdfs/ 裡應該有 {expected} 份 PDF，現在有 {len(pdfs)} 份。"
                         f"先跑 python -m eval.build_pdfs --corpus {corpus}")
    return build_knowledge_base(pdfs, CORPORA[corpus]["workspace"])


# 建表、把 PDF 一份一份走系統正常的處理流程放進知識庫；訓練資料也用這個（train/make_answers.py）
def build_knowledge_base(pdfs: dict[str, bytes], workspace_name: str):
    # 本機模式：沒有 SQS，上傳後直接在同一個流程裡處理（docs/decisions/003）
    settings.sqs_queue_url = None
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # 檔案不放 S3：處理時「從 S3 下載」改成直接讀本機的 PDF，其他步驟（解析、切段、轉向量、存資料庫）都跟線上一樣
    s3_client.download_file = lambda key: pdfs[key]

    db = SessionLocal()
    user = auth_service.register_user(db, "eval@example.com", "eval-password")
    workspace = workspace_repo.create(db, user.id, workspace_name)
    started = time.perf_counter()
    for name in pdfs:
        document = document_repo.create(db, workspace.id, name, name)
        document_service.process_document(db, document)
        if document.status != "processed":
            raise SystemExit(f"{name} 處理失敗：{document.error_message}")
    chunks = db.execute(text("SELECT count(*) FROM chunks")).scalar()
    print(f"知識庫：{len(pdfs)} 份文件、{chunks} 段，處理花了 {time.perf_counter() - started:.1f} 秒")
    return db, user, workspace, chunks


# ---------- 評審：gpt-4o 對清單 ----------
JUDGE_PROMPT = """你是問答系統的評審。系統回答的是{domain}的問題，你要逐項檢查系統的回答。

判斷原則：
- 看「意思」不看字面：同一件事換個說法也算有提到；數字必須一致（14 天和 15 天是不同的事）
- 「不能說的錯誤說法」：系統把它當成問題的答案，或當成適用於提問的人、現在有效的規定，才算出現。以下不算：
  - 系統明確標出那是別的對象（例如別的公司、職級、地點）的規定，跟適用的答案分開列出
  - 系統明確說明那是舊版、已經廢止、還沒生效、草案或沒有通過的內容
  - 只是出現同一個數字，整句的意思不同（例如把兩種假分開說明之後再加總，不等於說其中一種假有那麼多天）
- 回答前後矛盾時（例如開頭說「可以」，後面又說不符合條件），以開頭的結論為準：使用者通常先看結論
- 「拒答」：系統明確表示文件中找不到、沒有相關資訊、無法回答

問題：{question}

標準答案（參考用）：{answer}

系統的回答：
{response}

系統引用的文件：{cited}

請逐項判斷，先寫理由再下判斷，item 照下面列的原文抄、順序一樣，沒有列出的項目不要自己加，輸出 JSON：
{{
  "must_include": [{{"item": "原文", "reason": "一句話", "present": true/false}}],
  "nice_to_have": [{{"item": "原文", "reason": "一句話", "present": true/false}}],
  "must_not": [{{"item": "原文", "reason": "一句話", "present": true/false}}],
  "refused": true/false
}}

一定要提到的：{must_include}
有提到更好的：{nice_to_have}
不能說的：{must_not}"""


def judge(client: OpenAI, domain: str, q: dict, response: str, cited: list[str]) -> dict:
    prompt = JUDGE_PROMPT.format(
        domain=domain, question=q["question"], answer=q["answer"], response=response, cited="、".join(cited) or "（無）",
        must_include=json.dumps(q["must_include"], ensure_ascii=False),
        nice_to_have=json.dumps(q.get("nice_to_have", []), ensure_ascii=False),
        must_not=json.dumps(q["must_not"], ensure_ascii=False),
    )
    # 評審偶爾會漏掉整個欄位（例如沒寫 refused），整輪評估會在最後一題前中斷：再問一次，問 3 次都缺才報錯
    for _ in range(3):
        result = client.chat.completions.create(
            model=JUDGE_MODEL, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        verdict = json.loads(result.choices[0].message.content)
        if "refused" in verdict:
            return verdict
    raise ValueError(f"評審連續 3 次沒有回 refused：{q['id']}")


# ---------- 不矛盾（docs/decisions/038）----------
# 上面的評審遇到前後矛盾時以開頭的結論為準：開頭對了，後面自相矛盾也不會扣分
# 這裡另外問一次，只判斷回答本身有沒有矛盾，不管內容對不對
# 分開問：上面的量尺一個字都不改，之前存下來的回答也能補評（eval/trust_checks.py），分數可以互相比
CONSISTENCY_PROMPT = """你是問答系統的評審。這次只判斷一件事：系統的回答本身有沒有前後矛盾。

算矛盾：
- 同一件事給了互相衝突的結論，例如開頭說「可以」，後面又說不符合條件、不能申請
- 同一件事前後給了不同的數字，例如前面說 14 天、後面說 15 天
- 回答到一半推翻自己前面的說法（例如「更正：……」「前面說錯了」）

不算矛盾：
- 分別說明不同對象、情況、版本、公司的規定，例如「正職 14 天，約聘 10 天」
- 說明舊版、草案、別的單位的規定，並講清楚為什麼不適用
- 先講原則，再講例外或但書

只看回答本身，不用判斷內容對不對，也不用管它有沒有回答到問題。

問題：{question}

系統的回答：
{response}

先寫理由再下判斷，輸出 JSON：{{"reason": "一句話", "contradiction": true/false}}"""


def judge_consistency(client: OpenAI, question: str, response: str) -> dict:
    result = client.chat.completions.create(
        model=JUDGE_MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": CONSISTENCY_PROMPT.format(question=question, response=response)}],
    )
    verdict = json.loads(result.choices[0].message.content)
    return {"contradiction": bool(verdict.get("contradiction")), "contradiction_reason": verdict.get("reason", "")}


# 模型看到的 [n]：檔名、文件開頭（跟 qa_service._build_context 一樣取前面一段），加上那一段的內容
def seen_passage(filename: str, header: str, content: str) -> str:
    return f"{filename}\n{' '.join(header.split())[:qa_service.DOCUMENT_HEADER_CHARS]}\n{content}"


# 引用對得上（eval/common.py 的 citation_checks）：有可以檢查的句子，而且每一句的數字都在標的原文裡；沒有可以檢查的句子是 None
def trust_fields(answer: str, passages: list[str], question: str, today) -> dict:
    checks = citation_checks(answer, passages, f"{question}\n今天日期：{qa_service._date_text(today)}")
    return {"citation_checks": checks, "citation_ok": all(not c["missing"] for c in checks) if checks else None}


# ---------- 一輪：逐題問、評分 ----------
# 題目附的依據原文（目前只有勞動法規有）：{題號: [原文, ...]}，用來算「依據的原文有沒有找到」（docs/decisions/031）
def load_evidence(corpus: str) -> dict[str, list[str]]:
    module = importlib.import_module(f"eval.{corpus}.questions")
    return {qid: [quote for _, _, quote in items] for qid, items in getattr(module, "EVIDENCE", {}).items()}


# 接 Qwen 時的抽字設定：照 Qwen3.5 模型說明的建議，思考和不思考各一組（docs/decisions/034）
# 不給的話 vLLM 用自己的預設（temperature 1、候選字不設限），偶爾會抽到機率很低的字，出現簡體字、亂碼
#   top_k / top_p：只從最可能的前 20 個字、累積機率 0.8（或 0.95）以內挑；presence_penalty：已經出現過的字扣分，減少重複
QWEN_SAMPLING = {
    "think": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5},
    "no_think": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5},
}

# 理想回答的寫法（docs/decisions/038），接在回答原則第 7 條後面，只在評估時加（--ideal-rules），線上的回答原則不動
# 用途：量「不訓練、只改規定」能做到多少；也是寫訓練資料時給示範答案的規定
# 跟回答原則一樣只放通用的寫法，不放任何領域的詞（docs/decisions/019）
IDEAL_RULES = """
8. 第一句直接給結論，不要先鋪陳背景或重述問題。
9. 用自己的話整理重點，不要整段照抄資料的原文；同一件事只講一次。
10. 結論只有一個：不要寫到一半改口或更正自己，前後的數字要一致。
11. 只詳細說明適用於提問者的規定；不適用的（舊版、草案、還沒生效、別的單位），用一句話說明為什麼不適用，不要列出它的內容。
12. 問題沒說明對象或情況、答案又因此不同時，每種情況各用一句話說明。
13. 每一句標的段落編號，那一段裡要真的寫著這句話講的內容。
14. 整個回答盡量在 150 字以內，需要分情況時可以稍長；不要加「結論」「說明」這類標題。"""

# 把回答原則裡不夠精準的地方講清楚（--precise-rules），只在評估時加，線上的回答原則不動
# 來源：訓練後的回答在練習題上錯在哪（選錯適用的規定、漏了限制、說明不適用的句子沒標引用、依據那一行時有時無）
# 跟回答原則一樣只放通用的寫法，不放任何領域的詞（docs/decisions/019）
PRECISE_RULES = """
8. 下結論前，先找出規定寫的適用條件（對象、身分、單位、職級、期間、生效或廢止日期），逐一對照提問者的情況和今天的日期；條件不符合的規定，不能當成答案。
9. 規定裡有限制或例外時（例如某些人不適用、不給薪、有上限），也要一起說。
10. 每一句講到資料內容的句子都要標段落編號，包括說明某條規定不適用、已廢止、還沒生效的句子；只有最後的依據那一行不用標。
11. 回答的最後一定要另起一行，寫「依據：文件名稱 條次」；說找不到相關資訊時不用寫。"""
# 問題後面的提醒也一起改：模型對離問題最近的指示比較會照做（docs/decisions/021）
PRECISE_REMINDER = "（回答時，每一句講到資料內容的句子，句尾都要標上依據的段落編號，例如 [1]；說明某條規定不適用的句子也要標。）"


# 回答的服務暫時連不上時（例如接實驗室自己架的模型，SSH 轉接斷線），等一下再試，最多等約 5 分鐘
# 不然斷一下整輪評估就中止了（docs/decisions/034）。秒數只算成功的那一次
ASK_RETRIES = 10
ASK_RETRY_WAIT = 30


def _ask(db, workspace, user, question, today):
    for attempt in range(ASK_RETRIES):
        started = time.perf_counter()
        try:
            response, passages = qa_service.ask_with_passages(db, workspace.id, user.id, question, today=today)
            return response, passages, time.perf_counter() - started
        except qa_service.AIServiceUnavailable:
            if attempt == ASK_RETRIES - 1:
                raise
            print(f"  （回答的服務連不上，{ASK_RETRY_WAIT} 秒後重試，第 {attempt + 1} 次）")
            time.sleep(ASK_RETRY_WAIT)


def run_once(db, user, workspace, questions, client, domain, today, evidence=None, consistency=False) -> list[dict]:
    rows = []
    for q in questions:
        # 今天日期固定成知識庫設定的「現在」（2026-10-20），分數才不會因為哪天跑評估而變
        response, passages, seconds = _ask(db, workspace, user, q["question"], today)
        retrieved = sorted({doc_code(p.filename) for p in passages})
        # 引用：自己從答案裡數 [n]，不看 response.citations（模型忘了標時，系統會退回列出全部搜到的段落，會灌水）
        _, marked = qa_service._extract_citations(response.answer, len(passages))
        cited = sorted({doc_code(p.filename) for p in passages if p.number in marked})
        quotes = (evidence or {}).get(q["id"], [])
        texts = [p.content for p in passages]
        row = score(client, domain, q, response.answer, retrieved, cited, seconds, evidence_rate(quotes, texts))
        # 模型當時看到的每個 [n] 也存下來：之後要逐句檢查引用、補新的檢查，不用重跑
        headers = chunk_repo.first_chunks(db, list({p.document_id for p in passages}))
        row["passages"] = [seen_passage(p.filename, headers.get(p.document_id, ""), p.content) for p in passages]
        row.update(trust_fields(response.answer, row["passages"], q["question"], today))
        if consistency:
            row.update(judge_consistency(client, q["question"], response.answer))
        rows.append(row)
    return rows


# 評一題：retrieved 是搜到的文件代號，cited 是答案標出來的文件代號
# 抽出來是為了讓別的比較對象（例如 OpenAI 的 file search，eval/openai_baseline.py）用同一套標準打分
# evidence：依據的原文找到了幾成（0～1），題目沒有附原文的是 None
def score(client, domain, q, answer, retrieved, cited, seconds, evidence=None) -> dict:
    required = sorted({doc_code(s) for s in q["sources"]})
    verdict = judge(client, domain, q, answer, cited)
    included = _align(q["must_include"], verdict.get("must_include", []))
    bonus = _align(q.get("nice_to_have", []), verdict.get("nice_to_have", []))
    violated = _align(q["must_not"], verdict.get("must_not", []))
    refused = bool(verdict["refused"])
    row = {
        "id": q["id"], "type": q["type"], "question": q["question"], "response": answer,
        "seconds": round(seconds, 2), "retrieved": retrieved, "cited": cited, "required": required,
        # 檢索完整（Full Hit）：該引用的文件全部都搜到了。沒有答案的題目不算
        "full_hit": None if q["should_refuse"] else all(r in retrieved for r in required),
        # 依據的原文全部找到：長文件上，找到那份文件不代表找到答案的那一條（docs/decisions/031）
        "evidence_rate": evidence,
        "evidence_full": None if evidence is None else evidence == 1,
        # 引用完整：該引用的文件，答案全部都有標出來。沒有答案的題目，要「一個都沒標」才算對
        "cite_full": not cited if q["should_refuse"] else all(r in cited for r in required),
        "include_rate": sum(included) / len(included) if included else None,
        # 加分項有提到幾成（不影響全對，docs/decisions/030）
        "bonus_rate": sum(bonus) / len(bonus) if bonus else None,
        "misuse": any(violated),
        "refused": refused,
        "refusal_ok": refused == q["should_refuse"],
        "verdict": verdict,
    }
    # 全對：重點全提到、沒有錯誤說法、拒答正確
    row["correct"] = row["refusal_ok"] and not row["misuse"] and (row["include_rate"] in (None, 1.0))
    mark = "✓" if row["correct"] else "✗"
    print(f"  {mark} {q['id']} {q['question'][:30]:32} 檢索 {'完整' if row['full_hit'] else ('—' if row['full_hit'] is None else '缺')}"
          f"{'  誤用' if row['misuse'] else ''}{'  拒答' if refused else ''}")
    return row


# 評審偶爾會自己多寫一項、漏寫一項或改寫項目的文字（954 次裡有 15 次，docs/decisions/030）
# 只採用題目裡有的項目：先照原文對回去，對不到、但數量一樣的話照順序對；都對不到就當作 false
def _align(expected: list[str], judged: list[dict]) -> list[bool]:
    by_text = {v.get("item"): v for v in judged}
    aligned = []
    for i, item in enumerate(expected):
        v = by_text.get(item) or (judged[i] if len(judged) == len(expected) else None)
        aligned.append(bool(v and v.get("present")))
    return aligned


def summarize(rows: list[dict]) -> dict:
    def rate(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 3) if values else None

    def stats(group):
        answerable = [r for r in group if r["full_hit"] is not None]
        wrong = [r for r in answerable if not r["correct"]]
        return {
            "n": len(group),
            "correct": rate([r["correct"] for r in group]),
            "full_hit": rate([r["full_hit"] for r in group]),
            "evidence_full": rate([r.get("evidence_full") for r in group]),
            "cite_full": rate([r.get("cite_full") for r in group]),
            "misuse": rate([r["misuse"] for r in group]),
            "include": rate([r["include_rate"] for r in group]),
            # 可信、可追蹤（docs/decisions/038）：之前的結果沒有這兩項，是 None
            "citation_ok": rate([r.get("citation_ok") for r in group]),
            "consistent": rate([None if "contradiction" not in r else not r["contradiction"] for r in group]),
            "refusal_ok": rate([r["refusal_ok"] for r in group]),
            # 雙層分析（取自我的論文）：答錯的題目，是文件沒搜到（取得層），還是搜到了還答錯（使用層）
            "fail_acquisition": sum(1 for r in wrong if not r["full_hit"]),
            "fail_usage": sum(1 for r in wrong if r["full_hit"]),
        }

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    return {"all": stats(rows), **{t: stats(by_type[t]) for t in TYPE_NAMES if t in by_type}}


def pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


def report_markdown(summaries: list[dict], meta: dict) -> str:
    lines = [f"## {meta['time']}　{meta['title']}　{meta['note']}", "",
             (f"題目 {meta['split']}｜commit `{meta['commit']}`｜{meta['chunks']} 段｜搜尋前 {meta['top_k']} 段｜"
              # OpenAI 的 file search 沒有「規劃」這個設定（它自己改寫查詢），meta 裡沒有就不寫
              + (f"規劃 {meta['planner']}｜" if "planner" in meta else "")
              + (f"切段 {meta['chunker']}｜" if meta.get("chunker", "fixed") != "fixed" else "")
              + (f"轉向量 {meta['embedding']}｜" if meta.get("embedding", "text-embedding-3-small") != "text-embedding-3-small" else "")
              + f"回答 {meta.get('model', '—')}"
              + {True: "（思考）", False: "（不思考）"}.get(meta.get("thinking"), "")
              + ("｜加上理想回答的規定" if meta.get("ideal_rules") else "")
              + ("｜加上精準的規定" if meta.get("precise_rules") else "")
              + f"｜{len(summaries)} 輪｜評審 {JUDGE_MODEL}"), ""]
    lines += ["| 題型 | 題數 | 全對 | 檢索完整 | 引用完整 | 誤用 | 重點涵蓋 | 拒答正確 | 答錯：沒搜到 / 搜到還錯 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for key in ["all", *TYPE_NAMES]:
        if key not in summaries[0]:
            continue
        cells = []
        for metric in ("correct", "full_hit", "cite_full", "misuse", "include", "refusal_ok"):
            cells.append(" / ".join(pct(s[key].get(metric)) for s in summaries))
        fails = " ； ".join(f"{s[key]['fail_acquisition']} / {s[key]['fail_usage']}" for s in summaries)
        name = "**全部**" if key == "all" else TYPE_NAMES[key]
        lines.append(f"| {name} | {summaries[0][key]['n']} | {' | '.join(cells)} | {fails} |")
    if summaries[0]["all"].get("evidence_full") is not None:
        lines += ["", "依據的原文全部找到：" + " / ".join(pct(s["all"]["evidence_full"]) for s in summaries)
                  + "（「檢索完整」只看找到那份文件；長文件要看有沒有找到答案的那一條）"]
    if summaries[0]["all"].get("citation_ok") is not None:
        lines += ["", "引用對得上：" + " / ".join(pct(s["all"]["citation_ok"]) for s in summaries)
                  + "（有數字、有標 [n] 的句子，數字都出現在標的那段原文裡）"]
    if summaries[0]["all"].get("consistent") is not None:
        lines += ["", "不矛盾：" + " / ".join(pct(s["all"]["consistent"]) for s in summaries) + "（另外請評審判斷回答前後有沒有矛盾）"]
    lines += ["", "跑了多輪時，每格依序是每一輪的結果（用來看隨機造成的波動）。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="同樣的設定跑幾輪（看分數的隨機波動）")
    parser.add_argument("--note", default="", help="這次評估的說明，例如「基準」「改 prompt」")
    parser.add_argument("--corpus", choices=CORPORA, required=True, help="enterprise 企業 / school 學校 / labor 勞動法規")
    parser.add_argument("--model", help="回答用的模型（不指定就用系統現在的 GENERATION_MODEL），用來比較不同模型")
    parser.add_argument("--answer-base-url",
                        help="回答改接這個 OpenAI 格式的服務，例如 http://localhost:8000/v1（實驗室用 vLLM 架的開源模型，"
                             "docs/decisions/034）；轉向量照設定（環境變數 EMBEDDING_BASE_URL 等，docs/decisions/037）")
    parser.add_argument("--no-think", action="store_true",
                        help="搭配 --answer-base-url：關掉開源模型的思考模式（Qwen 系列），直接回答")
    parser.add_argument("--answer-timeout", type=float,
                        help="回答每次最多等幾秒（預設看設定的 answer_timeout，OpenAI 是 15 秒）；本機架的模型比較慢時調大")
    parser.add_argument("--top-k", type=int,
                        help="每次搜幾段（不指定就用系統現在的設定 qa_service.SEARCH_LIMIT），用來比較不同段數")
    parser.add_argument("--planner", choices=["none", "catalog", "oracle"],
                        help="搜尋前怎麼規劃（不指定就用系統現在的設定 qa_service.PLANNER，docs/decisions/028）；"
                             "oracle 直接用題目的標準答案指定要去哪幾份文件找，量「找文件做到完美時」的上限（031）")
    parser.add_argument("--oracle-recall", type=float, default=1.0,
                        help="搭配 --planner oracle：每份該用的文件被選到的機率，模擬不完美的規劃器（docs/decisions/033）")
    parser.add_argument("--oracle-wrong", type=int, default=0,
                        help="搭配 --planner oracle：另外多選幾份不相關的文件")
    parser.add_argument("--chunker", choices=["fixed", "structured"],
                        help="切段的方式（不指定就用系統現在的設定 chunker.STRATEGY，docs/decisions/032）")
    parser.add_argument("--split", choices=["dev", "test", "all"],
                        help="dev 練習題（平常改進用）/ test 考試題（只在最後確認時跑）/ all 全部；"
                             "不指定的話：有練習題就只跑練習題，沒有（學校）就全部跑")
    parser.add_argument("--consistency", action="store_true",
                        help="另外請評審判斷每個回答前後有沒有矛盾（docs/decisions/038），每 70 題約多 0.15 美元")
    parser.add_argument("--per-type", type=int,
                        help="每種題型只抽幾題（固定隨機種子，每次抽到的一樣）：很慢的設定先小規模試，例如思考模式")
    parser.add_argument("--ids", nargs="+", help="只跑這幾題（題號），例如只挑真實情境的題目")
    parser.add_argument("--ideal-rules", action="store_true",
                        help="回答原則後面加上理想回答的寫法（IDEAL_RULES，docs/decisions/038），線上的回答原則不動")
    parser.add_argument("--precise-rules", action="store_true",
                        help="回答原則後面加上講得更精準的規定，問題後面的提醒也換掉（PRECISE_RULES），線上的回答原則不動")
    args = parser.parse_args()
    corpus = CORPORA[args.corpus]
    if args.top_k:
        qa_service.SEARCH_LIMIT = args.top_k
    if args.chunker:
        chunker.STRATEGY = args.chunker
    if args.planner:
        qa_service.PLANNER = args.planner
    if args.model:
        openai_client.GENERATION_MODEL = args.model
        # 系統的價目表只有線上用的模型，比較別的模型時補上（每百萬 token 的美元：輸入, 輸出）
        openai_client.PRICES_PER_MILLION.setdefault("gpt-4o-mini", (Decimal("0.15"), Decimal("0.60")))
    if args.answer_base_url:
        # 自己架的模型不用付 API 的錢（GPU 的成本另外算），價目表記 0
        openai_client.PRICES_PER_MILLION.setdefault(openai_client.GENERATION_MODEL, (Decimal(0), Decimal(0)))
        extra = dict(QWEN_SAMPLING["no_think" if args.no_think else "think"])
        if args.no_think:
            extra["chat_template_kwargs"] = {"enable_thinking": False}
        openai_client.use_answer_endpoint(args.answer_base_url, extra_body=extra)
    if args.answer_timeout:
        openai_client.ANSWER_TIMEOUT = args.answer_timeout
        # 整個問答的時間預算也跟著放寬：重試兩次加上轉向量、搜尋
        qa_service.ANSWER_BUDGET_SECONDS = max(qa_service.ANSWER_BUDGET_SECONDS, args.answer_timeout * 2 + 20)
    # 評估不受每日用量上限限制：用的是臨時資料庫，而且用貴的模型跑一輪就會超過每人每天 0.5 美元
    usage_service.ensure_within_quota = lambda db, owner_id: None

    questions, split = load_questions(args.corpus, args.split)
    if args.per_type:
        questions = sample_per_type(questions, args.per_type)
    if args.ids:
        questions = [q for q in questions if q["id"] in set(args.ids)]
    if args.ideal_rules:
        openai_client.ANSWER_RULES += IDEAL_RULES
    if args.precise_rules:
        openai_client.ANSWER_RULES += PRECISE_RULES
        openai_client.CITATION_REMINDER = PRECISE_REMINDER
    if args.planner == "oracle":
        qa_service._plan = oracle_plan(questions, args.oracle_recall, args.oracle_wrong)
    client = OpenAI(api_key=settings.openai_api_key)
    db, user, workspace, chunks = setup_knowledge_base(args.corpus)

    all_rows, summaries = [], []
    for i in range(args.runs):
        print(f"\n第 {i + 1} 輪")
        today = importlib.import_module(f"eval.{args.corpus}.rules").TODAY
        rows = run_once(db, user, workspace, questions, client, corpus["domain"], today, load_evidence(args.corpus),
                        consistency=args.consistency)
        all_rows.append(rows)
        summaries.append(summarize(rows))

    meta = {"time": datetime.now(TAIWAN).strftime("%Y-%m-%d %H:%M"), "note": args.note or "（無說明）",
            "corpus": args.corpus, "title": corpus["title"], "commit": current_commit(), "chunks": chunks,
            "top_k": qa_service.SEARCH_LIMIT, "model": openai_client.GENERATION_MODEL, "split": split,
            "planner": qa_service.PLANNER if args.planner != "oracle" or args.oracle_recall == 1 and not args.oracle_wrong
            else f"oracle（選對 {args.oracle_recall:.0%}、多選 {args.oracle_wrong} 份）",
            "chunker": chunker.STRATEGY,
            "answer_endpoint": args.answer_base_url or settings.answer_base_url or "OpenAI",
            # 轉向量的模型看設定（環境變數 EMBEDDING_BASE_URL 等，docs/decisions/037），預設是 OpenAI
            "embedding": openai_client.EMBEDDING_MODEL,
            # 有沒有開思考模式：接 OpenAI 時沒有這回事；接自己的服務時看送出去的參數（命令列或環境變數設定的）
            "thinking": None if openai_client.answer_client is openai_client.client else
            (openai_client.answer_extra_body or {}).get("chat_template_kwargs", {}).get("enable_thinking", True),
            "ideal_rules": args.ideal_rules,
            "precise_rules": args.precise_rules,
            "answer_sampling": openai_client.answer_extra_body}
    save_results(meta, summaries, all_rows, tag=meta["commit"])
    db.close()


# 完美的規劃器（docs/decisions/031）：換掉 qa_service._plan，參數跟它一樣
# 照題目標準答案的出處（sources），每一份該用的文件各搜一次（只在那份文件裡搜），再跟原本的問題輪流合併
# 量的是「該用的文件一定找得到時，答案最多能好多少」
#
# recall、wrong：模擬「不完美的規劃器」（docs/decisions/033）
#   全部題目該用的文件合起來，剛好拿掉 (1 - recall) 的份數（題目不多，逐份擲骰子會偏離設定的比例），
#   另外每題多選 wrong 份不相關的文件
#   量的是「模型要準到幾成，才換得到分數」。隨機種子固定，每次跑選到的文件都一樣
def oracle_plan(questions: list[dict], recall: float = 1.0, wrong: int = 0):
    by_question = {q["question"]: q for q in questions}
    pairs = sorted((q["id"], code) for q in questions for code in {doc_code(s) for s in q["sources"]})
    random.Random(f"drop-{recall}").shuffle(pairs)
    dropped = set(pairs[:round(len(pairs) * (1 - recall))])

    def plan(db, workspace_id, question, deadline, on_usage):
        filenames = {doc_code(f): f for f, _ in chunk_repo.document_headers(db, workspace_id, 10_000)}
        q = by_question.get(question)
        if q is None:
            return []
        required = sorted({doc_code(s) for s in q["sources"]})
        picked = [code for code in required if (q["id"], code) not in dropped]
        others = sorted(code for code in filenames if code not in required)
        picked += random.Random(f"{q['id']}-wrong-{wrong}").sample(others, min(wrong, len(others)))
        return [openai_client.PlannedSearch(question, filenames[code]) for code in picked]
    return plan


# 讀題目：不指定 split 的話，有練習題就只跑練習題，沒有（學校）就全部跑
def load_questions(corpus: str, split: str | None) -> tuple[list[dict], str]:
    lines = (EVAL / corpus / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    questions = [json.loads(line) for line in lines if line]
    split = split or ("dev" if any(q["split"] == "dev" for q in questions) else "all")
    if split != "all":
        questions = [q for q in questions if q["split"] == split]
    return questions, split


# 每種題型各抽 n 題，題型的比例跟整套一樣；順序照原本的題目順序
def sample_per_type(questions: list[dict], n: int) -> list[dict]:
    rng = random.Random(2026)
    picked = set()
    for qtype in TYPE_NAMES:
        group = [q["id"] for q in questions if q["type"] == qtype]
        picked.update(rng.sample(group, min(n, len(group))))
    return [q for q in questions if q["id"] in picked]


def current_commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()


# 存逐題明細（eval/data/results/，不進 git），並把摘要加到 docs/eval-results.md 最上面
def save_results(meta: dict, summaries: list[dict], all_rows: list[list[dict]], tag: str) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{datetime.now(TAIWAN):%Y%m%d-%H%M}_{meta['corpus']}_{tag}.json"
    out.write_text(json.dumps({"meta": meta, "summaries": summaries, "runs": all_rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    markdown = report_markdown(summaries, meta)
    header = "# 評估結果\n\n每次評估的結果。題目和評分方式見 docs/decisions/016、017；逐題的明細在 eval/data/results/（不進 git）。\n\n"
    old = REPORT.read_text(encoding="utf-8").removeprefix(header) if REPORT.exists() else ""
    REPORT.write_text(header + markdown + "\n" + old, encoding="utf-8")
    print("\n" + markdown)
    print(f"逐題明細：{out}")


if __name__ == "__main__":
    main()
