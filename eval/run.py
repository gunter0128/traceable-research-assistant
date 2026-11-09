# 用考卷評估問答系統（docs/decisions/017）
#
# 兩套考卷：enterprise（虛構的青嶼科技規章，docs/decisions/016）、labor（真實的勞動法規，docs/decisions/031）
#
# 用法（專案資料夾）：
#   1. 開一個臨時資料庫（跟 CI 一樣，用完就丟）：
#        docker run -d --rm --name eval-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=eval -p 55433:5432 pgvector/pgvector:pg16
#   2. 產生 PDF（第一次才需要）：.venv\Scripts\python -m eval.build_pdfs --corpus enterprise
#   3. 跑評估：.venv\Scripts\python -m eval.run --corpus enterprise --runs 2 --note "基準"
#        預設只跑練習題（--split dev）；最後確認時才跑 --split test
#        接實驗室的模型：加上 --answer-base-url http://localhost:8000/v1 --model <adapter 名稱>
#   4. 用完關掉資料庫：docker stop eval-pg
#
# 流程：
#   建空的資料表 → 每份 PDF 走系統正常的處理流程（解析、切段、轉向量、存資料庫）
#   → 逐題呼叫 qa_service.ask（跟線上同一份程式碼）→ 評分 → 報表
# 評分：
#   檢索（程式判斷）：該引用的文件有沒有在系統搜到的段落裡
#   引用（程式判斷）：該引用的文件有沒有在答案標的 [1][2] 裡（逐句引用，docs/decisions/021）
#   回答（gpt-4o 對清單）：一定要提到的有沒有提到、不能說的有沒有出現、該拒答的有沒有拒答
# 花費：兩套練習題（94 題）各跑一輪，評審約 0.5 美元（含 --consistency）；回答接自己的模型不用錢

import os

# 一定要在 import app 之前換掉資料庫：.env 裡是正式資料庫，評估絕對不能碰它
os.environ["DATABASE_URL"] = os.environ.get(
    "EVAL_DATABASE_URL", "postgresql+psycopg://postgres:test@localhost:55433/eval"
)

import argparse
import importlib
import json
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
from app.core import openai_client, s3_client
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.repositories import chunk_repo, document_repo, workspace_repo
from app.services import auth_service, document_service, qa_service, usage_service
from eval.common import CORPORA, TYPE_NAMES, citation_checks, evidence_rate

EVAL = Path(__file__).parent
RESULTS = EVAL / "data" / "results"
REPORT = EVAL.parent / "docs" / "eval-log.md"
JUDGE_MODEL = "gpt-4o"
TAIWAN = timezone(timedelta(hours=8))

def doc_code(name: str) -> str:
    # 「HR-02 第 6 條」或「HR-02_員工手冊_2026年版.pdf」→ HR-02；勞動法規的是 L-01
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
# 分開問：上面的量尺一個字都不改，分數可以跟加這項之前的結果互相比
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


# 模型看到的 [n]：檔名、文件開頭（跟 qa_service.build_context 一樣取前面一段），加上那一段的內容
def seen_passage(filename: str, header: str, content: str) -> str:
    return f"{filename}\n{' '.join(header.split())[:qa_service.DOCUMENT_HEADER_CHARS]}\n{content}"


# 引用對得上（eval/common.py 的 citation_checks）：有可以檢查的句子，而且每一句的數字都在標的原文裡；沒有可以檢查的句子是 None
def trust_fields(answer: str, passages: list[str], question: str, today) -> dict:
    checks = citation_checks(answer, passages, f"{question}\n今天日期：{qa_service.date_text(today)}")
    return {"citation_checks": checks, "citation_ok": all(not c["missing"] for c in checks) if checks else None}


# ---------- 一輪：逐題問、評分 ----------
# 題目附的依據原文（目前只有勞動法規有）：{題號: [原文, ...]}，用來算「依據的原文有沒有找到」（docs/decisions/031）
def load_evidence(corpus: str) -> dict[str, list[str]]:
    module = importlib.import_module(f"eval.{corpus}.questions")
    return {qid: [quote for _, _, quote in items] for qid, items in getattr(module, "EVIDENCE", {}).items()}


# 接 Qwen 時的抽字設定：照 Qwen3.5 模型說明的建議，思考和不思考各一組（docs/decisions/034）
# 不給的話 vLLM 用自己的預設（temperature 1、候選字不設限），偶爾會抽到機率很低的字，出現簡體字、亂碼
#   top_k / top_p：只從最可能的前 20 個字、累積機率 0.8（或 0.95）以內挑；presence_penalty：已經出現過的字扣分，減少重複
# 不思考（線上回答）改成 temperature 0（docs/decisions/042）：每次都選機率最高的字，同一題的答案才不會忽對忽錯
QWEN_SAMPLING = {
    "think": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5},
    "no_think": {"temperature": 0.0, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5},
}

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
        except openai_client.OpenAITemporaryError:
            if attempt == ASK_RETRIES - 1:
                raise
            print(f"  （回答的服務連不上，{ASK_RETRY_WAIT} 秒後重試，第 {attempt + 1} 次）")
            time.sleep(ASK_RETRY_WAIT)


