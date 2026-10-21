"""Find and read chemical drawings on page images. Runs in .venv-decimer (Python 3.10).

    python ocsr/decimer_worker.py job.json result.json

job:    {"pages": [{"page": 1, "image": "p1.png"}], "out_dir": "...", "label_gap": 125}
result: [{"page": 1, "bbox": [x0, y0, x1, y1], "image": "p1_0.png", "ocr": ["IT-M", ...], "decimer": "CC..."}]

For each page: DECIMER Segmentation cuts out every chemical drawing, RapidOCR reads the text
in and just below each drawing (where its label is), and DECIMER reads the drawing as SMILES.
bbox is in pixels of the page image.
"""
import json
import sys
from pathlib import Path

import cv2
from decimer_segmentation import segment_chemical_structures
from DECIMER import predict_SMILES
from rapidocr_onnxruntime import RapidOCR


def main(job_path, result_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    gap = job.get("label_gap", 125)
    ocr = RapidOCR()
    results = []
    for p in job["pages"]:
        page_img = cv2.imread(p["image"])
        segments, bboxes = segment_chemical_structures(page_img, expand=True, return_bboxes=True)
        h, w = page_img.shape[:2]
        for n, (segment, (y0, x0, y1, x1)) in enumerate(zip(segments, bboxes)):
            crop = out_dir / f"p{p['page']}_{n}.png"
            cv2.imwrite(str(crop), segment)
            pad = int((x1 - x0) * 0.1)
            around = page_img[y0:min(h, y1 + gap), max(0, x0 - pad):min(w, x1 + pad)]
            found, _ = ocr(around)
            try:
                smiles = predict_SMILES(str(crop))
            except Exception as e:  # one unreadable drawing shouldn't stop the page
                smiles, error = None, str(e)
            else:
                error = None
            results.append({"page": p["page"], "bbox": [int(x0), int(y0), int(x1), int(y1)], "image": str(crop),
                            "ocr": [t for _, t, _ in found or []], "decimer": smiles, "error": error})
    Path(result_path).write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:3])
