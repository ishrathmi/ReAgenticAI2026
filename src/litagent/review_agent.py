"""Review agent: a LangGraph loop that works on the problems the pipeline flagged.

The pipeline (agent.py) is the cheap first pass. Its checks flag records; this agent takes each
problem as a task, e.g. "these 12 records from SI Table 2 have no unit" or "table p7 table 1 is only
60% extracted", and lets a model decide which tool to call next:

  search_paper(query)                    full-text search over the paper and its SI
  read_section(section, query="")        e.g. the Methods section, for termini, units, salt forms
  read_nearby(chunk_id, before, after)   the passages around a chunk (captions, table continuations)
  find_sequence(label)                   sequence tables and passages that name the label
  re_extract(chunk_id, instruction)      extract again from one passage or table (missed rows)
  update_record(record_ids, field, value, quote)   change a field; the quote must be in the paper
  reject(record_ids, reason)             mark records as wrong; they move to a separate sheet
  accept(reason)                         finish the task

Rules enforced by code, not by the prompt: every change needs a verbatim quote that is found in the
paper; records are re-checked with the same domain checks after each change; each task has a step
limit; the agent has its own call budget. Every decision is logged (agent_step events) and timed.

Graph per task:   model --(tool calls)--> tools --(not finished, steps left)--> model
                    |                       |
                    +--(no tool call)--> END +--(accept / step limit)--> END
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage, ToolMessage, message_to_dict,
                                     messages_from_dict, messages_to_dict)
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from . import domains
from .agent import label_key, label_lookup, link_sequence, missing_cells, table_coverage
from .models import BudgetExceeded
from .tools.extract import build_system_prompt, coerce, extract, unit_in_source, verify_evidence

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM = """You review records that an automatic pipeline extracted from one scientific paper (main text and
supporting information). An automatic check flagged a problem with them. Use the tools to look in the
paper's documents, then fix what the documents support.

Rules:
- Use only information found in this paper's documents. Never guess, infer from general knowledge, or
  copy values between different peptides or conditions.
- Every change needs a verbatim quote from the documents; update_record checks the quote.
- If the documents do not contain the missing information, call accept and say so. An empty field is
  correct when the paper does not state the value.
- Refer to records by their id numbers. Change only the records in this task.
- You have at most {max_steps} tool calls for this task. Call accept as soon as you are done."""

TASK = """Paper: {title}{doi}
Files: {files}
Sections available: {sections}

PROBLEM: {reason}
{hint}
RECORDS IN THIS TASK ({n}):
{table}

