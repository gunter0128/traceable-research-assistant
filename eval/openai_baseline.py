# 拿同一套題目，問 OpenAI 現成的「上傳文件、自動搜尋、回答」服務（file search），跟我們的系統比
# 目的：回答「為什麼不直接用 ChatGPT 就好？」——同樣的文件、同樣的題目、同樣的評審標準
#
# 用法（專案資料夾）：
#   .venv\Scripts\python -m eval.openai_baseline --corpus enterprise --split test --runs 2
#   .venv\Scripts\python -m eval.openai_baseline --corpus school --runs 2
#
# 做法：
#   建一個 OpenAI 的 vector store，把 PDF 傳上去（切段、轉向量都由 OpenAI 用它預設的方式做）
#   → 每題用 Responses API 搭配 file_search 工具回答 → 用 eval/run.py 同一個評審打分
#   → 跑完把 vector store 和上傳的檔案刪掉
# 給模型的指示只有一句話和今天的日期，不放我們系統的 7 條回答原則：要比的是「直接拿現成服務來用」的樣子
# 不需要評估用的資料庫：這裡完全不經過我們的系統

import argparse
import importlib
import time
from datetime import datetime

from openai import OpenAI

from app.core import openai_client
from app.core.config import settings
from eval.common import CORPORA
from eval.run import (
    EVAL,
    TAIWAN,
    current_commit,
    doc_code,
    load_questions,
    save_results,
    score,
    summarize,
)

INSTRUCTIONS = "你是一個根據使用者提供的文件回答問題的助手。今天日期：{today}。"
# --rules：改用我們系統的 7 條回答原則（docs/decisions/019）當指示，排除「只是 prompt 寫得比較好」的可能（027）
RULES_INSTRUCTIONS = "{rules}\n\n今天日期：{today}。"


def upload(client: OpenAI, corpus: str):
    pdfs = sorted((EVAL / corpus / "pdfs").glob("*.pdf"))
    store = client.vector_stores.create(name=f"trace-eval-{corpus}")
    file_ids = []
    for pdf in pdfs:
        with pdf.open("rb") as f:
            uploaded = client.vector_stores.files.upload_and_poll(vector_store_id=store.id, file=f)
        if uploaded.status != "completed":
            raise SystemExit(f"{pdf.name} 上傳後處理失敗：{uploaded.last_error}")
        file_ids.append(uploaded.id)
    print(f"已上傳 {len(pdfs)} 份 PDF 到 OpenAI（vector store {store.id}）")
    return store, file_ids


# 問一題：回傳答案、搜到的文件代號、答案標出來的文件代號
def ask(client: OpenAI, model: str, store_id: str, instructions: str, question: str):
    response = client.responses.create(
        model=model, instructions=instructions, input=question,
        tools=[{"type": "file_search", "vector_store_ids": [store_id]}],
        include=["file_search_call.results"],  # 要回傳搜到的段落，才算得出「檢索完整」
    )
    retrieved, cited = set(), set()
    for item in response.output:
        if item.type == "file_search_call":
            retrieved.update(doc_code(r.filename) for r in item.results or [])
        elif item.type == "message":
            for content in item.content:
                for note in getattr(content, "annotations", None) or []:
                    if note.type == "file_citation":
                        cited.add(doc_code(note.filename))
    return response.output_text, sorted(retrieved), sorted(cited)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, required=True)
    parser.add_argument("--split", choices=["dev", "test", "all"])
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--model", default="gpt-4o", help="回答用的模型，預設跟我們的系統一樣用 gpt-4o，才公平")
    parser.add_argument("--rules", action="store_true", help="給它跟我們系統一樣的 7 條回答原則")
    args = parser.parse_args()
    corpus = CORPORA[args.corpus]
    questions, split = load_questions(args.corpus, args.split)
    today = importlib.import_module(f"eval.{args.corpus}.rules").TODAY
    today_text = f"{today.year} 年 {today.month} 月 {today.day} 日"
    instructions = (RULES_INSTRUCTIONS.format(rules=openai_client.ANSWER_RULES, today=today_text) if args.rules
                    else INSTRUCTIONS.format(today=today_text))
    client = OpenAI(api_key=settings.openai_api_key)

    store, file_ids = upload(client, args.corpus)
    try:
        all_rows, summaries = [], []
        for i in range(args.runs):
            print(f"\n第 {i + 1} 輪")
            rows = []
            for q in questions:
                started = time.perf_counter()
                answer, retrieved, cited = ask(client, args.model, store.id, instructions, q["question"])
                rows.append(score(client, corpus["domain"], q, answer, retrieved, cited, time.perf_counter() - started))
            all_rows.append(rows)
            summaries.append(summarize(rows))
    finally:
        # 用完就刪：不留在 OpenAI 上，也不會累積儲存費用
        client.vector_stores.delete(store.id)
        for file_id in file_ids:
            client.files.delete(file_id)
        print("已刪除 OpenAI 上的 vector store 和檔案")

    note = "對照：OpenAI file search，給同樣的 7 條回答原則" if args.rules else "對照：OpenAI 現成的 file search"
    meta = {"time": datetime.now(TAIWAN).strftime("%Y-%m-%d %H:%M"), "note": note,
            "corpus": args.corpus, "title": f"{corpus['title']}（OpenAI file search）", "commit": current_commit(),
            "chunks": "OpenAI 自己切", "top_k": "OpenAI 預設", "model": f"{args.model}（file search）", "split": split}
    save_results(meta, summaries, all_rows, tag="openai-file-search" + ("-rules" if args.rules else ""))


if __name__ == "__main__":
    main()
