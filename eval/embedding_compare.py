# 挑本機的轉向量模型：只比檢索（docs/decisions/034 階段 0）
#
# 企業內部版的資料不能出公司，轉向量也要換成本機的開源模型。換了之後找到的段落會不一樣，
# 所以要先比：同樣的段落、同樣的題目，開源模型找到的跟 OpenAI 比差多少
# 只比「搜到哪些段落」，不生成答案、不請評審，跟 eval/retrieval.py 一樣便宜
#
# 分兩步，因為模型要在實驗室的 GPU 上跑，但實驗室沒有專案的程式和資料庫：
#   1. export（筆電，專案的 .venv）：用系統自己的流程（抓 PDF 文字 → 切段）切好三套知識庫，
#      連同題目、OpenAI 的向量（基準）存成一個檔案
#        .venv\Scripts\python -m eval.embedding_compare export
#   2. score（實驗室）：每個開源模型把段落和題目轉成向量、找最像的幾段、算分數。這個檔案自己就能跑，不用專案的其他程式
#        python embedding_compare.py score bundle.json
#
# 分數（只算有答案的題目：沒有答案的題目沒有「該找到的文件」）：
#   檢索完整：該用的文件全部出現在找到的段落裡（跟 eval/run.py 同一個定義）
#   原文全部找到：答案依據的條文原文都在找到的段落裡（勞動法規才有，docs/decisions/031）。比檢索完整嚴：找到勞基法不一定找到第 38 條
# 搜尋跟線上一樣：只拿問題本身搜、不規劃，餘弦相似度最高的幾段（跟 pgvector 的 cosine_distance 排序相同）

import argparse
import json
import re
import sys
import time
from pathlib import Path

# 候選模型（2026-10）：可以商用、不用申請就能下載、小到能跟回答模型擠在同一張 3090 上或用 CPU 跑
#   query / document：模型要求的前綴寫法，用 sentence-transformers 設定檔裡的名字；None 代表不加
CANDIDATES = {
    "Qwen/Qwen3-Embedding-0.6B": {"query": "query", "document": None},
    "BAAI/bge-m3": {"query": None, "document": None},
    "google/embeddinggemma-2": {"query": "query", "document": "document"},
}
TOP_KS = [5, 8]
OUT = Path("eval/data/embeddings")


# ================= 1. export（筆電） =================
def export(corpora: list[str]) -> None:
    # eval.run 要比 app 先 import（它會先把資料庫換成臨時的），雖然這裡用不到資料庫，順序還是跟其他評估程式一樣
    # isort: off
    from eval.run import EVAL, doc_code, load_evidence, load_questions
    from app.core import chunker, openai_client, pdf_parser
    # isort: on

    bundle = {"chunker": chunker.STRATEGY, "embedding_model": openai_client.EMBEDDING_MODEL, "corpora": {}}
    for corpus in corpora:
        chunks = []
        for pdf in sorted((EVAL / corpus / "pdfs").glob("*.pdf")):
            for index, piece in enumerate(chunker.chunk(pdf_parser.extract_text(pdf))):
                chunks.append({"doc": doc_code(pdf.name), "index": index, "text": piece})
        questions, _ = load_questions(corpus, "all")
        evidence = load_evidence(corpus)
        items = [{"id": q["id"], "type": q["type"], "split": q["split"], "question": q["question"],
                  "required": sorted({doc_code(s) for s in q["sources"]}), "evidence": evidence.get(q["id"], [])}
                 for q in questions if not q["should_refuse"]]
        # OpenAI 的向量當基準：一次最多送 100 段（跟 document_service 一樣）
        vectors = []
        for start in range(0, len(chunks), 100):
            vectors += openai_client.embed_texts([c["text"] for c in chunks[start : start + 100]])
        for c, v in zip(chunks, vectors, strict=True):
            c["openai"] = [round(x, 6) for x in v]
        for q, v in zip(items, openai_client.embed_queries([q["question"] for q in items]), strict=True):
            q["openai"] = [round(x, 6) for x in v]
        bundle["corpora"][corpus] = {"chunks": chunks, "questions": items}
        print(f"{corpus}：{len(chunks)} 段、有答案的題目 {len(items)} 題")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    print(f"存到 {path}（{path.stat().st_size / 1e6:.1f} MB）")