Source passages of these records: {chunks}"""


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    steps: int
    done: bool


@dataclass
class Task:
    task_id: str
    paper_id: str
    source_file: str
    location: str | None
    reason: str
    trigger: str
    record_ids: list
    chunk_ids: list = field(default_factory=list)
    outcome: str = ""
    summary: str = ""
    trace: list = field(default_factory=list)
    steps: int = 0
    calls: int = 0
    seconds: float = 0.0


# --- model -----------------------------------------------------------------------------------

class AgentLLM:
    """Tool-calling chat model with a disk cache, a call budget and run logging.

    `chat` defaults to ChatOpenAI pointed at OpenRouter; tests pass a fake with .invoke(messages).
    """

    def __init__(self, model_id: str, tools: list, max_calls: int = 100, cache_dir: str = ".cache/agent",
                 log=None, chat=None, temperature=None, max_tokens: int | None = 4000):
        self.model = model_id
        self.max_calls, self.calls, self.cache_hits = max_calls, 0, 0
        self.log = log
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self._tool_names = sorted(t.name for t in tools)
        if chat is None:
            from langchain_openai import ChatOpenAI
            kw = dict(model=model_id, base_url=OPENROUTER_BASE_URL, api_key=os.environ.get("OPENROUTER_API_KEY"),
                      timeout=120, max_retries=3, extra_body={"usage": {"include": True}})
            if temperature is not None:              # some reasoning models reject a temperature setting
                kw["temperature"] = temperature
            if max_tokens:
                kw["max_tokens"] = max_tokens
            chat = ChatOpenAI(**kw)
        self.chat = chat.bind_tools(tools) if hasattr(chat, "bind_tools") else chat

    def invoke(self, messages: list) -> AIMessage:
        key = hashlib.sha256(json.dumps([self.model, self._tool_names, messages_to_dict(messages)],
                                        sort_keys=True, default=str).encode()).hexdigest()
        path = self.cache / f"{key}.json"
        if path.exists():
            self.cache_hits += 1
            if self.log:
                self.log.llm_call(0.0, None, cache_hit=True)
            return messages_from_dict([json.loads(path.read_text())])[0]
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"reached agent.max_calls={self.max_calls}")
        t = time.perf_counter()
        ai = self.chat.invoke(messages)
        latency = time.perf_counter() - t
        self.calls += 1
        um = getattr(ai, "usage_metadata", None) or {}
        raw = (getattr(ai, "response_metadata", None) or {}).get("token_usage") or {}
        usage = {"prompt_tokens": um.get("input_tokens", raw.get("prompt_tokens")),
                 "completion_tokens": um.get("output_tokens", raw.get("completion_tokens")),
                 "cost": raw.get("cost")}
        if self.log:
            self.log.llm_call(latency, usage, cache_hit=False)
        path.write_text(json.dumps(message_to_dict(ai), default=str))
        return ai


# --- the agent -------------------------------------------------------------------------------

class ReviewAgent:
    def __init__(self, cfg: dict, schema: dict, store, records: list, extract_model=None, log=None,
                 chat=None, root: Path | None = None, progress=None):
        self.progress = progress or (lambda event, **info: None)
        self.cfg, self.schema, self.store, self.records = cfg, schema, store, records
        self.acfg = cfg.get("agent") or {}
        self.extract_model = extract_model
        self.log = log
        self.root = root or Path.cwd()
        self.evidence_key = cfg.get("evidence_field", "evidence")
        self.enrich = domains.load(cfg.get("domain"))
        self.paper_flags = domains.load_paper_flags(cfg.get("domain"))
        self.passage_flags = domains.load_hook(cfg.get("domain"), "passage_flags", lambda text: {})
        self.repair_rules = domains.load_hook(cfg.get("domain"), "REPAIRS", {}) or {}
        self.extract_prompt = build_system_prompt(schema, cfg["task"], cfg.get("instructions", ""), self.evidence_key)
        self.model_fields = [k for k, v in schema.get("properties", {}).items() if not v.get("x-provenance")]
        self.max_steps = int(self.acfg.get("max_steps_per_task", 6))
        self.tools = self._build_tools()
        self.tool_map = {t.name: t for t in self.tools}
        self.llm = AgentLLM(self.acfg.get("model", "openai/gpt-6.1-sol"), self.tools,
                            int(self.acfg.get("max_calls", 100)),
                            str(self.root / self.acfg.get("cache_dir", ".cache/agent")), log, chat,
                            self.acfg.get("temperature"), self.acfg.get("max_output_tokens", 4000))
        self.graph = self._build_graph()
        self.task: Task | None = None
        self.invalid_calls = 0
        self.rejected: list = []
        self._lookups: dict = {}
        self._external: dict = {}          # record index -> problems not produced by enrich()

    # -- tasks ----------------------------------------------------------------------------------
    def build_tasks(self) -> list:
        """Group flagged records into tasks: one per (paper, file[, location], trigger)."""
        triggers = self.acfg.get("triggers") or []
        cap = int(self.acfg.get("max_records_per_task", 30))
        groups: dict = {}
        for i, r in enumerate(self.records):
            problems = [p for p in re.split(r";\s*", f"{r.get('review_reason') or ''}; {r.get('data_caveats') or ''}") if p]
            for trig in triggers:
                hit = next((p for p in problems if trig["match"].lower() in p.lower()), None)
                if not hit:
                    continue
                by = trig.get("group_by", "file")
                key = (r.get("paper_id"), r.get("source_file"), r.get("location") if by == "location" else None, trig["match"])
                groups.setdefault(key, {"reason": hit, "ids": []})["ids"].append(i)
        order = {t["match"]: n for n, t in enumerate(triggers)}
        tasks = []
        for (pid, f, loc, trig), g in sorted(groups.items(), key=lambda kv: (order[kv[0][3]], str(kv[0][1]), str(kv[0][2]))):
            if not pid:
                continue
            for k in range(0, len(g["ids"]), cap):
                ids = g["ids"][k:k + cap]
                chunk_ids = list(dict.fromkeys(self.records[i].get("chunk_id") for i in ids if self.records[i].get("chunk_id")))
                tasks.append(Task(f"T{len(tasks) + 1}", pid, f, loc, g["reason"], trig, ids, chunk_ids))
        return tasks[: int(self.acfg.get("max_tasks", 40))]

    def _hint(self, task: Task) -> str:
        hints = dict(self.acfg.get("hints") or {})
        text = next((v for k, v in hints.items() if k.lower() in task.reason.lower()), "")
        if not text:
            text = next((instr for k, (_, instr) in self.repair_rules.items() if k in task.reason), "")
        if "table only" in task.reason.lower() and task.chunk_ids:
            c = self.store.chunk(task.chunk_ids[0])
            recs = [r for r in self.records if r.get("chunk_id") == task.chunk_ids[0]]
            miss = missing_cells(c.text, recs, self.cfg.get("coverage") or {}) if c else []
            if miss:
                text += "\nValues in the table with no record (row | column | cell): " + \
                        "; ".join(f"{a} | {b} | {v}" for a, b, v in miss[:40])
        return f"What to check: {text}\n" if text else ""

    def _record_table(self, ids: list) -> str:
        cols = ["peptide_name", "sequence", "c_terminus", "solubility_operator", "solubility_value",
                "solubility_unit", "solubility_qualitative", "solvent", "temperature_c", "ph"]
        lines = ["id | " + " | ".join(cols) + " | location | chunk"]
        for i in ids:
            r = self.records[i]
            lines.append(f"{i} | " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols)
                         + f" | {r.get('location')} | {r.get('chunk_id')}")
        return "\n".join(lines)

    # -- running --------------------------------------------------------------------------------
    def run(self) -> dict:
        tasks = self.build_tasks()
        print(f"Review agent ({self.llm.model}): {len(tasks)} task(s)")
        self.progress("agent_tasks", n=len(tasks), model=self.llm.model)
        for n, t in enumerate(tasks, start=1):
            self.progress("agent_task_start", task=t.task_id, task_no=n, tasks=len(tasks), reason=t.reason,
                          file=t.source_file, records=len(t.record_ids))
            try:
                self.run_task(t)
            except BudgetExceeded as exc:
                t.outcome = "stopped: agent call budget reached"
                print(f"  [stop] {exc}")
                break
            print(f"  {t.task_id} [{t.outcome}] {t.reason[:60]} ({len(t.record_ids)} records, {t.steps} steps): {t.summary[:90]}")
            self.progress("agent_task_end", task=t.task_id, outcome=t.outcome, steps=t.steps, summary=t.summary)
        self._finalise(tasks)
        kept = [r for r in self.records if not r.get("_rejected")]
        self.rejected = [r for r in self.records if r.get("_rejected")]
        for r in self.records:
            r.pop("_rejected", None)
        return {"tasks": tasks, "records": kept, "rejected": self.rejected, "summary": self._summary(tasks, kept)}

    def run_task(self, task: Task) -> Task:
        self.task = task
        pid = task.paper_id
        for i in task.record_ids:
            self._remember_external(i)
        paper = self.store.paper(pid)
        files = ", ".join(f"{f['name']} ({f['role']})" for f in self.store.files(pid))
        human = TASK.format(title=paper["title"] or task.source_file, doi=f" (doi {paper['doi']})" if paper["doi"] else "",
                            files=files, sections=", ".join(self.store.sections(pid)) or "unknown",
                            reason=task.reason, hint=self._hint(task), n=len(task.record_ids),
                            table=self._record_table(task.record_ids), chunks=", ".join(map(str, task.chunk_ids)))
        msgs = [SystemMessage(SYSTEM.format(max_steps=self.max_steps)), HumanMessage(human)]
        t0 = time.perf_counter()
        calls0 = self.llm.calls
        if self.log:
            self.log.context = {"phase": "agent", "task": task.task_id, "file": task.source_file, "location": task.location}
            self.log.event("agent_task_start", task=task.task_id, reason=task.reason, records=len(task.record_ids),
                           file=task.source_file, location=task.location)
        state = self.graph.invoke({"messages": msgs, "steps": 0, "done": False},
                                  config={"recursion_limit": 2 * self.max_steps + 6})
        task.steps = state["steps"]
        if not task.outcome:
            task.outcome = "step limit" if state["steps"] >= self.max_steps else "ended without accept"
        task.calls = self.llm.calls - calls0
        task.seconds = round(time.perf_counter() - t0, 2)
        if self.log:
            self.log.event("agent_task_end", task=task.task_id, outcome=task.outcome, steps=task.steps,
                           calls=task.calls, seconds=task.seconds, summary=task.summary)
            self.log.context = {}
        return task

    def _build_graph(self):
        def call_model(state: AgentState):
            return {"messages": [self.llm.invoke(state["messages"])]}

        def run_tools(state: AgentState):
            last = state["messages"][-1]
            steps, out = state["steps"], []
            for tc in last.tool_calls:
                steps += 1
                name, args = tc.get("name"), tc.get("args") or {}
                tool = self.tool_map.get(name)
                stage = self.log.stage(f"tool:{name}", task=self.task.task_id) if self.log else _nullctx()
                with stage:
                    if tool is None:
                        result = f"Error: unknown tool '{name}'. Available: {', '.join(self.tool_map)}"
                        self.invalid_calls += 1
                    else:
                        try:
                            result = str(tool.invoke(args))
                        except Exception as exc:              # invalid arguments, failed checks
                            result = f"Error: {exc}"
                            self.invalid_calls += 1
                short = json.dumps(args, ensure_ascii=False, default=str)
                self.task.trace.append(f"{name}({short[:120]}) -> {result[:100]}")
                if self.log:
                    self.log.event("agent_step", task=self.task.task_id, step=steps, tool=name,
                                   args=args, result=result[:500])
                self.progress("agent_step", task=self.task.task_id, step=steps, tool=name, args=args, result=result[:300])
                out.append(ToolMessage(content=result[:6000], tool_call_id=tc["id"], name=name or "unknown"))
            return {"messages": out, "steps": steps, "done": bool(self.task.outcome)}

        def after_model(state: AgentState):
            last = state["messages"][-1]
            return "tools" if getattr(last, "tool_calls", None) else END

        def after_tools(state: AgentState):
            return END if state["done"] or state["steps"] >= self.max_steps else "model"

        g = StateGraph(AgentState)
        g.add_node("model", call_model)
        g.add_node("tools", run_tools)
        g.set_entry_point("model")
        g.add_conditional_edges("model", after_model, {"tools": "tools", END: END})
        g.add_conditional_edges("tools", after_tools, {"model": "model", END: END})
        return g.compile()

    # -- tools ----------------------------------------------------------------------------------
    def _fmt(self, c, limit: int = 1500) -> str:
        text = c.text if len(c.text) <= limit else c.text[:limit] + " …[truncated]"
        return f"[chunk {c.chunk_id} | {c.source_file} | {c.label} | section: {c.section}]\n{text}"

    def _check_ids(self, record_ids) -> list:
        ids = [int(i) for i in (record_ids if isinstance(record_ids, list) else [record_ids])]
        bad = [i for i in ids if i not in self.task.record_ids]
        if bad:
            raise ValueError(f"records {bad} are not in this task; use ids {self.task.record_ids[:40]}")
        return ids

    def _build_tools(self) -> list:
        agent = self

        def search_paper(query: str) -> str:
            """Search all files of this paper (main text and supporting information) for the given words.
            Returns up to 5 matching passages with their chunk_id."""
            hits = agent.store.search(agent.task.paper_id, query, limit=5)
            if not hits:
                return f"No passage contains: {query}"
            return "\n\n".join(f"[chunk {c.chunk_id} | {c.source_file} | {c.label} | section: {c.section}]\n…{snip}…"
                               for c, snip in hits)

        def read_section(section: str, query: str = "") -> str:
            """Read one section of the paper, e.g. 'methods', 'results', 'abstract', 'supporting information'.
            Optional query: keep only passages that contain all of these words (e.g. 'amide resin')."""
            chunks = agent.store.section(agent.task.paper_id, section)
            words = [w.lower() for w in re.findall(r"\w+", query or "")]
            if words:
                chunks = [c for c in chunks if all(w in c.text.lower() for w in words)]
            if not chunks:
                return (f"No passages in section '{section}'" + (f" containing '{query}'" if query else "")
                        + f". Sections: {', '.join(agent.store.sections(agent.task.paper_id))}")
            out, total = [], 0
            for c in chunks:
                block = agent._fmt(c)
                if total + len(block) > 5000:
                    out.append(f"[{len(chunks) - len(out)} more passages; narrow with query]")
                    break
                out.append(block)
                total += len(block)
            return "\n\n".join(out)

        def read_nearby(chunk_id: int, before: int = 1, after: int = 1) -> str:
            """Read a passage together with the passages just before and after it in the same file
            (useful for table captions, footnotes and tables split across chunks)."""
            c = agent.store.chunk(int(chunk_id))
            if not c or c.paper_id != agent.task.paper_id:
                return f"Error: chunk {chunk_id} is not part of this paper"
            near = agent.store.neighbours(int(chunk_id), min(int(before), 3), min(int(after), 3))
            return "\n\n".join(agent._fmt(x, 2500) for x in near)

        def find_sequence(label: str) -> str:
            """Find the amino-acid sequence of a peptide named by a label (e.g. 'variant 7', 'P3', 'compound 12'):
            looks in sequence tables of the paper and its SI, and in passages that mention the label."""
            pid = agent.task.paper_id
            key = label_key(label)
            out = []
            lookup = agent._lookup(pid)
            if key in lookup:
                seq, where = lookup[key]
                fname, _, clabel = where.partition(" ")
                c = agent.store.find_chunk(pid, fname, clabel)
                row = next((ln for ln in (c.text.split("\n") if c else []) if seq in ln), seq)
                out.append(f"Sequence table: label {key} -> {seq}\n  quote: \"{row}\" (chunk {c.chunk_id if c else '?'})")
            for c, snip in agent.store.search(pid, label, limit=5):
                for m in SEQ_RE.finditer(c.text):
                    s = m.group(0)
                    if len(s) >= 5 and abs(m.start() - c.text.lower().find(label.lower())) < 400:
                        lo = max(0, m.start() - 120)
                        out.append(f"Candidate near '{label}' in chunk {c.chunk_id} ({c.source_file} {c.label}): {s}\n"
                                   f"  context: …{c.text[lo:m.end() + 60]}…")
                        break
            return "\n".join(out[:6]) if out else f"No sequence found for '{label}' in this paper or its SI."

        def re_extract(chunk_id: int, instruction: str = "") -> str:
            """Run the extraction again on one passage or table of this paper (by chunk_id), for example to
            recover table rows that were missed. 'instruction' says what to focus on (e.g. 'rows 30% and 35%').
            New records are added to this task if they are not already in the database."""
            if agent.extract_model is None:
                return "Error: re_extract is not available (no extraction model)"
            c = agent.store.chunk(int(chunk_id))
            if not c or c.paper_id != agent.task.paper_id:
                return f"Error: chunk {chunk_id} is not part of this paper"
            prompt = agent.extract_prompt + (f"\n- For this passage: {instruction}" if instruction else "")
            paper = agent.store.paper(c.paper_id)
            found = extract(c.text, agent.schema, agent.extract_model, prompt, paper["title"] or "", c.label, c.kind,
                            agent.evidence_key, (agent.cfg.get("llm") or {}).get("max_output_tokens", 4000))
            existing = {_rec_key(r) for r in agent.records if r.get("source_file") == c.source_file}
            added = []
            pflags, paper_fl = agent.passage_flags(c.text), agent.paper_flags(agent.store.paper_text(c.paper_id))
            base = next((r for r in agent.records if r.get("paper_id") == c.paper_id), {})
            for rec in found:
                rec.update(pflags)
                rec.update(paper_fl)
                rec.update(doi=base.get("doi") or paper["doi"], paper_title=base.get("paper_title") or paper["title"],
                           source=base.get("source"), paper_id=c.paper_id, source_file=c.source_file, page=c.page,
                           location=c.label, chunk_id=c.chunk_id, model=getattr(agent.extract_model, "model", ""),
                           extracted_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                link_sequence(rec, agent._lookup(c.paper_id))
                rec = agent.enrich(rec)
                if _rec_key(rec) in existing:
                    continue
                existing.add(_rec_key(rec))
                rec["_agent_added"] = agent.task.task_id
                agent.records.append(rec)
                idx = len(agent.records) - 1
                agent.task.record_ids.append(idx)
                agent._remember_external(idx)
                added.append(idx)
            msg = f"{len(found)} record(s) extracted, {len(added)} new (ids {added}), {len(found) - len(added)} already present."
            cov_cfg = agent.cfg.get("coverage")
            if cov_cfg and c.kind == "table":
                recs = [r for r in agent.records if r.get("chunk_id") == c.chunk_id and not r.get("_rejected")]
                cov = table_coverage(c.text, recs, cov_cfg)
                if cov:
                    msg += f" Table coverage now {cov['coverage']:.0%} ({cov['matched']}/{cov['cells']})."
                    if cov["coverage"] >= cov_cfg.get("min_coverage", 0.8):
                        for i, r in enumerate(agent.records):
                            if r.get("chunk_id") == c.chunk_id:
                                agent._external[i] = {p for p in agent._external.get(i, set()) if not p.startswith("table only")}
                                agent._recheck(i)
                    miss = missing_cells(c.text, recs, cov_cfg)
                    if miss:
                        msg += " Still missing: " + "; ".join(f"{a} | {b} | {v}" for a, b, v in miss[:20])
            return msg

        def update_record(record_ids: list[int], field: str, value: str, quote: str) -> str:
            """Set one field on one or more records of this task. 'quote' must be copied verbatim from the
            paper or its SI and must support the new value; it is checked against the documents.
            Use value 'null' to clear a field the paper does not state."""
            ids = agent._check_ids(record_ids)
            if field not in agent.model_fields:
                raise ValueError(f"unknown field '{field}'. Fields: {', '.join(agent.model_fields)}")
            new = coerce({field: value}, agent.schema).get(field)
            found_in = None
            if quote:
                for c in agent.store.paper_chunks(agent.task.paper_id):
                    if verify_evidence(quote, c.text)[0]:
                        found_in = c
                        break
            if new is not None and found_in is None:
                raise ValueError("the quote was not found in this paper's documents; copy it exactly")
            if field == "solubility_unit" and new is not None and not unit_in_source(new, quote):
                raise ValueError(f"the quote does not contain the unit '{new}'")
            changed = []
            for i in ids:
                r = agent.records[i]
                old = r.get(field)
                if old == new:
                    continue
                r[field] = new
                if field == "solubility_unit":
                    r["unit_in_source"] = None if new is None else True
                if field == agent.evidence_key:
                    src = agent.store.chunk(r.get("chunk_id")) if r.get("chunk_id") else None
                    ok, ratio = verify_evidence(new, src.text if src else "")
                    r.update(evidence_verified=ok, evidence_match=ratio)
                where = f"chunk {found_in.chunk_id}" if found_in else "no quote"
                r.setdefault("_agent_changes", []).append(
                    f"{agent.task.task_id}: {field} {old!r} -> {new!r} ({where}: \"{(quote or '')[:80]}\")")
                agent._recheck(i)
                changed.append(i)
            remaining = {i: agent.records[i].get("review_reason") for i in changed if agent.records[i].get("review_reason")}
            return (f"Updated {len(changed)} record(s) {changed}: {field} = {new!r}."
                    + (f" Remaining problems: {json.dumps(remaining)[:600]}" if remaining else " No problems remain on them."))

        def reject(record_ids: list[int], reason: str) -> str:
            """Mark records of this task as wrong (not a peptide, a duplicate, a misread or invented value).
            They are removed from the database and listed on a separate sheet with this reason."""
            ids = agent._check_ids(record_ids)
            for i in ids:
                agent.records[i]["_rejected"] = f"{agent.task.task_id}: {reason}"
            return f"Rejected {len(ids)} record(s): {ids}."

        def accept(reason: str) -> str:
            """Finish this task. Give one sentence: what was fixed, or why the documents do not contain the information."""
            agent.task.outcome = "accepted"
            agent.task.summary = reason
            return "Task finished."

        return [StructuredTool.from_function(f) for f in
                (search_paper, read_section, read_nearby, find_sequence, re_extract, update_record, reject, accept)]

    # -- bookkeeping ----------------------------------------------------------------------------
    def _lookup(self, pid: str) -> dict:
        if pid not in self._lookups:
            found = {}
            for f in self.store.files(pid):
                for k, v in label_lookup(self.store.file_chunks(f["file_id"]), f["name"]).items():
                    found.setdefault(k, v)
            self._lookups[pid] = found
        return self._lookups[pid]

    def _remember_external(self, i: int) -> None:
        """Problems on a record that enrich() does not produce (table coverage, schema) are kept across re-checks."""
        if i in self._external:
            return
        r = self.records[i]
        before = _reasons(r)
        self._external[i] = before - _reasons(self.enrich(copy.deepcopy(r)))

    def _recheck(self, i: int) -> None:
        r = self.enrich(self.records[i])
        reasons = _reasons(r) | self._external.get(i, set())
        r.update(needs_review=bool(reasons), review_reason="; ".join(sorted(reasons)))
        self.records[i] = r

    def _finalise(self, tasks: list) -> None:
        by_record: dict = {}
        for t in tasks:
            for i in t.record_ids:
                by_record.setdefault(i, []).append(t)
        for i, r in enumerate(self.records):
            ts = by_record.get(i, [])
            changes = r.pop("_agent_changes", [])
            added = r.pop("_agent_added", None)
            if not ts and not added:
                continue
            r["agent_trace"] = " || ".join(f"{t.task_id} [{t.outcome}]: " + " -> ".join(s.split("(")[0] for s in t.trace)
                                           + (f" ({t.summary})" if t.summary else "") for t in ts)
            r["agent_changes"] = "; ".join(changes)
            r["agent_status"] = ("rejected" if r.get("_rejected") else "added by agent" if added
                                 else "changed" if changes else "reviewed, unchanged")
            if r.get("_rejected"):
                r["agent_reject_reason"] = r["_rejected"]

    def _summary(self, tasks: list, kept: list) -> dict:
        return {
            "agent_model": self.llm.model, "agent_tasks": len(tasks),
            "agent_accepted": sum(t.outcome == "accepted" for t in tasks),
            "agent_step_limit": sum(t.outcome == "step limit" for t in tasks),
            "agent_invalid_tool_calls": self.invalid_calls,
            "agent_records_changed": sum(r.get("agent_status") == "changed" for r in kept),
            "agent_records_added": sum(r.get("agent_status") == "added by agent" for r in kept),
            "agent_records_rejected": len(self.rejected),
            "records_after_agent": len(kept), "flagged_after_agent": sum(bool(r.get("needs_review")) for r in kept),
        }


# A sequence written in text: three-letter code (Ala-Gly-...) or 5+ one-letter residues, optionally with
# bracketed non-canonical residues ([Cha]) and caps (Ac-, -NH2)
SEQ_RE = re.compile(r"(?:Ac-|H-|H2N-)?(?:(?:[A-Z][a-z]{2}-){2,}[A-Z][a-z]{2}|(?:[ACDEFGHIKLMNPQRSTVWY]|\[[A-Za-z0-9]+\]){5,})(?:-NH2|-OH)?")


def _reasons(rec: dict) -> set:
    return set(p for p in re.split(r";\s*", str(rec.get("review_reason") or "")) if p)


def _rec_key(r: dict) -> tuple:
    seq = r.get("sequence_one_letter") or r.get("sequence")
    return (label_key(r.get("peptide_name")) or "", str(seq or ""), str(r.get("solubility_value")),
            str(r.get("solubility_qualitative") or ""), str(r.get("solvent") or "").lower(), str(r.get("temperature_c")))


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
