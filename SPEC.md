```markdown
# <Project name>, Team <n>


Members: <names>
Repo: <URL>


## 1. Task
<2-3 sentences: the science question, who would use this tool,
what goes in, what comes out.>
Example: <one concrete input> -> <the output you expect>


## 2. Why an agent?
<Which decisions does the model make while it runs? If you already
know every step, write "workflow". That is a fine answer.>


## 3. Tools and data (3-6 tools)
- <tool name>: <what it does>. In: <...>. Out: <...>.
  Data source: <...>. Access checked: <yes/no>


## 4. Baseline (Milestone III, Tue 5 PM)
<The simplest version that runs end to end, e.g. one LLM call with
no tools, or a fixed script. Your agent will be compared to it.>


## 5. Evaluation (Milestone IV, Wed 11 AM)
- Test cases: <5-20, each with a known answer or a pass/fail check>
- Metric: <the number you will report>
- Runs per case: <model outputs vary, so more than one if you can>
- Comparison: <baseline vs. agent, model A vs. model B, ...>
- RL component (optional): <observation, actions, reward,
  baseline policy>


## 6. Demo (Milestone V, Wed 1 PM)
<The one example you will run live, and what the audience
should see.>


## 7. Risks, limits, roles
- Biggest risk: <...>
- Fallback if you are behind on Tuesday at noon: <smaller version>
- Limits: <max steps, max tokens, expected spend from your keys>
- Roles: <who owns which part>
```