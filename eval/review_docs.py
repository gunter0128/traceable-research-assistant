# 檢查擴大知識庫時加進來的新文件，會不會改變題目的答案（docs/decisions/026）
#
# 用法（專案資料夾）：.venv\Scripts\python -m eval.review_docs --corpus enterprise
#
# 對每一題（練習題、考試題都檢查）：
#   用向量找出「新文件」裡跟這題最像的 6 段 → 請 gpt-4o 逐段判斷它對這題的影響
#     irrelevant            無關
#     not_applicable        相關但不適用（舊版、子公司、草案、還沒生效、其他對象）：預期的陷阱
#     restates              把現在有效的正確答案再講一遍：問題（「該引用哪份文件」會變得不明確）
#     contradicts           跟正確答案衝突，又沒有版本、日期或適用範圍可以解釋：問題
#     answers_unanswerable  讓「沒有答案」的題目有了答案：問題
#   not_applicable 的段落另外寫出「粗心的人會怎麼答錯」，拿來補進題目的「不能說」
# 結果存在 eval/data/review_<corpus>.json

import argparse
import importlib
import json
import math
import re

from openai import OpenAI

from app.core import chunker
from app.core.config import settings
from eval.expand_docs import EVAL, find_doc

ASKER = {
    "enterprise": "問的人是母公司「青嶼科技股份有限公司」的員工（不是子公司青嶼雲端服務的員工），除非問題裡另外說明。今天是 2026 年 10 月 20 日。",
    "school": "問的人是澄溪國小本校的老師或家長（不是山海分校），除非問題裡另外說明。今天是民國 115 年 10 月 20 日（115 學年度第 1 學期）。",
}

PROMPT = """你在檢查一份問答評估資料集。知識庫裡原本就有這題正確答案的來源；現在新加入了一些文件，要確認新文件不會改變這題的正確答案。

{asker}
題目：{question}
正確答案：{answer}{refuse_note}

以下是新加入的文件裡，跟這題最相關的幾段。請逐段判斷它對這題的影響：
- irrelevant：跟這題無關
- not_applicable：跟這題的主題相關，但不適用於問的人或現在（例如舊版、子公司、草案、尚未生效、其他對象），仔細讀的人不會因此改變答案
- restates：直接講出了這題現在有效的正確答案（等於多了一個正確答案的來源）
- contradicts：跟正確答案衝突，而且段落裡沒有交代版本、日期或適用範圍可以解釋這個衝突
- answers_unanswerable：這題本來應該找不到答案，這段卻提供了答案

判斷 not_applicable 時，要確認段落裡真的看得出它不適用（例如寫了舊的日期、子公司名稱、草案、生效日期在未來）；看不出來的話，判成 contradicts。
對 not_applicable 的段落，再寫出「粗心的人可能因為這段給出的錯誤答案」，用一句很短的話（例如「特休 13 天」），沒有就留空。

段落：
{passages}

輸出 JSON：{{"passages": [{{"index": 1, "verdict": "irrelevant", "reason": "一句話", "careless_answer": ""}}]}}"""

TOP = 6


def new_doc_chunks(corpus):
    catalog = importlib.import_module(f"eval.{corpus}.catalog")
    chunks = []
    for spec in catalog.GENERATED + catalog.DERIVED:
        path = find_doc(corpus, spec["code"])
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].removeprefix("# ")
        # 跟系統一樣切段；切之前去掉 Markdown 的符號，比較接近 PDF 抽出來的文字
        plain = re.sub(r"[#*|>]", " ", text)
        for piece in chunker.chunk_text(plain):
            chunks.append({"code": spec["code"], "title": title, "text": piece})
    return chunks


def embed(client, texts):
    vectors = []
    for start in range(0, len(texts), 100):
        result = client.embeddings.create(model="text-embedding-3-small", input=texts[start:start + 100])
        vectors += [d.embedding for d in result.data]
    # 先把每個向量調成長度 1，之後內積就等於 cosine 相似度
    return [[x / math.sqrt(sum(y * y for y in v)) for x in v] for v in vectors]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True, choices=["enterprise", "school"])
    parser.add_argument("--only", nargs="*", help="只檢查這幾題（題號）")
    args = parser.parse_args()
    client = OpenAI(api_key=settings.openai_api_key)

    questions = [json.loads(line) for line in (EVAL / args.corpus / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]
    chunks = new_doc_chunks(args.corpus)
    chunk_vectors = embed(client, [c["text"] for c in chunks])
    question_vectors = embed(client, [q["question"] for q in questions])
    print(f"新文件切成 {len(chunks)} 段，檢查 {len(questions)} 題")

    results, problems, traps = [], [], []
    for q, qv in zip(questions, question_vectors, strict=True):
        scores = [dot(cv, qv) for cv in chunk_vectors]
        top = sorted(range(len(chunks)), key=lambda j: -scores[j])[:TOP]
        passages = "\n\n".join(f"[{i + 1}] {chunks[j]['code']}（{chunks[j]['title']}）：{chunks[j]['text']}" for i, j in enumerate(top))
        refuse_note = "\n（這題的文件裡沒有答案，正確的回應是說文件中找不到）" if q["should_refuse"] else ""
        response = client.chat.completions.create(
            model="gpt-4o", temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": PROMPT.format(asker=ASKER[args.corpus], question=q["question"], answer=q["answer"],
                                                                refuse_note=refuse_note, passages=passages)}],
        )
        verdicts = json.loads(response.choices[0].message.content)["passages"]
        for v in verdicts:
            j = top[v["index"] - 1]
            v["code"] = chunks[j]["code"]
            v["text"] = chunks[j]["text"][:200]
            if v["verdict"] in ("restates", "contradicts", "answers_unanswerable"):
                problems.append((q["id"], v))
            elif v["verdict"] == "not_applicable" and v.get("careless_answer"):
                traps.append((q["id"], v))
        results.append({"id": q["id"], "question": q["question"], "verdicts": verdicts})
        marks = "".join({"irrelevant": "·", "not_applicable": "○", "restates": "R", "contradicts": "X",
                         "answers_unanswerable": "A"}.get(v["verdict"], "?") for v in verdicts)
        print(f"  {q['id']:4} {marks}  {q['question'][:30]}")

    # 只檢查部分題目時另存一個檔案，不要蓋掉完整的檢查紀錄
    out = EVAL / "data" / f"review_{args.corpus}{'_partial' if args.only else ''}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n問題 {len(problems)} 個（R 重講答案、X 衝突、A 讓沒答案的題目有答案）")
    for qid, v in problems:
        print(f"  {qid} {v['verdict']} {v['code']}：{v.get('reason', '')}\n      段落：{v['text'][:120]}")
    print(f"\n陷阱 {len(traps)} 個（不適用，但粗心的人可能答錯，考慮補進「不能說」）")
    for qid, v in traps:
        print(f"  {qid} {v['code']}：{v['careless_answer']}（{v.get('reason', '')}）")
    print(f"\n明細：{out}")


if __name__ == "__main__":
    main()
