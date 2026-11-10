#!/usr/bin/env bash
# 地端版一個指令開全部（docs/decisions/045 的「第一階段怎麼啟動」）
#   1. 開轉向量的模型（vLLM），等它回應
#   2. 開回答的模型（vLLM + 第 1 版 adapter），等它回應：兩個同時開會搶顯示卡記憶體，所以一個一個來
#   3. docker compose up -d：資料庫、儲存、API、網頁，等 API 回應
# 模型直接用主機上裝好的 vLLM 跑，不用 --profile gpu（vLLM 的 image 約 20GB，實驗室的硬碟放不下）
#
# 用法（在 onprem/ 資料夾）：
#   ./start.sh            平常開機後
#   ./start.sh --build    git pull 之後（重新 build API、網頁的 image）
# 已經在跑的模型會跳過、不會重複開（顯示卡是共用的）；這個腳本開的模型才記編號，stop.sh 只關這些
# 主機上的路徑填在 onprem/.env：VLLM_BIN、MODELS_DIR、ADAPTERS_DIR（見 .env.example）

# -e：任何一行失敗就停；-u：用到沒設定的變數就停；pipefail：管線裡任何一段失敗都算失敗
set -euo pipefail
# 不管從哪裡執行，都先切到這個檔案所在的 onprem/（.env、docker-compose.yml、logs/ 都在這裡）
cd "$(dirname "$0")"

BUILD=""
if [ "${1:-}" = "--build" ]; then
  BUILD="--build"
fi

if [ ! -f .env ]; then
  echo "onprem/.env 不存在：先複製 .env.example 成 .env 再填"
  exit 1
fi

# 從 .env 讀一個值。只讀需要的那幾個，不把整個 .env 載進來（裡面有密碼，不必傳給模型的程式）
env_value() {
  grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true
}

VLLM_BIN=$(env_value VLLM_BIN)
MODELS_DIR=$(env_value MODELS_DIR)
ADAPTERS_DIR=$(env_value ADAPTERS_DIR)
for name in VLLM_BIN MODELS_DIR ADAPTERS_DIR; do
  # ${!name}：取「名字叫 $name 的變數」的值
  if [ -z "${!name}" ]; then
    echo "onprem/.env 沒有填 $name（見 .env.example）"
    exit 1
  fi
done
if [ ! -x "$VLLM_BIN" ]; then
  echo "找不到 vLLM：$VLLM_BIN"
  exit 1
fi

# ---------- 兩個模型的參數：跟 docker-compose.yml 的 vllm-embed、vllm-answer 一樣 ----------
# 一行一個參數，tests/test_onprem_start.py 會逐行跟 docker-compose.yml 比對，改一邊沒改另一邊就會失敗
EMBED_ARGS=(
  Qwen/Qwen3-Embedding-0.6B
  --served-model-name=qwen3-embedding
  --runner=pooling
  --max-model-len=2048
  --gpu-memory-utilization=0.10
  --enforce-eager
  --port=8001
)
ANSWER_ARGS=(
  Qwen/Qwen3.5-9B
  --served-model-name=qwen3.5-9b
  --max-model-len=16384
  --gpu-memory-utilization=0.84
  '--limit-mm-per-prompt={"image": 0, "video": 0}'
  --max-num-seqs=4
  --max-num-batched-tokens=8192
  --enforce-eager
  --reasoning-parser=qwen3
  --enable-lora
  --max-lora-rank=16
  "--lora-modules=taipei-all-e1=$ADAPTERS_DIR/taipei-all/epoch-1"
  --port=8000
)

mkdir -p logs

# 等某個網址回應指定的狀態碼，最多等 limit 秒
# pid 有給的話，順便看那個程式還在不在：已經結束了就不用再等，直接印 log 的最後幾行
wait_for() {
  local url=$1 expected=$2 limit=$3 pid=$4 name=$5 waited=0 status
  while [ "$waited" -lt "$limit" ]; do
    status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" || true)
    if [ "$status" = "$expected" ]; then
      echo "  $name 好了（等了 $waited 秒）"
      return 0
    fi
    # kill -0：不送任何訊號，只問「這個程式還在不在」
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      echo "  $name 啟動失敗，onprem/logs/$name.log 的最後 20 行："
      tail -n 20 "logs/$name.log"
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "  $name 等了 $limit 秒還沒好，看 onprem/logs/$name.log"
  exit 1
}

# 開一個模型：已經在跑（/health 有回應）就跳過
# "$@"：剩下的參數原封不動交給 vllm serve
start_model() {
  local name=$1 port=$2 pid
  shift 2
  # -f：回應是錯誤的狀態碼（例如別的程式回 404）也算失敗，才不會把別人的服務當成模型
  if curl -sf -o /dev/null --max-time 5 "http://localhost:$port/health"; then
    echo "$name 已經在跑（port $port），跳過"
    return 0
  fi
  echo "開 $name（port $port），log 在 onprem/logs/$name.log"
  # HF_HOME：模型檔在哪；HF_HUB_OFFLINE=1：找不到模型檔就直接報錯，不會在共用主機上下載十幾 GB
  # nohup ... &：丟到背景跑，關掉 SSH 連線也不會跟著停
  HF_HOME="$MODELS_DIR" HF_HUB_OFFLINE=1 nohup "$VLLM_BIN" serve "$@" > "logs/$name.log" 2>&1 &
  pid=$!
  echo "$pid" > "logs/$name.pid"
  wait_for "http://localhost:$port/health" 200 600 "$pid" "$name"
}

start_model embed 8001 "${EMBED_ARGS[@]}"
# VLLM_USE_FLASHINFER_SAMPLER=0：實驗室沒有 C++ 編譯器，設定 top_k 時 vLLM 預設會現場編譯一個元件，編不起來整個服務會停掉（036）
# 只給回答的模型：export 之後開的程式都會帶到這個設定，開完就 unset，後面的 docker compose 不帶
export VLLM_USE_FLASHINFER_SAMPLER=0
start_model answer 8000 "${ANSWER_ARGS[@]}"
unset VLLM_USE_FLASHINFER_SAMPLER

# ${BUILD:+ $BUILD}：BUILD 有值才印「空格 + 值」，沒有就什麼都不印
echo "開資料庫、儲存、API、網頁（docker compose up -d${BUILD:+ $BUILD}）"
# $BUILD 不加引號：沒有 --build 時是空的，就當作沒這個參數
docker compose up -d $BUILD
# 經過 nginx 打 API：沒登入應該回 401，代表網頁、API、資料庫都通了
wait_for "http://127.0.0.1:8080/auth/me" 401 180 "" "網頁和 API"

echo ""
echo "全部起來了。在筆電開 SSH 通道：ssh -N -L 8080:localhost:8080 <帳號@這台主機>"
echo "再用瀏覽器打開 http://localhost:8080"
