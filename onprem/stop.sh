#!/usr/bin/env bash
# 關掉 start.sh 開的東西（docs/decisions/045 的「第一階段怎麼啟動」）
#   資料庫、儲存、API、網頁：docker compose stop，只是停下來，資料都留著（不是 down -v）
#   模型：只關 start.sh 自己開的（logs/*.pid 記了編號）；別人手動開的不碰（顯示卡是共用的）
#
# 用法（在 onprem/ 資料夾）：
#   ./stop.sh                全部關
#   ./stop.sh --keep-models  只關網頁那些，模型留著（例如只是要更新程式）

set -euo pipefail
cd "$(dirname "$0")"

echo "停資料庫、儲存、API、網頁（docker compose stop，資料留著）"
docker compose stop

if [ "${1:-}" = "--keep-models" ]; then
  echo "模型留著沒關"
  exit 0
fi

for name in answer embed; do
  pidfile="logs/$name.pid"
  if [ ! -f "$pidfile" ]; then
    echo "$name 不是 start.sh 開的（或已經關了），不動"
    continue
  fi
  pid=$(cat "$pidfile")
  # 確認這個編號現在還是 vLLM：程式結束後編號可能被系統分給別的程式，不能直接關
  if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= | grep -q "vllm serve"; then
    # 一般的結束訊號（SIGTERM）：vLLM 會自己收尾、把顯示卡記憶體放掉
    kill "$pid"
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "$name（$pid）30 秒還沒結束，看一下 onprem/logs/$name.log；真的卡住再用 kill -9 $pid"
      continue
    fi
    echo "已關 $name（$pid）"
  else
    echo "$name（$pid）已經不在了"
  fi
  rm -f "$pidfile"
done
