# 青嶼科技知識庫的評估題目（B3，docs/decisions/016）
# 用法（專案資料夾）：.venv\Scripts\python -m eval.enterprise.questions
# 產生 questions.jsonl（給評分程式用）和 questions.md（給人看）
#
# 標準答案裡的數字都用 rules.py 算出來，不是手寫的，所以跟文件一定一致
# 每題的欄位：
#   type          general 普通題 / conditional 條件題 / persona 身分題 / version 版本題 /
#                 scope 範圍陷阱 / cross_doc 跨文件題 / no_answer 沒有答案
#   question      使用者的問題（身分寫在問題裡，跟真實使用時一樣）
#   answer        標準答案（給人看、也給評審參考）
#   must_include  好的答案一定要提到的事實
#   must_not      出現就算錯的說法（例如把正職的規定套到實習生身上）
#   sources       應該引用的文件和條文
#   should_refuse 文件裡沒有答案，應該說找不到

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from eval.enterprise import rules as R
from eval.enterprise.rules import Person

HERE = Path(__file__).parent


@dataclass
class Question:
    id: str
    type: str
    question: str
    answer: str
    must_include: list[str]
    sources: list[str]
    must_not: list[str] = field(default_factory=list)
    should_refuse: bool = False


def money(n: int) -> str:
    return f"{n:,} 元"


# 常用的身分
def staff(**kw) -> Person:
    return Person(**kw)


LEAVE = {months: R.annual_leave(staff(months=months)) for months in (8, 24, 72, 144)}
OLD_LEAVE = {months: R.annual_leave(staff(months=months), 2025) for months in (8, 24, 72, 144)}
L = R.LODGING["new"]
OLD = R.LODGING["old"]

