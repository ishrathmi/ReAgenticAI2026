# Image tools: DECIMER and MolScribe

`python -m energy_extractor images` checks each extracted structure against the drawing of that compound in the paper. It needs two optional image-recognition tools, each in its own Python environment next to the main `.venv`:

| Environment | Tools | Disk |
|---|---|---|
| `.venv-decimer` | DECIMER Segmentation (finds drawings on a page), DECIMER (drawing → SMILES), RapidOCR (reads the label under a drawing) | about 3 GB incl. models |
| `.venv-molscribe` | MolScribe (drawing → SMILES, second opinion) | about 2.5 GB incl. the 1.1 GB checkpoint |

They're separate because their libraries conflict (DECIMER Segmentation needs TensorFlow ≤ 2.15, MolScribe needs old PyTorch-era packages), and because the main pipeline shouldn't need either. Both run on the CPU; no graphics card is needed. Each uses about 2–3 GB of memory while it runs, one after the other.

## Install (Windows, Python 3.10)

From the `energy_extractor/` folder:

```
py -3.10 -m venv .venv-decimer
.venv-decimer\Scripts\python -m pip install -r ocsr\requirements-decimer.txt

py -3.10 -m venv .venv-molscribe
.venv-molscribe\Scripts\python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
.venv-molscribe\Scripts\python -m pip install -r ocsr\requirements-molscribe.txt
```

On macOS/Linux use `python3.10 -m venv …` and `…/bin/python`. Model weights download on first use (DECIMER through pystow into `~/.data`, MolScribe from Hugging Face into `~/.cache/huggingface`).

If MolScribe isn't installed, `images` runs with DECIMER alone.

## Files

- `decimer_worker.py`: segmentation, label OCR and DECIMER reading, for the page images it's given.
- `molscribe_worker.py`: MolScribe reading of the cropped drawings.

Both take a JSON job file and write a JSON result; `energy_extractor/ocsr.py` runs them as subprocesses and caches the results in `data/cache/ocsr/`.
