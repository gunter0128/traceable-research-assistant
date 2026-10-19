# 評估用的論文集（B3）：用系統自己的解析和切段程式讀論文，跟系統實際看到的內容一樣
# 論文檔案本身不放進 git（有著作權），放在 EVAL_PAPERS_DIR 指定的資料夾，預設是桌面的 paper/

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.core import chunker, pdf_parser

PAPERS_DIR = Path(os.environ.get("EVAL_PAPERS_DIR", r"C:\Users\user\Desktop\paper"))

# 每篇一個簡短的名字（題目、證據裡用它），和它的分類。比對檔名裡的關鍵字
# 分類：attack-defense（攻擊與防禦）、conflict（知識衝突與拒答）、context（檢索結果的精簡與排序）、
#      multihop（多跳推理）、fact-check（事實查核與假新聞）
PAPERS = [
    ("NEW LENS ON RETRIEVAL", "SufficientContext", "conflict"),
    ("Prompt Compression", "LongLLMLingua", "context"),
    ("Multi-Hop Queries", "MultiHop-RAG", "multihop"),
    ("REPRESENTATIONEDITING", "CARE-RepEdit", "conflict"),
    ("Conflict-Aware Soft Prompting", "CARE-SoftPrompt", "conflict"),
    ("Evidence Tree Search", "ETS", "context"),
    ("Conflict-Driven Summarization", "CARE-RAG", "conflict"),
    ("GraphFC", "GraphFC", "fact-check"),
    ("COMPRESSION AND SELECTIVE", "RECOMP", "context"),
    ("IRCoT", "IRCoT", "multihop"),
    ("Incorporating Retrieval Information", "R2AG", "context"),
    ("Explicit Denoising", "InstructRAG", "conflict"),
    ("MAD-sherlock", "MAD-Sherlock", "fact-check"),
    ("MADAM-RAG", "MADAM-RAG", "conflict"),
    ("RobustRAG", "RobustRAG", "attack-defense"),
    ("Set Selection", "SetR", "context"),
    ("TED.pdf", "TED", "fact-check"),
    ("TrustRAG", "TrustRAG", "attack-defense"),
    ("Unifying Context Ranking", "RankRAG", "context"),
    ("context pruning", "Provence", "context"),
    ("poinsonedRAG", "PoisonedRAG", "attack-defense"),
]


@dataclass
class Paper:
    name: str  # 簡短的名字，例如 RobustRAG
    category: str
    filename: str  # 原本的檔名，上傳到系統時就是這個名字
    text: str  # 解析出來的全文
    body_chunks: list[str]  # 正文的段落（不含參考文獻和附錄），出題只從這裡挑


def normalize(text: str) -> str:
    # 顯示用：把連續的空白和換行變成一個空白
    return re.sub(r"\s+", " ", text).strip().lower()


def match_key(text: str) -> str:
    # 比對用：只留英文字母和數字。PDF 換行時會把單字斷開（mod- els），出題的 AI 複製時會自動接回去（models）；
    # 還有「R 2-Former」這種多出來的空白、彎引號和直引號的差別。全部拿掉再比，才不會明明有這句卻判定找不到
    return re.sub(r"[^a-z0-9]", "", text.lower())


def locate(text: str, quote: str) -> tuple[int, int] | None:
    # 證據句在原文裡的位置（用 match_key 比對，回傳原文的起訖位置），找不到回傳 None
    positions = [i for i, ch in enumerate(text) if re.match(r"[A-Za-z0-9]", ch)]
    key = "".join(text[i].lower() for i in positions)
    at = key.find(match_key(quote))
    if at < 0 or not match_key(quote):
        return None
    return positions[at], positions[at + len(match_key(quote)) - 1] + 1


def _body(text: str) -> str:
    # 參考文獻和附錄常佔全文一半以上，出題時不要從那裡挑（題目會變成「第 37 篇參考文獻是誰寫的」）
    # 找最後一個「References」標題，而且要在全文 30% 之後（避免抓到前面提到 references 的句子）
    lower = text.lower()
    candidates = [m.start() for m in re.finditer(r"\n\s*references\s*\n", lower)]
    candidates = [c for c in candidates if c > len(text) * 0.3]
    return text[: candidates[-1]] if candidates else text


def load_papers() -> list[Paper]:
    files = sorted(PAPERS_DIR.glob("*.pdf"))
    papers = []
    for keyword, name, category in PAPERS:
        matches = [f for f in files if keyword.lower() in f.name.lower()]
        if len(matches) != 1:
            raise RuntimeError(f"找不到、或找到不只一個符合「{keyword}」的檔案：{matches}")
        path = matches[0]
        with open(path, "rb") as f:
            text = pdf_parser.extract_text(f)
        papers.append(Paper(name, category, path.name, text, chunker.chunk_text(_body(text))))
    return papers