# ================= 2. score（實驗室） =================
# 跟 eval/common.py 同一套比對方式：中文數字轉成阿拉伯數字、去掉空白再比（複製過來，score 才能單獨跑）
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


def quote_found(quote: str, texts: list[str]) -> bool:
    target = normalize(quote)
    found = [normalize(text) for text in texts]
    if any(target in text for text in found):
        return True
    head, tail = target[:12], target[-12:]
    return any(head in text for text in found) and any(tail in text for text in found)


def evaluate(np, chunks: list[dict], questions: list[dict], chunk_vectors, query_vectors) -> list[dict]:
    # 向量先正規化成長度 1，內積就是餘弦相似度
    chunk_vectors = chunk_vectors / np.linalg.norm(chunk_vectors, axis=1, keepdims=True)
    query_vectors = query_vectors / np.linalg.norm(query_vectors, axis=1, keepdims=True)
    # argsort 由小到大，加負號變成由大到小；stable：分數一樣時照段落的順序，每次結果一樣
    order = np.argsort(-(query_vectors @ chunk_vectors.T), axis=1, kind="stable")
    rows = []
    for q, ranking in zip(questions, order, strict=True):
        for k in TOP_KS:
            top = [chunks[i] for i in ranking[:k]]
            retrieved = {c["doc"] for c in top}
            found = [d for d in q["required"] if d in retrieved]
            quotes = q["evidence"]
            rows.append({
                "id": q["id"], "type": q["type"], "split": q["split"], "top_k": k,
                "retrieved": sorted(retrieved), "required": q["required"],
                "full_hit": len(found) == len(q["required"]),
                "evidence_full": all(quote_found(quote, [c["text"] for c in top]) for quote in quotes) if quotes else None,
            })
    return rows


def summarize(rows: list[dict]) -> dict:
    def rate(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 4) if values else None

    out = {}
    for k in TOP_KS:
        for split in ["dev", "test", "all"]:
            group = [r for r in rows if r["top_k"] == k and (split == "all" or r["split"] == split)]
            out[f"{split}@{k}"] = {"full_hit": rate([r["full_hit"] for r in group]),
                                   "evidence_full": rate([r["evidence_full"] for r in group]), "questions": len(group)}
    return out


def score(bundle_path: Path, models: list[str], device: str, batch_size: int) -> None:
    import numpy as np

    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    corpora = bundle["corpora"]
    out = bundle_path.with_name("embedding_results.json")
    results = {"chunker": bundle["chunker"], "models": {}, "errors": {}}

    # 基準：OpenAI 的向量在筆電算好了
    results["models"]["openai/" + bundle["embedding_model"]] = {
        "dimension": len(next(iter(corpora.values()))["chunks"][0]["openai"]),
        "corpora": {name: _score_corpus(np, c, [x["openai"] for x in c["chunks"]], [q["openai"] for q in c["questions"]])
                    for name, c in corpora.items()},
    }

    # 只算基準的話（--models 不給），不需要裝 torch
    if models:
        import torch
        from sentence_transformers import SentenceTransformer

        device = device if device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
        results["device"] = device
        results["gpu"] = torch.cuda.get_device_name(0) if device == "cuda" else None
    for name in models:
        print(f"\n== {name}（{device}）")
        # 一個模型出錯（例如下載失敗、套件版本不合），記下原因，繼續跑下一個
        try:
            results["models"][name] = score_model(np, SentenceTransformer, name, corpora, device, batch_size)
        except Exception as error:  # noqa: BLE001
            results["errors"][name] = f"{type(error).__name__}: {error}"
            print(f"  失敗：{results['errors'][name]}")
        if device == "cuda":
            torch.cuda.empty_cache()
        # 每跑完一個模型就存一次：後面的模型出事，前面的結果還在
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print_table(results)
    print(f"\n逐題明細：{out}")


