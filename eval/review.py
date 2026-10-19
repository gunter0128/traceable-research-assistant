# 人工檢查題目的網頁（B3）
# 用法（專案資料夾）：.venv\Scripts\python -m eval.review，然後打開 http://localhost:8765
#
# 一題一題看：題目、證據原文（附前後文）、重點清單。可以直接改字，按「保留」或「刪除」
# 每次按下去就存檔（eval/data/review.json），關掉再開會接著上次的進度
# 按「匯出」把保留的題目寫成正式的評估資料集 eval/data/dataset.jsonl

import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from eval.corpus import load_papers, locate

DATA = Path(__file__).parent / "data"
CANDIDATES = DATA / "candidates.jsonl"
STATE = DATA / "review.json"
DATASET = DATA / "dataset.jsonl"

app = FastAPI()
items = [json.loads(line) for line in CANDIDATES.read_text(encoding="utf-8").splitlines() if line.strip()]
state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
papers = {p.name: p for p in load_papers()}


def context_of(paper_name: str, quote: str, around: int = 400) -> dict:
    # 證據原句的前後文：只看一句很難判斷題目合不合理，附上前後各 400 個字元
    # 切成 before / match / after 三段給網頁，證據句才標得出來
    # 用 locate 找位置（忽略標點、空白、斷字），原文和證據句的寫法不一定完全一樣
    paper = papers.get(paper_name)
    if paper is None:
        return {"before": "", "match": f"（沒有這篇論文：{paper_name}）" + quote, "after": ""}
    flat = " ".join(paper.text.split())
    found = locate(flat, quote)
    if found is None:
        return {"before": "", "match": "（原文裡找不到這句）" + quote, "after": ""}
    at, end = found
    return {"before": flat[max(0, at - around) : at], "match": flat[at:end], "after": flat[end : end + around]}


def merged(item: dict) -> dict:
    # 候選題目 + 檢查時改過的內容
    edits = state.get(item["id"], {})
    return {
        **item,
        "status": edits.get("status", "pending"),
        "question": edits.get("question", item["question"]),
        "key_points": edits.get("key_points", item["key_points"]),
        "note": edits.get("note", ""),
        "contexts": [context_of(ev["paper"], ev["quote"]) for ev in item["evidence"]],
    }


class Decision(BaseModel):
    status: str  # keep / delete / pending
    question: str
    key_points: list[str]
    note: str = ""


@app.get("/api/items")
def list_items():
    return [merged(i) for i in items]


@app.post("/api/items/{item_id}")
def decide(item_id: str, decision: Decision):
    state[item_id] = decision.model_dump()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True}


