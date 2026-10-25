# 下載訓練資料用的文件：臺北市政府的內部規章（docs/decisions/039）
#
# 為什麼用臺北市政府：單一組織（市府和所屬各局處），天然有真實企業會遇到的情境——
#   全市府適用的規定 vs 只適用某個局處的規定（像母公司 vs 子公司）、現行 vs 已廢止的舊規定（版本）
# 授權：臺北市法規查詢系統的資料開放宣告允許重製、改作，註明出處；行政規則依著作權法第 9 條也不受保護
# 跟考卷不重複：刻意避開請假、休假、加班、差勤、退休這些主題，勞動法規那套考卷有公務人員請假規則
#
# 用法（專案資料夾）：.venv\Scripts\python -m train.collect_taipei
# 下載到 train/data/taipei/pdfs/（不進 git），清單在 train/data/taipei/manifest.jsonl

import html
import json
import re
import time
from pathlib import Path

import httpx

BASE = "https://laws.gov.taipei"
OUT = Path(__file__).parent / "data" / "taipei"
HEADERS = {"User-Agent": "Mozilla/5.0 (research; traceable-research-assistant)"}

# 主題：搜尋法規名稱的關鍵字 → 最多取幾份。都是市府同仁日常會查的行政規定
# 不用「停車」：搜到的多半是給市民的停車場收費規定，不是同仁上班會查的內部規定
TOPICS = {
    "公務車輛": 15, "宿舍": 10, "財產": 12, "檔案": 12, "文書": 8, "會議": 12,
    "出國": 12, "研習": 12, "門禁": 8, "採購": 12, "志工": 10,
}
# 名稱有這些字的不要：可能跟勞動法規那套考卷的內容重疊（公務人員請假規則），或跟主題無關
EXCLUDE = re.compile(r"請假|休假|加班|差勤|退休|資遣|撫卹|產假|陪產|育嬰|俸|薪|待遇|勞")


def search(client: httpx.Client, keyword: str, page: int) -> list[tuple[str, str, bool]]:
    # 只查臺北市的法規（curcateid=001），每頁 20 筆：[(法規代號, 名稱, 是否已廢止)]
    response = client.get(f"{BASE}/Law/LawSearch/SearchResult", params={
        "searchtype": 1, "searchstring.keyword1": keyword, "cursearchtype": 1, "curcateid": "001",
        "showtype": 0, "sort": 1, "isSmartSearch": "False", "page": page})
    rows = []
    for law_id, inner in re.findall(r'href="/Law/LawSearch/LawInformation/(FL\d+)\?[^"]*" class="law-link"[^>]*>(.*?)</a>',
                                     response.text, flags=re.DOTALL):
        abolished = 'class="abolished"' in inner
        name = html.unescape(re.sub(r"<[^>]+>", "", inner)).replace("(廢)", "").strip()
        # 改過名的會接著寫「原名稱：……」，檔名只留現在的名稱
        rows.append((law_id, re.sub(r"原名稱：.*", "", re.sub(r"\s+", "", name)), abolished))
    return rows


def main():
    OUT.joinpath("pdfs").mkdir(parents=True, exist_ok=True)
    manifest = OUT / "manifest.jsonl"
    done = {json.loads(line)["law_id"] for line in manifest.read_text(encoding="utf-8").splitlines()} if manifest.exists() else set()
    picked = {}
    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        for keyword, limit in TOPICS.items():
            found, page = [], 1
            while len(found) < limit:
                rows = search(client, keyword, page)
                time.sleep(1)  # 政府網站，慢慢抓
                if not rows:
                    break
                found += [r for r in rows if r[0] not in picked and not EXCLUDE.search(r[1])]
                page += 1
            for law_id, name, abolished in found[:limit]:
                picked[law_id] = (keyword, name, abolished)
            print(f"{keyword}：取 {min(len(found), limit)} 份")

        with manifest.open("a", encoding="utf-8") as out:
            for number, (law_id, (keyword, name, abolished)) in enumerate(picked.items(), start=1):
                if law_id in done:
                    continue
                # 匯出「法規所有條文」的 PDF：開頭有法規名稱、修正日期、沿革，文字抓得出來
                response = client.get(f"{BASE}/law/LawSearch/LawExport/{law_id}", params={"type": 0})
                time.sleep(1)
                if response.headers.get("content-type", "").split(";")[0] != "application/pdf":
                    print(f"  {law_id} {name}：下載失敗（{response.status_code}）")
                    continue
                filename = f"TP-{number:03d}_{'(廢)' if abolished else ''}{name}.pdf"
                (OUT / "pdfs" / filename).write_bytes(response.content)
                out.write(json.dumps({"law_id": law_id, "file": filename, "name": name, "abolished": abolished,
                                      "topic": keyword}, ensure_ascii=False) + "\n")
                print(f"  {filename}")
    print(f"清單：{manifest}")


if __name__ == "__main__":
    main()
