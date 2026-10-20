"""Keyword scoring that decides which chunks are worth sending to the model.

The keywords live in each profile's papers.yaml (prefilter.signals), so a teammate
can retarget the agent to another property without editing this file.
"""

from __future__ import annotations

import re


def compile_signals(signals: list) -> list:
    """signals: list of {pattern: <regex>, weight: <int>}"""
    return [(re.compile(s["pattern"], re.I), int(s.get("weight", 1))) for s in signals]


def score(text: str, compiled: list, required: str | None = None) -> int:
    total = sum(w * min(len(rx.findall(text)), 3) for rx, w in compiled)
    if required and not re.search(required, text, re.I):
        total = min(total, 1)          # the core term is missing: keep below any sensible threshold
    if text.count("et al") > 3 or re.search(r"^\s*references\s*$", text, re.I | re.M):
        total = 0                      # reference list
    return total


def select(chunks: list, cfg: dict) -> list:
    compiled = compile_signals(cfg["signals"])
    for c in chunks:
        c.score = score(c.text, compiled, cfg.get("required_pattern"))
    keep = [c for c in chunks if c.score >= cfg.get("min_score", 3)]
    keep.sort(key=lambda c: c.score, reverse=True)
    keep = keep[: cfg.get("max_chunks_per_file", 25)]
    order = {id(c): i for i, c in enumerate(chunks)}
    keep.sort(key=lambda c: order[id(c)])      # restore reading order
    return keep