@app.post("/api/export")
def export():
    kept = []
    for item in items:
        m = merged(item)
        if m["status"] == "keep":
            kept.append({k: m[k] for k in ("id", "type", "language", "question", "evidence", "key_points", "should_refuse", "note")})
    DATASET.write_text("\n".join(json.dumps(k, ensure_ascii=False) for k in kept) + "\n", encoding="utf-8")
    return {"count": len(kept), "path": str(DATASET)}


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>評估題目檢查</title>
<style>
body{margin:0;font-family:'Microsoft JhengHei',sans-serif;background:#f3f4f6;color:#111827;display:flex;height:100vh}
#list{width:230px;overflow:auto;background:white;border-right:1px solid #e5e7eb}
#list div{padding:6px 12px;cursor:pointer;font-size:13px;border-left:4px solid transparent}
#list div.cur{background:#eef2ff}
#list .keep{border-left-color:#16a34a}#list .delete{border-left-color:#dc2626;color:#9ca3af;text-decoration:line-through}
main{flex:1;overflow:auto;padding:24px 32px}
.tag{display:inline-block;padding:2px 10px;border-radius:12px;font-size:13px;margin-right:6px;background:#e5e7eb}
textarea{width:100%;box-sizing:border-box;font:inherit;font-size:16px;padding:10px;border:1px solid #d1d5db;border-radius:8px}
.ev{background:white;border:1px solid #e5e7eb;border-radius:8px;padding:12px;margin:8px 0;font-size:14px;line-height:1.6}
.ev b{color:#2563eb}.ev mark{background:#fef08a}
.warn{background:#fef2f2;color:#b91c1c;padding:8px 12px;border-radius:8px;margin:8px 0}
.bar{position:sticky;bottom:0;background:#f3f4f6;padding:12px 0;display:flex;gap:10px;align-items:center}
button{font:inherit;padding:8px 18px;border-radius:8px;border:none;cursor:pointer}
.k{background:#16a34a;color:white}.d{background:#dc2626;color:white}.n{background:#e5e7eb}
h3{margin:22px 0 6px;font-size:15px;color:#4b5563}
</style></head><body>
<div id="list"></div><main id="main"></main>
<script>
const TYPES={fact:'事實',explain:'理解解釋',summary:'總結',compare:'跨篇比較',numeric:'數字',no_answer:'沒有答案'};
let items=[],cur=0;
const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function load(){items=await (await fetch('/api/items')).json();const p=items.findIndex(i=>i.status==='pending');cur=p<0?0:p;render()}
function renderList(){document.getElementById('list').innerHTML=items.map((it,i)=>
 `<div class="${it.status} ${i===cur?'cur':''}" onclick="go(${i})">${it.id} ${TYPES[it.type]} ${it.language}</div>`).join('')}
function render(){
 renderList();const it=items[cur];const done=items.filter(i=>i.status!=='pending').length;
 const evs=it.evidence.map((e,j)=>{const c=it.contexts[j];
  return `<div class="ev"><b>${e.paper}</b><br>…${esc(c.before)}<mark>${esc(c.match)}</mark>${esc(c.after)}…</div>`}).join('')||'<div class="ev">（沒有證據：這題文件裡應該找不到答案）</div>';
 document.getElementById('main').innerHTML=`
 <div><span class="tag">${it.id}</span><span class="tag">${TYPES[it.type]}</span><span class="tag">${it.language}</span>
 ${it.should_refuse?'<span class="tag">應該拒答</span>':''}<span class="tag">進度 ${done} / ${items.length}</span></div>
 ${it.problems.length?'<div class="warn">⚠ '+it.problems.map(esc).join('<br>')+'</div>':''}
 <h3>題目</h3><textarea id="q" rows="2">${esc(it.question)}</textarea>
 <h3>證據原文（黃色是證據句，前後是上下文）</h3>${evs}
 <h3>重點清單（一行一點）</h3><textarea id="kp" rows="5">${esc(it.key_points.join('\\n'))}</textarea>
 <h3>備註（選填：為什麼刪、哪裡不好）</h3><textarea id="note" rows="2">${esc(it.note)}</textarea>
 <div class="bar"><button class="k" onclick="save('keep')">保留（K）</button><button class="d" onclick="save('delete')">刪除（D）</button>
 <button class="n" onclick="go(cur-1)">← 上一題</button><button class="n" onclick="go(cur+1)">下一題 →</button>
 <span style="flex:1"></span><button class="n" onclick="exp()">匯出資料集</button></div>`}
async function save(status){const it=items[cur];
 it.status=status;it.question=document.getElementById('q').value;
 it.key_points=document.getElementById('kp').value.split('\\n').map(s=>s.trim()).filter(Boolean);
 it.note=document.getElementById('note').value;
 await fetch('/api/items/'+it.id,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({status,question:it.question,key_points:it.key_points,note:it.note})});
 go(cur+1)}
function go(i){if(i<0||i>=items.length){render();return}cur=i;render();window.scrollTo(0,0)}
async function exp(){const r=await (await fetch('/api/export',{method:'POST'})).json();alert('已匯出 '+r.count+' 題到 '+r.path)}
document.addEventListener('keydown',e=>{if(e.target.tagName==='TEXTAREA')return;
 if(e.key==='k')save('keep');if(e.key==='d')save('delete');if(e.key==='ArrowRight')go(cur+1);if(e.key==='ArrowLeft')go(cur-1)});
load();
</script></body></html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
