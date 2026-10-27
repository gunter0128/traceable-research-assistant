# 把 adapter 合併進底模，存成一份完整的模型給 vLLM 載入（docs/decisions/040）
#
# 為什麼要合併：有人回報 vLLM 直接掛 adapter，在 Qwen3.5 的架構上不會生效（不報錯，但回答跟沒掛一樣）
# 合併 = 每個掛了 adapter 的權重 W 改成 W + (alpha / r) × B × A；其他權重、設定檔、斷字器、對話格式原封不動複製
#   一次讀一個權重檔處理，不用把整個模型載進記憶體
# 確認合併沒做錯：train_adapter.py --eval-only 合併後的模型，驗證集的 loss 要跟訓練時最好的那輪一樣
#
# 用法（實驗室的訓練環境；底模已經被 vLLM 下載過，不會再下載）：
#   python merge_adapter.py adapters/taipei-50 merged/taipei-50
# 一份約 19GB：考完試就可以刪，adapter 本身留在 adapters/ 和 MLflow

import argparse
import json
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download
from safetensors.torch import load_file, save_file

BASE = "Qwen/Qwen3.5-9B"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("adapter")
    parser.add_argument("out")
    args = parser.parse_args()
    adapter, out = Path(args.adapter), Path(args.out)

    config = json.loads((adapter / "adapter_config.json").read_text())
    if config.get("use_rslora"):
        raise SystemExit("rsLoRA 的縮放不是 alpha / r，這支程式沒處理")
    scale = config["lora_alpha"] / config["r"]
    lora = load_file(adapter / "adapter_model.safetensors")
    # PEFT 存的名字是 base_model.model.<原本的權重名稱去掉 .weight>.lora_A.weight
    pairs = {key.removeprefix("base_model.model.").replace(".lora_A.weight", ".weight"):
             (lora[key], lora[key.replace(".lora_A.", ".lora_B.")])
             for key in lora if ".lora_A." in key}

    base = Path(snapshot_download(BASE, local_files_only=True))
    # 先對名字：權重的索引列了底模每個權重在哪個檔，對不上就不要開始寫
    names = json.loads((base / "model.safetensors.index.json").read_text())["weight_map"]
    if missing := [name for name in pairs if name not in names]:
        raise SystemExit(f"有 {len(missing)} 個 adapter 的權重在底模裡找不到，例如 {missing[:3]}")
    out.mkdir(parents=True, exist_ok=False)
    merged, biggest = 0, 0.0
    for file in sorted(base.iterdir()):
        if file.is_dir():
            continue
        if file.suffix != ".safetensors":
            shutil.copy(file, out / file.name)  # 設定檔、斷字器、對話格式、權重的索引
            continue
        tensors = load_file(file)
        for name in tensors:
            if name in pairs:
                a, b = pairs.pop(name)
                delta = (b.float() @ a.float()) * scale  # 用 32 位元算，加完再轉回原本的 bf16
                tensors[name] = (tensors[name].float() + delta).to(tensors[name].dtype)
                merged += 1
                biggest = max(biggest, delta.abs().max().item())
        save_file(tensors, out / file.name, metadata={"format": "pt"})
    print(f"合併了 {merged} 個權重，改動最大的一個數字 {biggest:.2e}；存在 {out}")


if __name__ == "__main__":
    main()
