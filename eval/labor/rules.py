# 勞動法規知識庫的設定（docs/decisions/031）
#
# 企業的 rules.py 是「規則表」：虛構的規定先寫成程式，文件和答案都從這裡產生
# 勞動法規是真的法規，答案從條文讀出來，所以這裡只放評估要用的設定

from datetime import date

from eval.labor.questions import EVIDENCE

# 評估的「今天」：民國 115 年 10 月 24 日。勞工請假規則 10 月 1 日起婚假改成 14 日，剛好已經生效
TODAY = date(2026, 10, 24)

# 轉成 PDF 之後要讀得到的字（eval/build_pdfs.py 檢查）：每題依據的條文原文
FACTS = sorted({(code, quote) for quotes in EVIDENCE.values() for code, _, quote in quotes})