QUESTIONS = [
    # ---------- 普通題：沒有條件，直接回答，不要加多餘的「視情況而定」 ----------
    Question("g01", "general", "青嶼科技的總公司地址在哪裡？",
             "台北市內湖區星湖路 88 號 12 樓。", ["星湖路 88 號 12 樓"], ["GEN-01"]),
    Question("g02", "general", "公司是哪一年成立的？", "2012 年。", ["2012"], ["GEN-01"]),
    Question("g03", "general", "IT 服務台的分機是幾號？", "分機 1234。", ["1234"], ["GEN-01", "IT-01 第 5 條"]),
    Question("g04", "general", "公司系統的密碼有什麼規定？",
             "至少 12 個字元，包含大寫、小寫英文和數字，每 90 天更換一次，不得共用。",
             ["12 個字元", "90 天"], ["IT-02 第 2 條"]),
    Question("g05", "general", "報銷的款項每個月哪幾天撥款？", "每月 10 日和 25 日。", ["10 日", "25 日"], ["FIN-03 第 5 條"]),
    Question("g06", "general", "公司有哪些部門？",
             "研發部、業務部、人力資源部、財務部、資訊部、行政部，共 6 個部門。",
             ["研發部", "業務部", "人力資源部", "財務部", "資訊部", "行政部"], ["GEN-01"]),
    Question("g07", "general", "每個月加班最多可以加幾個小時？",
             f"每月合計不得超過 {R.OVERTIME_MONTHLY_CAP_HOURS} 小時。", ["46 小時"], ["HR-05 第 7 條"]),
    Question("g08", "general", "公司筆電不見了，要在多久之內通報？",
             "發現後 24 小時內通報資訊部服務台（分機 1234）。", ["24 小時"], ["IT-01 第 5 條"]),

    # ---------- 條件題：答案依身分而不同，要把條件列出來 ----------
    Question("c01", "conditional", "特休有幾天？",
             f"依 2026 年版員工手冊，正職員工：滿 6 個月未滿 1 年 {LEAVE[8]} 天、滿 1 年未滿 5 年 {LEAVE[24]} 天、"
             f"滿 5 年未滿 10 年 {LEAVE[72]} 天、滿 10 年以上 {LEAVE[144]} 天。約聘人員依勞基法最低日數；實習生沒有特休。"
             "台中分公司正職另有 1 天地區彈性假（不屬於特休）。",
             ["依年資", f"{LEAVE[24]} 天", f"{LEAVE[72]} 天", f"{LEAVE[144]} 天"],
             ["HR-02 第 6 條", "HR-03 第 3、5 條"],
             must_not=[f"正職年資 1～5 年是 {OLD_LEAVE[24]} 天（那是 2025 舊版）"]),
    Question("c02", "conditional", "國內出差的住宿費上限是多少？",
             f"依 2026 年 1 月 1 日起的修訂公告：P1–P3 台北 {money(L['P1-P3'][0])}、其他縣市 {money(L['P1-P3'][1])}；"
             f"P4–P5 台北 {money(L['P4-P5'][0])}、其他 {money(L['P4-P5'][1])}；"
             f"M1–M3 台北 {money(L['M'][0])}、其他 {money(L['M'][1])}（每晚含稅）。",
             ["依職級", "依台北或其他縣市", money(L["P1-P3"][0])],
             ["FIN-04", "FIN-01 第 4 條"],
             must_not=[f"P1–P3 台北目前是 {money(OLD['P1-P3'][0])}（那是修訂前的標準）"]),
    Question("c03", "conditional", "每週可以遠距工作幾天？",
             "試用期滿的正職和約聘人員：研發部 3 天、業務部 2 天（2026 年 4 月 1 日起由 1 天調整）、其他部門 2 天。"
             "試用期員工和實習生不能遠距。",
             ["研發部 3", "業務部 2", "其他部門 2", "試用期", "實習生"], ["HR-06 第 2、3 條", "HR-06 修訂紀錄"],
             must_not=["業務部目前每週 1 天"]),
    Question("c04", "conditional", "出國出差可以坐商務艙嗎？",
             "原則是經濟艙。要同時符合兩個條件才能坐商務艙：單程表定飛行 8 小時以上，而且職級是 M2（經理）以上。",
             ["經濟艙", "8 小時以上", "M2"], ["FIN-02 第 2 條"]),
    Question("c05", "conditional", "健康檢查有補助多少錢？",
             f"正職員工每年 {money(R.health_check_subsidy(staff(age=30)))}，年滿 40 歲以上 {money(R.health_check_subsidy(staff(age=45)))}；"
             f"約聘人員每年 {money(R.health_check_subsidy(staff(employment='約聘')))}；實習生沒有。",
             ["3,000", "5,000", "40 歲", "2,000"], ["GEN-03 第 2 條", "HR-03 第 7 條"]),
    Question("c06", "conditional", "外部課程的教育訓練補助上限是多少？",
             "限到職滿 1 年的正職：P1–P3 每年 15,000 元、P4–P5 25,000 元、M1–M3 40,000 元。約聘和實習生不適用。",
             ["到職滿 1 年", "15,000", "25,000", "40,000"], ["GEN-04 第 2、3 條"]),
    Question("c07", "conditional", "加班有加班費嗎？",
             "一般職（P1–P5）可以選加班費或 1:1 補休；主管職（M1–M3）不給加班費，只能補休（每月上限 16 小時）；"
             "約聘比照一般職；實習生不得加班。",
             ["一般職", "主管職不支給加班費", "補休", "實習生不得加班"], ["HR-05 第 4、5 條", "HR-03 第 4、5 條"]),
    Question("c08", "conditional", "出差搭計程車可以報公帳嗎？",
             "原則上搭高鐵、台鐵或客運。只有三種情況可以搭計程車：晚上 10 點到早上 6 點之間、攜帶 10 公斤以上公務設備、"
             "經直屬主管核准的急迫情形。",
             ["晚上 10 點", "10 公斤", "主管核准"], ["FIN-01 第 3 條"]),
    Question("c09", "conditional", "公司配的筆電多久可以換？",
             "研發部高效能筆電 2 年可以申請更換；其他部門標準筆電 3 年。實習生用公用借用機，不配發。",
             ["研發部 2 年", "其他部門 3 年"], ["IT-01 第 2、4 條"]),
    Question("c10", "conditional", "加班的餐費補助多少？",
             f"平日加班到晚上 8 點以後，每次 {money(R.overtime_meal(staff()))}；台中分公司是 {money(R.overtime_meal(staff(site='台中')))}。",
             ["晚上 8 點", "150", "台中", "200"], ["HR-05 第 6 條", "HR-04 第 3 條"]),
    Question("c11", "conditional", "可以申請公司的停車位嗎？",
             "台北總部的汽車位限主管職 M1 以上、懷孕或領有身心障礙證明的同仁申請，其他人可以登記候補；機車位免申請。"
             "台中分公司和高雄研發中心依各據點規定（文件中沒有）。",
             ["台北", "M1 以上", "懷孕", "身心障礙", "候補"], ["GEN-02 第 2～5 條"]),
    Question("c12", "conditional", "報銷超過期限怎麼辦？",
             "支出後 30 天內正常報銷；超過 30 天要附直屬主管加簽說明；超過 60 天不受理。",
             ["30 日", "主管加簽", "60 日", "不予受理"], ["FIN-03 第 2 條"]),
    Question("c13", "conditional", "可以用自己的手機收公司的信嗎？",
             "可以，安裝公司核可的郵件 App 收發信，但不能存取其他內部系統。研發部同仁例外：個人裝置不能連任何公司系統，包括郵件。",
             ["核可", "研發部", "不得"], ["IT-02 第 4 條"]),
    Question("c14", "conditional", "外接螢幕可以申請幾台？",
             "研發部每人 2 台，其他部門每人 1 台。", ["研發部 2", "其他部門 1"], ["IT-01 第 3 條"]),

    # ---------- 身分題：問題裡有身分，要挑出適用他的那一條 ----------
    Question("p01", "persona", "我是台中分公司的工程師，正職，到職 3 年，特休有幾天？",
             f"特休 {R.annual_leave(staff(months=36, site='台中'))} 天（2026 年版，年資 1～5 年），"
             f"另外台中分公司正職有地區彈性假 {R.regional_flex_leave(staff(site='台中'))} 天（不屬於特休，當年度要休完）。",
             [f"{R.annual_leave(staff(months=36))} 天", "地區彈性假", "不屬於特別休假"], ["HR-02 第 6 條", "HR-04 第 2 條"],
             must_not=["特休 15 天（把地區彈性假算進特休）", f"特休 {OLD_LEAVE[24]} 天（2025 舊版）"]),
    Question("p02", "persona", "我在台北總公司當經理，年資 12 年，特休有幾天？",
             f"{R.annual_leave(staff(months=144, level='M2'))} 天（2026 年版，滿 10 年以上）。",
             [f"{LEAVE[144]} 天"], ["HR-02 第 6 條"], must_not=[f"{OLD_LEAVE[144]} 天"]),
    Question("p03", "persona", "我是研發部 P4 的工程師，下週要去台北出差住兩晚，住宿每晚上限多少？",
             f"每晚 {money(R.domestic_lodging('P4', '台北'))}（2026 年修訂後的 P4–P5 台北標準）。",
             [money(R.domestic_lodging("P4", "台北"))], ["FIN-04"],
             must_not=[money(R.domestic_lodging("P4", "台北", date(2025, 6, 1)))]),
    Question("p04", "persona", "我是業務部的正職員工，已經過了試用期，每週最多可以遠距幾天？",
             f"{R.remote_days(staff(department='業務部'))} 天（2026 年 4 月 1 日起由 1 天調整為 2 天）。",
             ["2 天"], ["HR-06 修訂紀錄"], must_not=["每週 1 天"]),
    Question("p05", "persona", "我是剛到職兩個月的研發工程師，可以遠距工作嗎？",
             "不行。試用期（到職 3 個月內）的員工不能申請遠距工作，試用期滿後研發部每週最多 3 天。",
             ["不行", "試用期"], ["HR-06 第 2 條", "HR-02 第 3 條"], must_not=["可以遠距 3 天"]),
    Question("p06", "persona", "我是 M1 組長，要飛去美國出差，單程大約 13 小時，可以坐商務艙嗎？",
             "不行。雖然飛行超過 8 小時，但商務艙要 M2（經理）以上，M1 不符合。",
             ["不行", "M2"], ["FIN-02 第 2 條", "GEN-01"], must_not=["可以坐商務艙"]),
    Question("p07", "persona", "我是 M3 協理，要去東京出差，飛行大約 3 小時，可以坐商務艙嗎？",
             "不行。職級符合，但單程飛行要 8 小時以上才能坐商務艙，3 小時不符合。",
             ["不行", "8 小時"], ["FIN-02 第 2 條"], must_not=["可以坐商務艙"]),
    Question("p08", "persona", "我是正職員工，今年 45 歲，健康檢查補助多少？",
             f"{money(R.health_check_subsidy(staff(age=45)))}（年滿 40 歲以上的正職）。",
             ["5,000"], ["GEN-03 第 2 條"], must_not=["3,000 元"]),
    Question("p09", "persona", "我是台中分公司的 P2 工程師，平日加班到晚上 9 點，餐費補助多少？可以領加班費嗎？",
             f"餐費 {money(R.overtime_meal(staff(site='台中')))}（台中分公司規定）。P2 是一般職，可以選加班費或補休。",
             ["200", "加班費或補休"], ["HR-04 第 3 條", "HR-05 第 4 條"], must_not=["餐費 150 元"]),
    Question("p10", "persona", "我是財務部 P3、到職 2 年的正職員工，外部課程補助每年上限多少？",
             f"{money(R.training_cap(staff(level='P3', months=24)))}。",
             ["15,000"], ["GEN-04 第 3 條"]),

    # ---------- 版本題：新舊規定衝突，要用現行的、並指出舊的已經不適用 ----------
    Question("v01", "version", "聽說特休是 12 天，對嗎？",
             f"那是 2025 年版員工手冊（年資 1～5 年）的規定，已於 2026 年 1 月 1 日廢止。"
             f"現行 2026 年版：年資 1～5 年 {LEAVE[24]} 天，滿 5 年 {LEAVE[72]} 天，滿 10 年 {LEAVE[144]} 天。",
             ["2025 年版", "廢止", f"{LEAVE[24]} 天"], ["HR-02 第 6、12 條", "HR-01 第 6 條"],
             must_not=["特休是 12 天（沒有說明那是舊版）"]),
    Question("v02", "version", "早上最晚幾點要到公司？",
             "依 2026 年版員工手冊，可以在 8:00 到 10:00 之間彈性到班，所以最晚 10:00；台中分公司是 8:00 到 9:00，最晚 9:00。",
             ["10:00", "台中", "9:00"], ["HR-02 第 5 條", "HR-04 第 4 條"],
             must_not=["最晚 9:30（2025 舊版）"]),
    Question("v03", "version", "業務部每週可以遠距 1 天，對嗎？",
             "已經改了。遠距工作辦法 2026 年 3 月 15 日修訂，業務部從 2026 年 4 月 1 日起每週上限調整為 2 天。",
             ["2 天", "2026 年 4 月 1 日"], ["HR-06 修訂紀錄"], must_not=["對，業務部每週 1 天"]),
    Question("v04", "version", "國內差旅辦法寫 P1–P3 在台北的住宿上限是 2,400 元，現在還是這樣嗎？",
             f"不是。2026 年 1 月 1 日起依修訂公告調整為 {money(L['P1-P3'][0])}；2025 年 12 月 31 日以前發生的住宿，才適用 2,400 元。",
             [money(L["P1-P3"][0]), "2026 年 1 月 1 日"], ["FIN-04"], must_not=["現在還是 2,400 元"]),
    Question("v05", "version", "我是 P2，2025 年 12 月去台北出差住了一晚，現在才要報帳，住宿上限是多少？",
             f"住宿上限是 {money(R.domestic_lodging('P2', '台北', date(2025, 12, 10)))}（2025 年 12 月 31 日以前發生的住宿適用修正前的標準）。"
             f"但這筆支出到現在已經約 10 個月，依報銷規定超過 60 天{R.reimbursement_deadline(300)}，所以這筆已經不能報了。",
             ["2,400", "修正前", "60 日", "不予受理"], ["FIN-04 說明四", "FIN-01 第 4 條", "FIN-03 第 2 條"],
             must_not=["2,800 元", "可以正常報帳（忽略了已超過 60 天）"]),
    Question("v06", "version", "員工手冊 2025 年版現在還有效嗎？",
             "沒有。2026 年版員工手冊第 12 條規定，2025 年版自 2026 年 1 月 1 日起廢止。",
             ["廢止", "2026 年 1 月 1 日"], ["HR-02 第 12 條"]),

    # ---------- 範圍陷阱：規定不適用於問的人，不能把別人的規定套上去 ----------
    Question("s01", "scope", "我是實習生，特休有幾天？",
             "實習生不適用特別休假。需要請假可以請事假，每月最多 2 天，不給薪。",
             ["不適用特別休假", "事假", "2 日"], ["HR-03 第 5 條"], must_not=[f"{LEAVE[24]} 天", f"{LEAVE[8]} 天"]),
    Question("s02", "scope", "我是約聘人員，下個月結婚，可以領結婚禮金嗎？",
             "不行。婚喪補助限正職員工，約聘人員不適用。",
             ["不行", "限正職"], ["GEN-03 第 4 條"], must_not=["可以領 6,000 元"]),
    Question("s03", "scope", "我是約聘人員，特休有幾天？",
             "約聘人員依勞動基準法第 38 條的最低日數給予，不適用員工手冊的特休表（文件中沒有列出天數）。",
             ["勞動基準法第 38 條", "不適用員工手冊"], ["HR-03 第 3 條"], must_not=[f"{LEAVE[24]} 天"]),
    Question("s04", "scope", "我在高雄研發中心上班，可以請地區彈性假嗎？",
             "不行。地區彈性假是台中分公司正職員工專屬的假別。",
             ["不行", "台中分公司"], ["HR-04 第 2 條"], must_not=["可以請 1 天"]),
    Question("s05", "scope", "我是實習生，可以申請外部課程補助嗎？",
             "不行。實習生和約聘人員都不適用教育訓練補助辦法。",
             ["不行"], ["HR-03 第 8 條", "GEN-04 第 2 條"], must_not=["15,000 元"]),
    Question("s06", "scope", "我是研發部的同仁，可以用自己的手機收公司的信嗎？",
             "不行。研發部同仁的個人裝置不能連接公司任何系統，包括電子郵件。",
             ["不行", "研發部"], ["IT-02 第 4 條"], must_not=["可以安裝郵件 App"]),
    Question("s07", "scope", "我是約聘人員，可以申請教育訓練補助嗎？",
             "不行。教育訓練補助限到職滿 1 年的正職員工。", ["不行"], ["GEN-04 第 2 條", "HR-03 第 8 條"]),
    Question("s08", "scope", "我是實習生，主管想派我去日本出差，可以嗎？",
             "不行。實習生不得派赴國外出差。", ["不行"], ["FIN-02 第 6 條"]),

    # ---------- 跨文件題：要把兩份文件合起來看 ----------
    Question("x01", "cross_doc", "我是台中分公司的正職，到職 6 年，今年總共有幾天有薪的特休和地區彈性假？",
             f"{R.annual_leave(staff(months=72)) + R.regional_flex_leave(staff(site='台中'))} 天："
             f"特休 {R.annual_leave(staff(months=72))} 天（年資 5～10 年）加上地區彈性假 1 天。",
             ["17", f"{LEAVE[72]} 天", "1 天"], ["HR-02 第 6 條", "HR-04 第 2 條"]),
    Question("x02", "cross_doc", "我是 P5，要去台中出差兩晚，住宿每晚上限多少？膳雜費怎麼算？",
             f"住宿每晚 {money(R.domestic_lodging('P5', '台中'))}（修訂公告的其他縣市標準）；"
             f"膳雜費每天 {money(R.DOMESTIC_MEAL_PER_DAY)}，修訂公告沒有改，仍依國內差旅辦法。",
             [money(R.domestic_lodging("P5", "台中")), "600"], ["FIN-04", "FIN-01 第 5 條"],
             must_not=["住宿 2,500 元"]),
    Question("x03", "cross_doc", "我在台中分公司，加班到晚上 9 點，餐費補助是 150 元還是 200 元？",
             "200 元。加班與補休辦法寫 150 元，但同一條也寫「各據點另有規定者從其規定」，台中分公司補充規定是 200 元。",
             ["200"], ["HR-04 第 3 條", "HR-05 第 6 條"], must_not=["150 元"]),
    Question("x04", "cross_doc", "約聘人員加班，可以選加班費還是補休？",
             "可以選。約聘人員準用加班與補休辦法中一般職的規定，可以選加班費或 1:1 補休。",
             ["準用", "一般職", "加班費或補休"], ["HR-03 第 4 條", "HR-05 第 4 條"]),
    Question("x05", "cross_doc", "我要買一套 12,000 元的設計軟體，採購要誰核准？之後報帳要誰簽？",
             "採購：單價超過 5,000 元，要先經資訊部審核，再經部門主管核准。報帳：單筆 1 萬元以上，"
             "除直屬主管外還要財務長核准。",
             ["資訊部", "部門主管", "財務長"], ["IT-03 第 3 條", "FIN-03 第 3 條"]),
    Question("x06", "cross_doc", "在家遠距工作時，要怎麼連公司的內部系統？",
             "一律透過公司 VPN 連線，並遵守資訊安全規範。",
             ["VPN"], ["HR-06 第 5 條", "IT-02 第 3 條"]),

    # ---------- 沒有答案：文件裡沒有寫，要老實說找不到 ----------
    Question("n01", "no_answer", "年終獎金發幾個月？", "文件中沒有年終獎金的規定。", [], [], should_refuse=True),
    Question("n02", "no_answer", "公司有提供員工宿舍嗎？", "文件中沒有員工宿舍的相關規定。", [], [], should_refuse=True),
    Question("n03", "no_answer", "台中分公司的汽車停車位要怎麼申請？",
             "文件中沒有。台北總部停車管理辦法只適用台北，台中分公司依各據點規定，但知識庫裡沒有台中的停車規定。",
             [], ["GEN-02 第 2 條"], must_not=["套用台北的資格（M1 以上、懷孕、身障）"], should_refuse=True),
    Question("n04", "no_answer", "尾牙是幾月舉辦？", "文件中沒有尾牙的資訊。", [], [], should_refuse=True),
    Question("n05", "no_answer", "公司有員工認股計畫嗎？", "文件中沒有員工認股的相關規定。", [], [], should_refuse=True),
    Question("n06", "no_answer", "試用期滿之後薪水會調整嗎？",
             "文件中沒有。員工手冊只寫試用期 3 個月、滿了經考核轉正，沒有提到調薪。",
             [], ["HR-02 第 3 條"], must_not=["試用期滿會調薪"], should_refuse=True),
]


