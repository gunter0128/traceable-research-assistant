# 虛構學校「澄溪國小」的規則表：學校知識庫的「真相」（docs/decisions/017）
# 第二個領域：用來確認問答的改進不是只對企業文件有效（防止過度擬合）
# 8 份文件照這裡的規則寫，題目的標準答案也從這裡算出來
#
# 「現在」是 2026-10-20，也就是 115 學年度第 1 學期
# 學年度從每年 8 月 1 日開始；民國年 = 西元年 − 1911（文件裡都用民國年，跟真實的學校公文一樣）

import math
from dataclasses import dataclass
from datetime import date

TODAY = date(2026, 10, 20)
SCHOOL_YEAR = 115


@dataclass
class Teacher:
    employment: str = "正式"  # 正式（正式教師）/ 代理（整學期聘任的代理教師）/ 鐘點代課（按節計酬）
    role: str = "導師"  # 導師 / 科任 / 組長 / 主任
    campus: str = "本校"  # 本校 / 山海分校


# ---------- 教師請假（S-01 是 115 學年度版，S-02 是 114 學年度版、已被 S-01 廢止）----------
NOTICE_SCHOOL_DAYS = {114: 2, 115: 3}  # 事假要在幾個上課日前申請


def leave_rules_apply(teacher: Teacher) -> bool:
    # 要點只適用正式教師、代理教師；鐘點代課按節計酬，不適用
    return teacher.employment != "鐘點代課"


HOURLY_SUBSTITUTE_NOTICE = "前一日"  # 鐘點代課教師無法到校時，前一日通知教務處課務組，未授課的節數不支鐘點費


def leave_approvers(days: float, school_year: int = SCHOOL_YEAR) -> list[str]:
    if school_year == 114:
        return ["教務主任"] if days <= 2 else ["校長"]
    if days <= 1:
        return ["教務主任"]
    if days <= 3:
        return ["校長"]
    return ["校長", "人事室會辦"]


def personal_leave_substitute_payer(day: int) -> str:
    # 事假第幾天的代課鐘點費誰付（S-01）：前 3 日學校支付，第 4 日起請假教師自行負擔；病假、公假都是學校支付
    return "學校" if day <= 3 else "請假教師"


SICK_LEAVE_CALL_BY = "7:30"  # 臨時病假：當日上午 7:30 前電話通知課務組，返校後 3 日內補辦
SICK_LEAVE_PAPERWORK_DAYS = 3


# ---------- 導師與科任（S-03；S-08 分校補充）----------
def duty_weeks(teacher: Teacher) -> int:
    # 每學期導護輪值幾週；組長、主任免；山海分校由校車接送，免導護
    if teacher.campus == "山海分校":
        return 0
    return {"導師": 1, "科任": 2}.get(teacher.role, 0)


BASE_PERIODS = {"導師": 18, "科任": 21, "組長": 12, "主任": 4}  # 每週基本授課節數


# ---------- 成績評量（S-04）----------
def has_paper_exam(grade: int) -> bool:
    # 1–2 年級不辦紙筆的定期評量
    return grade >= 3


def grade_weights(grade: int) -> dict[str, int]:
    return {"定期評量": 0, "平時評量": 100} if grade <= 2 else {"定期評量": 40, "平時評量": 60}


def makeup_ratio(reason: str) -> float | None:
    # 定期評量缺考的補考成績：公假、病假、喪假照實際分數；事假或其他事由打八折；無故缺考不能補考（零分）
    if reason in ("公假", "病假", "喪假"):
        return 1.0
    if reason == "無故":
        return None
    return 0.8


MAKEUP_WITHIN_DAYS = 7  # 返校後一週內補考
REPORT_CARD_DAYS = 10  # 學期結束後 10 日內寄發成績通知單


# ---------- 校外教學（S-05；S-08 分校補充）----------
def students_per_chaperone(grade: int, campus: str = "本校") -> int:
    if campus == "山海分校":
        return 8  # 分校混齡編班
    return 10 if grade <= 2 else 15


def chaperones(students: int, grade: int, campus: str = "本校") -> int:
    # 帶隊人員（含導師、志工家長）至少幾位
    return math.ceil(students / students_per_chaperone(grade, campus))


