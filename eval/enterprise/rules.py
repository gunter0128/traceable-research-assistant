# 虛構公司「青嶼科技」的規則表：整份企業知識庫的「真相」（B3，docs/decisions/016）
# 18 份文件照這裡的規則寫，題目的標準答案也從這裡算出來，所以兩者一定一致，不靠 AI 判斷
# 「現在」設定為 2026 年 10 月：2026 年版的規定是現行版本

from dataclasses import dataclass
from datetime import date

TODAY = date(2026, 10, 20)


@dataclass
class Person:
    # 問問題的人。題目裡用文字描述身分，這裡是同一個身分的結構化版本，拿來算正確答案
    employment: str = "正職"  # 正職 / 約聘 / 實習生
    months: int = 36  # 到職幾個月
    level: str = "P3"  # P1～P5（一般職）、M1～M3（主管職：M1 組長、M2 經理、M3 協理）
    department: str = "研發部"  # 研發部 / 業務部 / 人力資源部 / 財務部 / 資訊部 / 行政部
    site: str = "台北"  # 台北（總公司）/ 台中（分公司）/ 高雄（研發中心）
    age: int = 30

    @property
    def is_manager(self) -> bool:
        return self.level.startswith("M")

    @property
    def on_probation(self) -> bool:
        # 員工手冊第 3 條：正職試用期 3 個月
        return self.employment == "正職" and self.months < 3


# ---------- 特別休假（HR-01 / HR-02 第 6 條、HR-03 第 3、5 條、HR-04 第 2 條）----------
# (到職滿幾個月以上, 日數)，由大到小比對
LEAVE_TABLE = {
    2025: [(120, 15), (60, 14), (12, 12), (6, 3), (0, 0)],
    2026: [(120, 18), (60, 16), (12, 14), (6, 3), (0, 0)],
}


def annual_leave(person: Person, year: int = 2026) -> int | None:
    # 回傳特休日數；約聘依勞基法（文件裡沒有列天數）回傳 None；實習生沒有特休回傳 0
    if person.employment == "實習生":
        return 0
    if person.employment == "約聘":
        return None
    return next(days for months, days in LEAVE_TABLE[year] if person.months >= months)


def regional_flex_leave(person: Person) -> int:
    # 台中分公司補充規定第 2 條：台中分公司「正職」另給地區彈性假 1 日（不是特休）
    return 1 if person.site == "台中" and person.employment == "正職" else 0


# ---------- 上班時間（HR-01 / HR-02 第 5 條、HR-04 第 4 條）----------
def flex_start_window(person: Person, year: int = 2026) -> tuple[str, str]:
    if person.site == "台中":
        return ("08:00", "09:00")
    return ("08:30", "09:30") if year == 2025 else ("08:00", "10:00")


# ---------- 加班（HR-05、HR-03 第 4、5 條、HR-04 第 3 條）----------
def overtime_pay_allowed(person: Person) -> bool | None:
    # True：可選加班費或補休；False：主管職不給加班費、只能補休；None：實習生不得加班
    if person.employment == "實習生":
        return None
    return not person.is_manager


def overtime_meal(person: Person) -> int:
    # 平日加班到晚上 8 點以後，每次的餐費補助
    return 200 if person.site == "台中" else 150


OVERTIME_MONTHLY_CAP_HOURS = 46

# ---------- 遠距工作（HR-06，第 3 條在 2026/3/15 修訂：業務部 1 → 2 日，2026/4/1 生效）----------
REMOTE_DAYS = {"研發部": 3, "業務部": 1, "其他": 2}
REMOTE_AMENDMENT_EFFECTIVE = date(2026, 4, 1)


def remote_days(person: Person, on: date = TODAY) -> int:
    if person.employment == "實習生" or person.on_probation:
        return 0
    if person.department == "業務部":
        return 2 if on >= REMOTE_AMENDMENT_EFFECTIVE else 1
    return REMOTE_DAYS.get(person.department, REMOTE_DAYS["其他"])


# ---------- 國內差旅住宿上限（FIN-01 第 4 條；FIN-04 修訂公告只改住宿，2026/1/1 起）----------
LODGING = {
    "old": {"P1-P3": (2400, 2000), "P4-P5": (3000, 2500), "M": (3800, 3200)},
    "new": {"P1-P3": (2800, 2300), "P4-P5": (3400, 2800), "M": (4200, 3500)},
}
LODGING_REVISION_EFFECTIVE = date(2026, 1, 1)
DOMESTIC_MEAL_PER_DAY = 600  # FIN-01 第 5 條，修訂公告沒有改


def level_band(level: str) -> str:
    if level.startswith("M"):
        return "M"
    return "P1-P3" if int(level[1]) <= 3 else "P4-P5"


def domestic_lodging(level: str, city: str, on: date = TODAY) -> int:
    # 實習生因公出差準用 P1（FIN-01 第 2 條）
    table = LODGING["new"] if on >= LODGING_REVISION_EFFECTIVE else LODGING["old"]
    taipei, other = table[level_band(level)]
    return taipei if city == "台北" else other


# ---------- 國外差旅（FIN-02）----------
def business_class(level: str, one_way_hours: float) -> bool:
    # 兩個條件同時成立：單程表定飛行 8 小時以上「且」職級 M2 以上
    return one_way_hours >= 8 and level in ("M2", "M3")


# ---------- 費用報銷（FIN-03）----------
def reimbursement_deadline(days_after: int) -> str:
    if days_after <= 30:
        return "正常提出"
    if days_after <= 60:
        return "須附直屬主管加簽說明"
    return "不予受理"


def reimbursement_approvers(amount: int) -> list[str]:
    return ["直屬主管", "財務長"] if amount >= 10000 else ["直屬主管"]


# ---------- 資訊設備（IT-01）、資安（IT-02）、軟體採購（IT-03）----------
def laptop_refresh_years(person: Person) -> int | None:
    if person.employment == "實習生":
        return None  # 實習生用公用借用機，不配發
    return 2 if person.department == "研發部" else 3


def monitors(person: Person) -> int:
    return 2 if person.department == "研發部" else 1


def byod_email_allowed(person: Person) -> bool:
    return person.department != "研發部"


def software_approvers(unit_price: int) -> list[str]:
    return ["部門主管"] if unit_price <= 5000 else ["資訊部審核", "部門主管"]


# ---------- 福利（GEN-03）、教育訓練（GEN-04）、停車（GEN-02）----------
def health_check_subsidy(person: Person) -> int:
    if person.employment == "實習生":
        return 0
    if person.employment == "約聘":
        return 2000
    return 5000 if person.age >= 40 else 3000


def wedding_gift(person: Person) -> int:
    return 6000 if person.employment == "正職" else 0


def training_cap(person: Person) -> int:
    # 限到職滿 1 年的正職
    if person.employment != "正職" or person.months < 12:
        return 0
    return {"P1-P3": 15000, "P4-P5": 25000, "M": 40000}[level_band(person.level)]


def taipei_parking_eligible(person: Person, pregnant: bool = False, disabled: bool = False) -> bool:
    return person.site == "台北" and (person.level in ("M1", "M2", "M3") or pregnant or disabled)