def main():
    ids = [q.id for q in QUESTIONS]
    assert len(ids) == len(set(ids)), "題號重複"
    (HERE / "questions.jsonl").write_text(
        "\n".join(json.dumps(asdict(q), ensure_ascii=False) for q in QUESTIONS) + "\n", encoding="utf-8")
    titles = {"general": "普通題", "conditional": "條件題", "persona": "身分題", "version": "版本題",
              "scope": "範圍陷阱", "cross_doc": "跨文件題", "no_answer": "沒有答案"}
    lines = ["# 青嶼科技知識庫評估題目", "", "由 `python -m eval.enterprise.questions` 產生，標準答案的數字來自 `rules.py`。", ""]
    for qtype, title in titles.items():
        group = [q for q in QUESTIONS if q.type == qtype]
        lines += [f"## {title}（{len(group)} 題）", ""]
        for q in group:
            lines.append(f"**{q.id}　{q.question}**")
            lines.append(f"- 標準答案：{q.answer}")
            if q.sources:
                lines.append(f"- 應該引用：{'、'.join(q.sources)}")
            if q.must_not:
                lines.append(f"- 不能說：{'；'.join(q.must_not)}")
            lines.append("")
    (HERE / "questions.md").write_text("\n".join(lines), encoding="utf-8")
    counts = {titles[t]: sum(1 for q in QUESTIONS if q.type == t) for t in titles}
    print(f"共 {len(QUESTIONS)} 題：{counts}")


if __name__ == "__main__":
    main()
