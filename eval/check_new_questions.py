# 新增題目的品質檢查（docs/decisions/031）：題數湊齊不代表題目是好的，新題目要先檢查過才能拿來量東西
#
# 用法（專案資料夾）：
#   .venv\Scripts\python -m eval.check_new_questions --corpus enterprise --only g09 g10 v07 ...
#
# 兩項檢查：
#   1. 關鍵字：「一定要提到」「加分」的數字和用詞，真的出現在題目「應該引用」的文件裡
#      （中文數字先轉成阿拉伯數字再比；沒有數字、要靠意思判斷的列出來，由盲答和人工確認）
#   2. 盲答：只給題目和「應該引用」的文件全文，不給標準答案，請 gpt-4o 回答，再用評審對清單
#      答得跟標準答案不一樣的列出來，逐題看是誰錯
# 另外要搭配 python -m eval.review_docs --corpus <corpus> --all-docs --only ...：檢查別的文件會不會改變答案

import argparse
import json

# eval.run 一定要比 app 先 import：它一 import 就把資料庫換成臨時的
# isort: off
from eval.run import EVAL, judge
# isort: on
from openai import OpenAI

from app.core.config import settings
from eval.common import CORPORA, normalize, numbers
from eval.expand_docs import find_doc
from eval.review_docs import ASKER

BLIND_PROMPT = """{asker}
以下是知識庫裡的文件，只能根據這些文件回答問題；答案會因情況而不同時，列出各種情況。

{docs}

問題：{question}"""


def source_text(corpus: str, sources: list[str]) -> dict[str, str]:
    codes = dict.fromkeys(source.split(" ")[0] for source in sources)
    return {code: find_doc(corpus, code).read_text(encoding="utf-8") for code in codes}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True, choices=["enterprise", "school"])
    parser.add_argument("--only", nargs="+", required=True, help="要檢查的題號")
    args = parser.parse_args()
    questions = [json.loads(line) for line in (EVAL / args.corpus / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line]
    questions = [q for q in questions if q["id"] in args.only]
    client = OpenAI(api_key=settings.openai_api_key)

    problems, manual, disagreements = [], [], 0
    for q in questions:
        docs = source_text(args.corpus, q["sources"])
        # 1. 關鍵字
        text = normalize("".join(docs.values()))
        for item in q["must_include"] + q.get("nice_to_have", []):
            target = normalize(item)
            if target in text or (numbers(target) and numbers(target) <= numbers(text)):
                continue
            (problems if numbers(target) else manual).append(f"{q['id']}「{item}」")
        if q["should_refuse"]:
            continue
        # 2. 盲答
        prompt = BLIND_PROMPT.format(asker=ASKER[args.corpus], question=q["question"],
                                     docs="\n\n".join(f"【{code}】\n{body}" for code, body in docs.items()))
        answer = client.chat.completions.create(
            model="gpt-4o", temperature=0, messages=[{"role": "user", "content": prompt}],
        ).choices[0].message.content
        verdict = judge(client, CORPORA[args.corpus]["domain"], q, answer, [])
        missing = [v["item"] for v in verdict.get("must_include", []) if not v.get("present")]
        wrong = [v["item"] for v in verdict.get("must_not", []) if v.get("present")]
        if missing or wrong:
            disagreements += 1
            print(f"\n✗ {q['id']} {q['question']}\n  標準答案：{q['answer']}\n  盲答：{answer}\n  缺：{missing}  說錯：{wrong}")
        else:
            print(f"✓ {q['id']} {q['question']}")
    print(f"\n關鍵字不在出處文件裡：{problems or '沒有'}")
    print(f"沒有數字、要靠意思判斷的（由盲答和人工確認）：{manual or '沒有'}")
    print(f"盲答跟標準答案不一致：{disagreements} 題")


if __name__ == "__main__":
    main()
