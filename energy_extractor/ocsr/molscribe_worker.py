"""Read cropped chemical drawings as SMILES with MolScribe. Runs in .venv-molscribe (Python 3.10).

    python ocsr/molscribe_worker.py job.json result.json

job:    {"images": ["p1_0.png", ...]}
result: [{"image": "p1_0.png", "molscribe": "CC...", "confidence": 0.93}]

The model checkpoint (1.1 GB) is downloaded from Hugging Face on first use and cached there.
"""
import json
import sys
from pathlib import Path

import torch
from huggingface_hub import hf_hub_download
from molscribe import MolScribe

CHECKPOINT = ("yujieq/MolScribe", "swin_base_char_aux_1m.pth")


def main(job_path, result_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    model = MolScribe(hf_hub_download(*CHECKPOINT), device=torch.device("cpu"))
    results = []
    for image in job["images"]:
        try:
            out = model.predict_image_file(image, return_confidence=True)
            results.append({"image": image, "molscribe": out.get("smiles"), "confidence": out.get("confidence")})
        except Exception as e:  # one unreadable drawing shouldn't stop the rest
            results.append({"image": image, "molscribe": None, "error": str(e)})
    Path(result_path).write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:3])
