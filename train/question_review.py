# 出題之後的人工審查（docs/decisions/039）：程式檢查之外，逐題看過 286 題，刪掉有問題的、修正小錯字
# 審查的決定寫在這裡（不是直接改資料檔），之後重出題目也能照同樣的決定重做，也看得到每題為什麼刪
#
# 用法（專案資料夾）：.venv\Scripts\python -m train.question_review
# 讀 train/data/taipei/questions.jsonl，寫出 questions_reviewed.jsonl

import json
from pathlib import Path

DATA = Path(__file__).parent / "data" / "taipei"

# 刪掉的題目和原因
REMOVE = {
    "t0012": "身分和條件跟標準答案對不起來，標準答案的說明也不正確",
    "t0064": "題目照抄規章的定義（「具中華民國國籍且在國內設籍的自然人」），同仁不會這樣問",
    "t0094": "「我是機關首長」沒說是幾級機關，答案會因此不同",
    "t0097": "標準答案說「符合資格」，規章還要求「成就卓越」，答案不正確",
    "t0131": "題目文句不通（「我是剛到這個人事管理員」「聘任用途」）",
    "t0150": "重點清單放了跟問題無關的細節（授權條件文號欄位）",
    "t0193": "規章只寫「第三款、第六款以外的文件要正本」，看不出哪些是身分證，標準答案是推測的",
    "t0194": "重點清單要求提到舊規定的 300 元，「不能說」又放了 300 元，自相矛盾",
    "t0195": "舊的是農會人員出國的規定，新的是公教人員因公出國，不是同一件事的新舊版本",
    "t0197": "「舊規定沒有里程標準」沒有原文可以確認",
    "t0203": "跟其他題重複（閒置車輛 3,000 公里），「舊標準 1,000 公里」沒有原文可以確認",
    "t0204": "「我是主管」沒說職等和機關規模，答案會因此不同；重點清單太複雜",
    "t0206": "重點清單放了跟問題無關的規定（電動公務小客車比照專用車）",
    "t0210": "把「清除的電子檔案清單永久留存」當成歸檔清單的舊規定，兩者不是同一份清單",
    "t0221": "「我們局裡」沒說是哪個局，標準答案卻只適用警察局",
    "t0228": "問的是一般職務宿舍，標準答案混進消防局自己的保證金規定，對其他機關的同仁是錯的",
    "t0229": "把火災報廢和公務車牌照湊成一題，同仁不會這樣問",
    "t0230": "「請比較兩局的規定」不是同仁會問的問題",
    "t0251": "題目夾英文（reimburse）",
}

# 小錯字、簡體字：直接改題目或答案
REPLACE = [("吗", "嗎"), ("什么", "什麼"), ("欧", "歐"), ("年检", "年檢"), ("其余", "其餘"),
           ("待遇里", "待遇裡"), ("合同里", "合約裡"), ("三千里", "三千公里")]
EDITS = {
    "t0050": {"question": "我捐了財物給聯合醫院，但不想在網站上看到自己的名字和捐款流向，可以這樣做嗎？"},
}
# 「不能說」裡混進出題提示的範例（跟這題無關），拿掉
DROP_MUST_NOT = {"t0054": ["每年盤點兩次"]}
# 重點清單裡不是事實、而是對答案寫法的要求，拿掉
DROP_MUST_INCLUDE = {"t0217": ["全市府通則"]}


def fix(text: str) -> str:
    for old, new in REPLACE:
        text = text.replace(old, new)
    return text


def main():
    questions = [json.loads(line) for line in (DATA / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line]
    kept = []
    for q in questions:
        if q["id"] in REMOVE:
            continue
        q.update(EDITS.get(q["id"], {}))
        q["question"], q["answer"] = fix(q["question"]), fix(q["answer"])
        q["must_include"] = [fix(m) for m in q["must_include"] if m not in DROP_MUST_INCLUDE.get(q["id"], [])]
        q["must_not"] = [fix(m) for m in q["must_not"] if m not in DROP_MUST_NOT.get(q["id"], [])]
        kept.append(q)
    (DATA / "questions_reviewed.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in kept),
                                                  encoding="utf-8")
    print(f"審查後 {len(kept)} 題（刪掉 {len(questions) - len(kept)} 題）")


if __name__ == "__main__":
    main()
