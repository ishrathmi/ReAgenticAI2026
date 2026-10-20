"""Baseline: one plain LLM call, all papers at once.

Deliberately has none of the machinery in extract.py / graph.py — no graph, no
per-paper loop, no enforced schema, no type checking, no retries, no database.
Everything goes into a single OpenRouter chat completion and whatever comes back
is what you get. It exists to measure what the agent is actually buying.

    python baseline.py                      # all PDFs in papers/, as extracted text
    python baseline.py --mode native        # attach the PDFs themselves (small batches only)
    python baseline.py papers/a.pdf b.pdf   # specific files
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent   # the project root, one level up
HERE = Path(__file__).resolve().parent

load_dotenv(ROOT / ".env")
logging.getLogger("pypdf").setLevel(logging.CRITICAL)

OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
REQUEST_LIMIT = 32 * 1024 * 1024  # upstream per-request ceiling


def build_prompt(cfg: dict) -> str:
    lines = [cfg.get("instructions", "").strip(), "",
             "You are given several scientific papers at once. For EVERY paper, extract these fields:"]
    for f in cfg["fields"]:
        lines.append(f"- {f['name']} ({f['type']}): {f.get('description', '')}")
    lines += ["",
              "Return one record per distinct organism or culture, across all the papers.",
              "Use null for any value a paper does not report.",
              'Reply with JSON only: {"records": [{"source_file": "<the paper\'s file name>", ...fields...}]}']
    return "\n".join(lines).strip()


def as_text_block(pdf: Path) -> dict:
    pages = [p.extract_text() or "" for p in PdfReader(pdf).pages]
    return {"type": "text", "text": f'<document name="{pdf.name}">\n' + "\n".join(pages) + "\n</document>"}


def as_file_block(pdf: Path) -> dict:
    b64 = base64.standard_b64encode(pdf.read_bytes()).decode()
    return {"type": "file", "file": {"filename": pdf.name, "file_data": f"data:application/pdf;base64,{b64}"}}


def main() -> None:
    ap = argparse.ArgumentParser(description="Single-call baseline extractor (no agent, no validation).")
    ap.add_argument("inputs", nargs="*", type=Path, default=[ROOT / "papers"],
                    help="PDFs or folders (default: ../papers)")
    ap.add_argument("--fields", type=Path, default=ROOT / "fields.yaml")
    ap.add_argument("--model", default=None)
    ap.add_argument("--mode", choices=["text", "native"], default="text",
                    help="text: locally extracted text (default). native: attach the PDFs themselves")
    ap.add_argument("--out", type=Path, default=HERE / "baseline_output.json")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.fields.read_text())
    pdfs = []
    for p in args.inputs:
        pdfs += sorted(p.rglob("*.pdf")) if p.is_dir() else ([p] if p.suffix.lower() == ".pdf" else [])
    if not pdfs:
        sys.exit("No PDFs found.")

    blocks = [as_file_block(p) if args.mode == "native" else as_text_block(p) for p in pdfs]
    payload = sum(len(json.dumps(b)) for b in blocks)
    print(f"{len(pdfs)} papers, {args.mode} mode, ~{payload / 1e6:.1f} MB of message content")
    if args.mode == "native" and payload > REQUEST_LIMIT:
        sys.exit(f"Too large: {payload / 1e6:.0f} MB exceeds the {REQUEST_LIMIT / 1e6:.0f} MB request limit. "
                 "Use --mode text, or pass fewer files.")

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("Set OPENROUTER_API_KEY (in .env or your shell).")
    client = OpenAI(base_url=OPENROUTER_URL, api_key=key)

    started = time.time()
    resp = client.chat.completions.create(
        model=args.model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
        max_tokens=32000,
        messages=[
            {"role": "system", "content": build_prompt(cfg)},
            {"role": "user", "content": [*blocks, {"type": "text", "text": "Extract from all the papers above."}]},
        ],
    )
    elapsed = time.time() - started

    text = resp.choices[0].message.content or ""
    usage = resp.usage
    print(f"\nfinish_reason={resp.choices[0].finish_reason}  {elapsed:.0f}s  "
          f"in={usage.prompt_tokens:,} out={usage.completion_tokens:,} tokens")

    args.out.with_suffix(".raw.txt").write_text(text)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(cleaned)
        records = data["records"] if isinstance(data, dict) else data
        args.out.write_text(json.dumps(records, indent=2, ensure_ascii=False))
        print(f"Parsed {len(records)} records -> {args.out}")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"Could not parse the reply as JSON ({e}). Raw text saved to {args.out.with_suffix('.raw.txt')}")


if __name__ == "__main__":
    main()
