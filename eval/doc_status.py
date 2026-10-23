# 每份文件在評估的「今天」（2026-10-20）的狀態：標準答案（docs/decisions/029）
#
# 兩個用途：
#   1. eval/run.py --doc-status oracle：用標準答案當標籤，量「標籤完全正確時，答案最多能進步多少」
#   2. 直接執行這個檔案：量小模型判斷得準不準
#        .venv\Scripts\python -m eval.doc_status --corpus enterprise
#
# 沒列出來的都是「現行」。依據：
#   企業：2024、2025 年版員工手冊和 2022 年版差旅辦法都有更新的版本；2027 年的修訂公告還沒生效；
#         福利辦法修正草案還在徵詢意見；SUB-* 是子公司青嶼雲端服務的規定
#         FIN-04（2026 年的修訂公告）已經生效，只改了住宿費，原本的國內差旅辦法仍然是現行
#   學校：111、113、114 學年度的要點都有 115 學年度的版本；116 學年度的收費是預告
#         S-12 是市政府教育局訂的，學校也要遵守，所以是現行，不是別的單位

GOLD = {
    "enterprise": {
        "HR-01": "舊版", "HR-08": "舊版", "FIN-12": "舊版",
        "FIN-14": "尚未生效",
        "HR-23": "草案",
        "SUB-01": "其他單位", "SUB-02": "其他單位", "SUB-03": "其他單位", "SUB-04": "其他單位",
    },
    "school": {
        "S-02": "舊版", "S-09": "舊版", "S-10": "舊版", "S-11": "舊版", "S-13": "舊版",
        "S-41": "尚未生效",
    },
}


def gold_status(corpus: str, code: str) -> str:
    return GOLD[corpus].get(code, "現行")


# ---------- 量小模型判斷得準不準 ----------
# 每次判斷 6 份（跟問答時搜到的文件數差不多），清單給全部的文件，跟問答時一樣
BATCH = 6


def main():
    import argparse
    import importlib
    from collections import Counter

    # eval.run 一定要比 app 先 import：它一 import 就把資料庫換成臨時的
    # isort: off
    from eval.run import doc_code, setup_knowledge_base
    # isort: on

    from app.core import openai_client
    from app.repositories import chunk_repo
    from app.services import qa_service
    from eval.common import CORPORA

    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, required=True)
    args = parser.parse_args()

    db, _, workspace, _ = setup_knowledge_base(args.corpus)
    today = importlib.import_module(f"eval.{args.corpus}.rules").TODAY
    headers = chunk_repo.document_headers(db, workspace.id, qa_service.PLAN_CATALOG_LIMIT)
    catalog = [(f, " ".join(h.split())[:qa_service.PLAN_CATALOG_HEADER_CHARS]) for f, h in headers]
    targets = [(f, " ".join(h.split())[:qa_service.DOCUMENT_HEADER_CHARS]) for f, h in headers]

    predicted = {}
    for start in range(0, len(targets), BATCH):
        batch = targets[start:start + BATCH]
        labels = openai_client.label_documents(workspace.name, qa_service._date_text(today), catalog, batch)
        for filename, _ in batch:
            predicted[filename] = labels.get(filename, ("現行", "（沒有判斷）"))

    pairs = Counter()
    for filename, (status, reason) in predicted.items():
        gold = gold_status(args.corpus, doc_code(filename))
        pairs[(gold, status)] += 1
        if gold != status:
            print(f"  ✗ {filename}：標準 {gold}，判斷 {status}（{reason}）")
    correct = sum(n for (gold, status), n in pairs.items() if gold == status)
    print(f"\n{CORPORA[args.corpus]['title']}：{correct} / {len(predicted)} 份判斷正確")
    print("標準答案 → 判斷結果：", dict(pairs))
    db.close()


if __name__ == "__main__":
    main()
