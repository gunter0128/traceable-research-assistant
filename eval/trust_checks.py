# 已經存下來的評估結果，補上「引用對得上」「不矛盾」兩項檢查（docs/decisions/038）
#
# 用法（專案資料夾，臨時資料庫要先開好，見 eval/run.py）：
#   .venv\Scripts\python -m eval.trust_checks eval\data\results\<檔名>.json [更多檔案] [--consistency]
#   --consistency：另外請評審判斷每個回答前後有沒有矛盾，每 70 題約 0.15 美元
#   轉向量的設定要跟當時評估的一樣：OpenAI 的結果不設 EMBEDDING_*；本機模型的結果要設 EMBEDDING_* 環境變數
#
# 舊的結果沒有存段落原文（之後的評估會存）：用同樣的問題、同樣的轉向量模型再搜一次，把當時給模型看的段落找回來
#   搜尋每次的結果一樣；找回來的文件跟當時存的不一樣的話，那一題不檢查引用，最後列出有幾題
# 結果存成 <原檔名>_checks.json，不改原本的檔案；--consistency 另外產生抽查用的 eval/data/consistency_review.md

import argparse
import importlib
import json
import random
from pathlib import Path

# eval.run 一定要比 app 先 import：它一 import 就把資料庫換成臨時的
# isort: off
from eval.run import EVAL, doc_code, judge_consistency, seen_passage, setup_knowledge_base, trust_fields
# isort: on
from openai import OpenAI

from app.core import chunker, openai_client
from app.core.config import settings
from app.repositories import chunk_repo
from app.services import qa_service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--consistency", action="store_true")
    args = parser.parse_args()
    client = OpenAI(api_key=settings.openai_api_key)
    review = []
    for path in args.files:
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = data["meta"]
        embedding = meta.get("embedding", "text-embedding-3-small")
        if embedding != openai_client.EMBEDDING_MODEL:
            print(f"{path.name}：當時用 {embedding} 轉向量，現在的設定是 {openai_client.EMBEDDING_MODEL}，跳過")
            continue
        passages = find_passages(data) if any("passages" not in r for run in data["runs"] for r in run) else {}
        target = path.with_name(path.stem + "_checks.json")
        # 之前補過的矛盾判斷（花了錢）沿用：這次沒加 --consistency 時，只重算引用，不把它蓋掉
        earlier = {}
        if target.exists():
            earlier = {(r["run"], r["id"]): r for r in json.loads(target.read_text(encoding="utf-8"))["rows"]}
        today = importlib.import_module(f"eval.{meta['corpus']}.rules").TODAY
        out, mismatched = [], 0
        for i, run in enumerate(data["runs"]):
            for r in run:
                texts = r.get("passages")
                if texts is None:
                    texts, docs = passages[r["id"]]
                    if docs != r["retrieved"]:
                        texts, mismatched = None, mismatched + 1
                row = {"run": i, "id": r["id"], "type": r["type"], "passages": texts}
                row.update(trust_fields(r["response"], texts, r["question"], today) if texts is not None
                           else {"citation_checks": None, "citation_ok": None})
                if args.consistency:
                    row.update(judge_consistency(client, r["question"], r["response"]))
                    review.append({"file": path.name, "question": r["question"], "response": r["response"], **row})
                elif "contradiction" in earlier.get((i, r["id"]), {}):
                    old = earlier[(i, r["id"])]
                    row.update(contradiction=old["contradiction"], contradiction_reason=old["contradiction_reason"])
                out.append(row)
        target.write_text(json.dumps({"meta": meta, "rows": out}, ensure_ascii=False, indent=1), encoding="utf-8")
        report(path.name, meta, out, mismatched)
    if review:
        write_review(review)


# 每題用同樣的問題再搜一次：{題號: (段落原文照編號排, 文件代號)}，跟 qa_service.ask_with_passages 同一套步驟（不規劃）
def find_passages(data: dict) -> dict:
    meta = data["meta"]
    chunker.STRATEGY = meta.get("chunker", "fixed")
    db, _, workspace, _ = setup_knowledge_base(meta["corpus"])
    found = {}
    for r in data["runs"][0]:
        searches = [openai_client.PlannedSearch(r["question"])]
        embeddings = openai_client.embed_queries([r["question"]])
        chunks = qa_service._search(db, workspace.id, searches, embeddings, meta["top_k"])
        ordered = [chunk for _, group in qa_service._group_by_document(chunks) for chunk in group]
        headers = chunk_repo.first_chunks(db, list({c.document_id for c in ordered}))
        found[r["id"]] = ([seen_passage(c.document.filename, headers.get(c.document_id, ""), c.content) for c in ordered],
                          sorted({doc_code(c.document.filename) for c in ordered}))
    db.close()
    return found


def report(name: str, meta: dict, rows: list[dict], mismatched: int) -> None:
    def rate(values):
        values = [v for v in values if v is not None]
        return f"{sum(values) / len(values) * 100:.0f}%（{len(values)} 個回答）" if values else "—"

    print(f"\n{name}：{meta['title']}，回答 {meta.get('model')}")
    print("  引用對得上：" + rate([r["citation_ok"] for r in rows]))
    if mismatched:
        print(f"  找回來的段落跟當時不一樣，沒檢查引用：{mismatched} 個回答")
    if rows and "contradiction" in rows[0]:
        print("  不矛盾：" + rate([not r["contradiction"] for r in rows]))


# 抽查用：評審判成矛盾的最多 10 題，加上判成不矛盾的隨機 10 題，人工看評審判得對不對
def write_review(review: list[dict]) -> None:
    rng = random.Random(2026)
    flagged = [r for r in review if r["contradiction"]]
    clean = [r for r in review if not r["contradiction"]]
    picked = rng.sample(flagged, min(10, len(flagged))) + rng.sample(clean, min(10, len(clean)))
    lines = ["# 不矛盾：評審判斷抽查", "",
             f"評審判成矛盾的 {len(flagged)} 個回答裡抽 {min(10, len(flagged))} 個，判成不矛盾的抽 {min(10, len(clean))} 個。",
             "每一題看：評審說的對不對？", ""]
    for i, r in enumerate(picked, start=1):
        verdict = "**矛盾**" if r["contradiction"] else "不矛盾"
        lines += [f"## {i}. {r['id']}（{r['file']}）評審判：{verdict}", "", f"**問題**：{r['question']}", "",
                  "**回答**：", "", r["response"], "", f"**評審的理由**：{r['contradiction_reason']}", "",
                  "你的判斷：□ 評審對　□ 評審錯", "", "---", ""]
    path = EVAL / "data" / "consistency_review.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n抽查用的檔案：{path}")


if __name__ == "__main__":
    main()