def run_once(db, user, workspace, questions, client, domain, today, evidence=None, consistency=False) -> list[dict]:
    rows = []
    for q in questions:
        # 今天日期固定成知識庫設定的「現在」（rules.py 的 TODAY），分數才不會因為哪天跑評估而變
        response, passages, seconds = _ask(db, workspace, user, q["question"], today)
        retrieved = sorted({doc_code(p.filename) for p in passages})
        # 引用：自己從答案裡數 [n]，不看 response.citations（模型忘了標時，系統會退回列出全部搜到的段落，會灌水）
        _, marked = qa_service.extract_citations(response.answer, len(passages))
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
              f"轉向量 {meta['embedding']}｜回答 {meta['model']}｜{len(summaries)} 輪｜評審 {JUDGE_MODEL}"), ""]
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
    parser.add_argument("--corpus", choices=CORPORA, required=True, help="enterprise 企業 / labor 勞動法規")
    parser.add_argument("--model", help="回答用的模型（不指定就用系統現在的 GENERATION_MODEL），用來比較不同模型")
    parser.add_argument("--answer-base-url",
                        help="回答改接這個 OpenAI 格式的服務，例如 http://localhost:8000/v1（實驗室用 vLLM 架的開源模型，"
                             "docs/decisions/034）；轉向量照設定（環境變數 EMBEDDING_BASE_URL 等，docs/decisions/037）")
    parser.add_argument("--answer-timeout", type=float,
                        help="回答每次最多等幾秒（預設看設定的 answer_timeout，OpenAI 是 15 秒）；本機架的模型比較慢時調大")
    parser.add_argument("--split", choices=["dev", "test", "all"],
                        default="dev", help="dev 練習題（平常改進用）/ test 考試題（只在最後確認時跑）/ all 全部")
    parser.add_argument("--consistency", action="store_true",
                        help="另外請評審判斷每個回答前後有沒有矛盾（docs/decisions/038），每 70 題約多 0.15 美元")
    args = parser.parse_args()
    corpus = CORPORA[args.corpus]
    if args.model:
        openai_client.GENERATION_MODEL = args.model
    if args.answer_base_url:
        # 自己架的模型不用付 API 的錢（GPU 的成本另外算），價目表記 0
        openai_client.PRICES_PER_MILLION.setdefault(openai_client.GENERATION_MODEL, (Decimal(0), Decimal(0)))
        # 跟上線一樣：不思考、temperature 0（docs/decisions/036、042）
        extra = {**QWEN_SAMPLING["no_think"], "chat_template_kwargs": {"enable_thinking": False}}
        openai_client.use_answer_endpoint(args.answer_base_url, extra_body=extra)
    if args.answer_timeout:
        openai_client.ANSWER_TIMEOUT = args.answer_timeout
        # 整個問答的時間預算也跟著放寬，生成答案才等得到完整的 answer_timeout（轉向量、搜尋另外要時間，這裡算得比較寬）
        qa_service.ANSWER_BUDGET_SECONDS = max(qa_service.ANSWER_BUDGET_SECONDS, args.answer_timeout * 2 + 20)
    # 評估不受每日用量上限限制：用的是臨時資料庫，而且用貴的模型跑一輪就會超過每人每天 0.5 美元
    usage_service.ensure_within_quota = lambda db, owner_id: None

    questions = load_questions(args.corpus, args.split)
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
            "top_k": qa_service.SEARCH_LIMIT, "model": openai_client.GENERATION_MODEL, "split": args.split,
            "answer_endpoint": args.answer_base_url or settings.answer_base_url or "OpenAI",
            # 轉向量的模型看設定（環境變數 EMBEDDING_BASE_URL 等，docs/decisions/037），預設是 OpenAI
            "embedding": openai_client.EMBEDDING_MODEL,
            "answer_sampling": openai_client.answer_extra_body}
    save_results(meta, summaries, all_rows, tag=meta["commit"])
    db.close()


# 讀題目：split 是 dev（練習題）、test（考試題）或 all（全部）
def load_questions(corpus: str, split: str = "dev") -> list[dict]:
    lines = (EVAL / corpus / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    questions = [json.loads(line) for line in lines if line]
    return questions if split == "all" else [q for q in questions if q["split"] == split]


def current_commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()


# 存逐題明細（eval/data/results/，不進 git），並把摘要加到 docs/eval-log.md 最上面
def save_results(meta: dict, summaries: list[dict], all_rows: list[list[dict]], tag: str) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{datetime.now(TAIWAN):%Y%m%d-%H%M}_{meta['corpus']}_{tag}.json"
    out.write_text(json.dumps({"meta": meta, "summaries": summaries, "runs": all_rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    markdown = report_markdown(summaries, meta)
    header = ("# 評估紀錄\n\n每次跑 eval/run.py 自動加在最上面的原始紀錄，包括試過不用的做法。"
              "整理過的結果見 docs/eval-results.md；逐題的明細在 eval/data/results/（不進 git）。\n\n")
    old = REPORT.read_text(encoding="utf-8").removeprefix(header) if REPORT.exists() else ""
    REPORT.write_text(header + markdown + "\n" + old, encoding="utf-8")
    print("\n" + markdown)
    print(f"逐題明細：{out}")


if __name__ == "__main__":
    main()
