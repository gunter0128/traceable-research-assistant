# 澄溪國小知識庫的評估題目（docs/decisions/017）
# 用法（專案資料夾）：.venv\Scripts\python -m eval.school.questions
# 產生 questions.jsonl（給評分程式用）和 questions.md（給人看）
#
# 這套整套都是「考試題」：只拿來確認企業題目上的改進，換到別的領域是不是一樣有效
# 改進的過程中不跑、不看，只在現在（基準）和改進全部做完之後各跑一次
#
# 問的人有老師也有家長：老師常常要回答家長的問題，也測「問的人是誰」會不會讓答案不同
# 標準答案裡的數字都用 rules.py 算出來；欄位的意思見 eval/common.py

from pathlib import Path

from eval.common import Question, money, write_question_files
from eval.school import rules as R
from eval.school.rules import Teacher

HERE = Path(__file__).parent

FULL_DAY_CAP = R.trip_fee_cap("全日")
OLD_FULL_DAY_CAP = R.TRIP_FEE_CAP["old"]["全日"]
FEE = R.after_school_fee()
OLD_FEE = R.AFTER_SCHOOL_FEE["old"]

QUESTIONS = [
    # ---------- 普通題：沒有條件，直接回答 ----------
    Question("g01", "general", "校外教學的家長同意書要在出發前幾天交？",
             f"出發前 {R.CONSENT_DAYS_BEFORE} 日回收；沒交同意書的學生不能參加，當天留校上課。",
             [f"出發前 {R.CONSENT_DAYS_BEFORE} 日"], ["S-05 第四點"]),
    Question("g02", "general", "孩子生病請假，連續幾天以上要附醫生證明？",
             f"病假連續 {R.SICK_PROOF_DAYS} 日以上（含）要附醫療院所證明。",
             [f"{R.SICK_PROOF_DAYS} 日以上"], ["S-06 一（三）"]),
    Question("g03", "general", "學期成績通知單什麼時候會寄？",
             f"學期結束後 {R.REPORT_CARD_DAYS} 日內寄給家長。",
             [f"{R.REPORT_CARD_DAYS} 日內"], ["S-04 第五點"]),

    # ---------- 條件題：答案依身分而不同，要把條件列出來 ----------
    Question("c01", "conditional", "老師一學期要輪幾週導護？",
             f"導師每學期 {R.duty_weeks(Teacher(role='導師'))} 週、科任 {R.duty_weeks(Teacher(role='科任'))} 週；"
             "兼任主任、組長免輪值。山海分校的老師也免輪值。",
             [f"導師 {R.duty_weeks(Teacher(role='導師'))} 週", f"科任 {R.duty_weeks(Teacher(role='科任'))} 週", "主任、組長免輪值"],
             ["S-03 第三點"]),
    Question("c02", "conditional", "校外教學要有幾個帶隊人員？",
             f"帶隊人員含導師和志工家長：一、二年級每 {R.students_per_chaperone(1)} 名學生至少 1 名，"
             f"三到六年級每 {R.students_per_chaperone(3)} 名至少 1 名，每班都要由導師帶隊。"
             f"山海分校是每 {R.students_per_chaperone(3, '山海分校')} 名至少 1 名。",
             [f"一、二年級每 {R.students_per_chaperone(1)} 名", f"三到六年級每 {R.students_per_chaperone(3)} 名"],
             ["S-05 第六點"]),
    Question("c03", "conditional", "學期成績怎麼算？",
             "一、二年級不辦紙筆的定期評量，學期成績以平時評量計算；"
             f"三到六年級定期評量占 {R.grade_weights(3)['定期評量']}%、平時評量占 {R.grade_weights(3)['平時評量']}%。",
             ["一、二年級以平時評量計算", f"定期評量 {R.grade_weights(3)['定期評量']}%", f"平時評量 {R.grade_weights(3)['平時評量']}%"],
             ["S-04 第三點"],
             must_not=["所有年級都是定期評量 40%、平時評量 60%"]),

    # ---------- 身分題：問題裡帶著身分，要挑出適用他的那一條 ----------
    Question("p01", "persona", "我是四年級導師，班上 28 個學生，校外教學至少要幾個帶隊人員？",
             f"至少 {R.chaperones(28, 4)} 位（含導師）：三到六年級每 {R.students_per_chaperone(4)} 名學生至少 1 名。",
             [f"{R.chaperones(28, 4)} 位"], ["S-05 第六點"],
             must_not=[f"至少 {R.chaperones(28, 1)} 位（那是一、二年級的比例）"]),
    Question("p02", "persona", "我是科任老師，下週要請 2 天事假，要誰核准？",
             f"{'、'.join(R.leave_approvers(2))}核准（115 學年度要點：請假逾一日至三日由校長核准）。"
             f"另外事假要在 {R.NOTICE_SCHOOL_DAYS[115]} 個上課日前申請；科任的課以同領域教師互代為優先。",
             ["校長"], ["S-01 第四點"],
             must_not=[f"{'、'.join(R.leave_approvers(2, 114))}核准（那是 114 學年度舊要點的規定）"]),
    Question("p03", "persona", "我家是低收入戶，孩子參加課後照顧班一個月要付多少？",
             f"免費（每月 {money(R.after_school_fee('低收入戶'))}）。低收入戶、中低收入戶學生免費，10 月的收費調整沒有改變減免規定。",
             ["免費"], ["S-06 二（四）"],
             must_not=[f"要付 {money(FEE)}", f"要付 {money(OLD_FEE)}"]),

    # ---------- 版本題：新舊規定衝突，要用現行的 ----------
    Question("v01", "version", "老師請事假要提前幾天申請？",
             f"{R.NOTICE_SCHOOL_DAYS[115]} 個上課日前，在校務系統申請（115 學年度要點）。"
             f"114 學年度要點的 {R.NOTICE_SCHOOL_DAYS[114]} 個上課日已經廢止。",
             [f"{R.NOTICE_SCHOOL_DAYS[115]} 個上課日前"], ["S-01 第三點"],
             must_not=[f"{R.NOTICE_SCHOOL_DAYS[114]} 個上課日前"]),
    Question("v02", "version", "校外教學全日活動，每個學生最多可以收多少錢？",
             f"{money(FULL_DAY_CAP)}（115 年 8 月 26 日修正；原本是 {money(OLD_FULL_DAY_CAP)}）。",
             [money(FULL_DAY_CAP)], ["S-05 修正紀錄"],
             must_not=[f"上限是 {money(OLD_FULL_DAY_CAP)}"]),
    Question("v03", "version", "現在課後照顧班一個月多少錢？",
             f"每月 {money(FEE)}（115 年 10 月起調整，原本 {money(OLD_FEE)}）。"
             f"低收入戶、中低收入戶免費，身心障礙學生半價。",
             [money(FEE)], ["S-07"],
             must_not=[f"現在是 {money(OLD_FEE)}"]),

    # ---------- 範圍陷阱：規定不適用於問的人 ----------
    Question("s01", "scope", "我是鐘點代課老師，請假要提前幾天申請？",
             "差假要點的請假規定不適用鐘點代課教師。鐘點代課教師無法到校授課時，"
             f"要在{R.HOURLY_SUBSTITUTE_NOTICE}通知教務處課務組，沒上的節數不支鐘點費。",
             ["不適用", f"{R.HOURLY_SUBSTITUTE_NOTICE}通知課務組"], ["S-01 第二點", "S-01 第七點"],
             must_not=[f"要在 {R.NOTICE_SCHOOL_DAYS[115]} 個上課日前申請"]),
    Question("s02", "scope", "我是山海分校的家長，課後照顧班幾點下課？",
             "山海分校不開辦課後照顧班，學生放學後由校車送回。",
             ["分校不開辦課後照顧班"], ["S-08 第三點"],
             must_not=[f"照顧到 {R.after_school_end(1)} 或 {R.after_school_end(3)}"]),
    Question("s03", "scope", "我是一年級導師，期中考的成績占學期成績多少？",
             "一、二年級不辦紙筆的定期評量（沒有期中考），學期成績以平時評量計算。",
             ["一、二年級不辦紙筆定期評量"], ["S-04 第二點、第三點"],
             must_not=[f"期中考（定期評量）占 {R.grade_weights(3)['定期評量']}%"]),

    # ---------- 跨文件題：要把兩份文件合起來看 ----------
    Question("x01", "cross_doc", "我是山海分校的老師，要帶 20 個學生去全日的校外教學，至少要幾個帶隊人員？學生自己要付的錢上限多少？",
             f"至少 {R.chaperones(20, 3, '山海分校')} 位（分校每 {R.students_per_chaperone(3, '山海分校')} 名學生至少 1 名）；"
             f"交通費由學校全額補助，門票、餐費等學生自付的費用適用本校要點的上限，全日活動每生 {money(FULL_DAY_CAP)}。",
             [f"{R.chaperones(20, 3, '山海分校')} 位", "交通費由學校全額補助", money(FULL_DAY_CAP)],
             ["S-08 第五點", "S-05 第五點及修正紀錄"],
             must_not=[f"至少 {R.chaperones(20, 3)} 位", f"上限 {money(OLD_FULL_DAY_CAP)}"]),
    Question("x02", "cross_doc", "領有身心障礙證明的學生參加課後照顧班，現在一個月多少錢？",
             f"{money(R.after_school_fee('身心障礙'))}：10 月起每月 {money(FEE)}，身心障礙學生半價（減免規定沒有變）。",
             [money(R.after_school_fee("身心障礙"))], ["S-07", "S-06 二（四）"],
             must_not=[f"現在是 {money(OLD_FEE // 2)}（那是調整前的半價）"]),
    Question("x03", "cross_doc", "低收入戶的學生參加校外教學和課後照顧班，要付錢嗎？",
             f"都不用：校外教學的費用由學校{R.trip_cost_note(low_income=True)}；課後照顧班低收入戶免費。",
             ["校外教學由學校全額補助", "課後照顧班免費"], ["S-05 第五點", "S-06 二（四）"]),

    # ---------- 沒有答案：文件沒寫，要老實說找不到 ----------
    # n01 特別：模型自己可能知道真實的教師請假規則（婚假 14 日），但文件裡沒有，答出來就是用了文件以外的知識
    Question("n01", "no_answer", "老師結婚可以請幾天婚假？",
             "文件中沒有寫婚假天數，差假要點只寫其他假別依教師請假規則辦理。",
             [], ["S-01 第八點"], must_not=["婚假 14 日"], should_refuse=True),
    Question("n02", "no_answer", "學校營養午餐一餐多少錢？",
             "文件中沒有午餐費用的資訊（工作要點只提到導師陪班級用餐）。", [], [], should_refuse=True),
    Question("n03", "no_answer", "這學期的校外教學要去哪裡？",
             "文件中沒有校外教學的地點，實施要點只規定辦理方式。", [], [], should_refuse=True),
]


def main():
    for q in QUESTIONS:
        q.split = "test"
    write_question_files(QUESTIONS, HERE, "澄溪國小知識庫評估題目", "python -m eval.school.questions")


if __name__ == "__main__":
    main()