TRIP_FEE_CAP = {"old": {"半日": 300, "全日": 600}, "new": {"半日": 300, "全日": 800}}
TRIP_FEE_REVISION = date(2026, 8, 26)  # 115 年 8 月 26 日校務會議修正第五點：全日 600 → 800，半日不變


def trip_fee_cap(kind: str, on: date = TODAY) -> int:
    return TRIP_FEE_CAP["new" if on >= TRIP_FEE_REVISION else "old"][kind]


CONSENT_DAYS_BEFORE = 7  # 家長同意書出發前 7 日回收，沒交的學生不得參加，留校上課
PLAN_DAYS_BEFORE = 30  # 計畫出發前 30 日送教務處


def trip_cost_note(low_income: bool, campus: str = "本校") -> str:
    if low_income:
        return "全額補助"  # 低收入戶、中低收入戶學生的費用由學校全額補助
    if campus == "山海分校":
        return "交通費由學校補助，其他費用自付"
    return "自付"


# ---------- 學生請假、課後照顧（S-06；S-07 收費調整公告；S-08 分校不開辦）----------
SICK_PROOF_DAYS = 3  # 學生病假連續 3 日以上（含）附醫療證明


def after_school_end(grade: int, campus: str = "本校") -> str | None:
    if campus == "山海分校":
        return None  # 分校不開辦
    return "17:30" if grade <= 2 else "18:00"


AFTER_SCHOOL_FEE = {"old": 2000, "new": 2200}
AFTER_SCHOOL_FEE_REVISION = date(2026, 10, 1)  # 收費調整公告：115 年 10 月起


def after_school_fee(status: str = "一般", campus: str = "本校", on: date = TODAY) -> int | None:
    # 每月費用；低收入戶、中低收入戶免費；身心障礙學生半價（調整後一樣半價）
    if campus == "山海分校":
        return None
    base = AFTER_SCHOOL_FEE["new" if on >= AFTER_SCHOOL_FEE_REVISION else "old"]
    if status in ("低收入戶", "中低收入戶"):
        return 0
    if status == "身心障礙":
        return base // 2
    return base


REFUND_MIN_SCHOOL_DAYS = 5  # 連續請假 5 個上課日以上才按日退費


# ---------- 自動檢查：這些字一定要出現在對應的文件裡（build_pdfs 產生 PDF 後檢查）----------
# 比對時去掉所有空白：PDF 解析出來的文字可能在任何地方斷行
FACTS = [
    ("S-01", "三個上課日前"), ("S-01", "一日以內者，由教務主任核准"), ("S-01", "逾一日至三日者，由校長核准"),
    ("S-01", "逾三日者"), ("S-01", "人事室會辦"), ("S-01", "第四日起"), ("S-01", "上午7時30分前"),
    ("S-01", "鐘點代課教師"), ("S-01", "前一日"), ("S-01", "代課交辦單"), ("S-01", "同時廢止"),
    ("S-02", "二個上課日前"), ("S-02", "二日以內者，由教務主任核准"),
    ("S-03", "每學期輪值一週"), ("S-03", "每學期輪值二週"), ("S-03", "免輪值"),
    ("S-03", "18"), ("S-03", "21"), ("S-03", "12"),
    ("S-04", "一、二年級不辦理紙筆"), ("S-04", "百分之四十"), ("S-04", "百分之六十"), ("S-04", "百分之八十"),
    ("S-04", "十日內"), ("S-04", "一週內"),
    ("S-05", "每十名學生"), ("S-05", "每十五名學生"), ("S-05", "出發前七日"), ("S-05", "600"), ("S-05", "800"),
    ("S-05", "全額補助"), ("S-05", "出發前三十日"),
    ("S-06", "連續三日以上"), ("S-06", "17:30"), ("S-06", "18:00"), ("S-06", "2,000"), ("S-06", "半價"),
    ("S-06", "連續五個上課日以上"),
    ("S-07", "2,200"), ("S-07", "115年10月"), ("S-07", "減免"),
    ("S-08", "不開辦課後照顧班"), ("S-08", "免輪值導護"), ("S-08", "每八名學生"), ("S-08", "交通費由學校全額補助"),
]