def score_model(np, SentenceTransformer, name: str, corpora: dict, device: str, batch_size: int) -> dict:
    prompts = CANDIDATES.get(name, {"query": None, "document": None})
    started = time.perf_counter()
    model = SentenceTransformer(name, device=device)
    # GPU 上用半精度：快一倍、記憶體少一半，檢索的排序幾乎不受影響
    if device == "cuda":
        model.half()
    model.max_seq_length = 2048  # 一段最多約 500 個 OpenAI 的 token，換成別的模型的算法也遠低於這個上限
    entry = {"load_seconds": round(time.perf_counter() - started, 1), "prompts": prompts, "corpora": {}}

    total_chunks, encode_seconds = 0, 0.0
    for corpus, c in corpora.items():
        started = time.perf_counter()
        chunk_vectors = encode(model, [x["text"] for x in c["chunks"]], prompts["document"], batch_size)
        encode_seconds += time.perf_counter() - started
        total_chunks += len(c["chunks"])
        query_vectors = encode(model, [q["question"] for q in c["questions"]], prompts["query"], batch_size)
        entry["dimension"] = chunk_vectors.shape[1]
        entry["corpora"][corpus] = _score_corpus(np, c, chunk_vectors, query_vectors)
        print(f"  {corpus}：全部題目、5 段，檢索完整 {entry['corpora'][corpus]['summary']['all@5']['full_hit']}")
    entry["chunks_per_second"] = round(total_chunks / encode_seconds, 1)

    # 線上每問一題要把問題轉一次向量：量一題一題轉要多久。用 CPU 量，因為部署時 GPU 可能讓給回答模型
    questions = [q["question"] for c in corpora.values() for q in c["questions"]][:20]
    cpu_model = model if device == "cpu" else SentenceTransformer(name, device="cpu")
    encode(cpu_model, questions[:1], prompts["query"], 1)  # 第一次比較慢（初始化），不算
    started = time.perf_counter()
    for text in questions:
        encode(cpu_model, [text], prompts["query"], 1)
    entry["query_ms_cpu"] = round((time.perf_counter() - started) / len(questions) * 1000)
    return entry


# prompt：模型設定檔裡前綴的名字。模型沒有這個名字的話就不加
def encode(model, texts: list[str], prompt: str | None, batch_size: int):
    prompt = prompt if prompt in (getattr(model, "prompts", None) or {}) else None
    return model.encode(texts, prompt_name=prompt, batch_size=batch_size, convert_to_numpy=True, show_progress_bar=False)


def _score_corpus(np, corpus: dict, chunk_vectors, query_vectors) -> dict:
    rows = evaluate(np, corpus["chunks"], corpus["questions"], np.asarray(chunk_vectors, dtype=np.float32),
                    np.asarray(query_vectors, dtype=np.float32))
    return {"summary": summarize(rows), "rows": rows}


def print_table(results: dict) -> None:
    corpora = list(next(iter(results["models"].values()))["corpora"])
    for k in TOP_KS:
        print(f"\n### 找 {k} 段（檢索完整；勞動法規括號內是原文全部找到）\n")
        print("| 模型 | 維度 | " + " | ".join(f"{c} 練習題 | {c} 考試題" for c in corpora) + " | 每秒幾段 | 一題問題 CPU 毫秒 |")
        print("|---|---|" + "---|---|" * len(corpora) + "---|---|")
        for name, m in results["models"].items():
            cells = []
            for c in corpora:
                for split in ["dev", "test"]:
                    s = m["corpora"][c]["summary"][f"{split}@{k}"]
                    cell = f"{s['full_hit'] * 100:.0f}%" if s["full_hit"] is not None else "—"
                    if s["evidence_full"] is not None:
                        cell += f"（{s['evidence_full'] * 100:.0f}%）"
                    cells.append(cell)
            print(f"| {name} | {m['dimension']} | " + " | ".join(cells)
                  + f" | {m.get('chunks_per_second', '—')} | {m.get('query_ms_cpu', '—')} |")
    for name, error in results["errors"].items():
        print(f"\n{name} 沒跑成：{error}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="筆電：切段、算 OpenAI 的向量，存成一個檔案")
    e.add_argument("--corpora", nargs="+", default=["enterprise", "labor", "school"])
    s = sub.add_parser("score", help="實驗室：開源模型轉向量、算分數")
    s.add_argument("bundle", type=Path)
    s.add_argument("--models", nargs="*", default=list(CANDIDATES), help="不給模型名字的話，只算 OpenAI 的基準")
    s.add_argument("--device", default="auto", help="auto / cuda / cpu")
    s.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()
    if args.command == "export":
        export(args.corpora)
    else:
        score(args.bundle, args.models, args.device, args.batch_size)


if __name__ == "__main__":
    sys.exit(main())
