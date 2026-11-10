# onprem/start.sh 在主機上直接開兩個 vLLM，onprem/docker-compose.yml 的 --profile gpu 在容器裡開同樣的兩個
# 兩邊的參數必須一樣（docs/decisions/045）：之後從 start.sh 換成 --profile gpu 時，模型的行為才不會變
# 這裡把兩邊的參數讀出來逐一比對，改了一邊、另一邊沒跟上，測試就會失敗
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ONPREM = Path(__file__).parent.parent / "onprem"
START = ONPREM / "start.sh"
STOP = ONPREM / "stop.sh"
COMPOSE = ONPREM / "docker-compose.yml"
# 主機上的 adapter 資料夾（start.sh）在容器裡掛在 /adapters（docker-compose.yml）
HOST_TO_CONTAINER = {"$ADAPTERS_DIR": "/adapters"}


# 從 start.sh 讀出 EMBED_ARGS=( ... ) 這種陣列：一行一個參數，去掉外面的引號
def _script_args(name: str) -> list[str]:
    found = re.search(rf"^{name}=\($(.*?)^\)$", START.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    assert found, f"start.sh 裡找不到 {name}"
    args = []
    for line in found.group(1).strip().splitlines():
        arg = line.strip()
        if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "'\"":
            arg = arg[1:-1]
        for host, container in HOST_TO_CONTAINER.items():
            arg = arg.replace(host, container)
        args.append(arg)
    return args


def _compose_service(name: str) -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][name]


@pytest.mark.parametrize(
    ("array", "service"),
    [("EMBED_ARGS", "vllm-embed"), ("ANSWER_ARGS", "vllm-answer")],
)
def test_model_args_match_docker_compose(array, service):
    assert _script_args(array) == _compose_service(service)["command"]


# 容器裡給模型的環境變數，start.sh 也要給（HF_HOME 例外：主機上是 .env 的 MODELS_DIR，下面另外檢查）
@pytest.mark.parametrize("service", ["vllm-embed", "vllm-answer"])
def test_model_environment_matches_docker_compose(service):
    script = START.read_text(encoding="utf-8")
    for key, value in _compose_service(service)["environment"].items():
        if key == "HF_HOME":
            assert 'HF_HOME="$MODELS_DIR"' in script
        else:
            assert f"{key}={value}" in script, f"start.sh 開模型時少了 {key}={value}"


# 回答的模型一定要帶 VLLM_USE_FLASHINFER_SAMPLER=0（實驗室沒有 C++ 編譯器，036）：要在開回答的模型之前設定
def test_flashinfer_sampler_is_off_when_starting_the_answer_model():
    script = START.read_text(encoding="utf-8")
    assert script.index("export VLLM_USE_FLASHINFER_SAMPLER=0") < script.index("start_model answer")


# bash -n：只檢查語法、不執行。Windows 上的 bash 可能是 WSL 的啟動程式，不一定能用，只在 Linux／macOS（CI）跑
@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="需要 Linux／macOS 的 bash")
@pytest.mark.parametrize("script", [START, STOP], ids=["start.sh", "stop.sh"])
def test_scripts_have_valid_bash_syntax(script):
    # check=False：失敗時不要直接丟例外，下一行自己檢查，失敗訊息裡才看得到 bash 說哪一行有錯
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
