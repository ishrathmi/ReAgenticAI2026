```markdown
# ChemScraper, Team ReAgenticAI2026


Members: Xu Chen, Filip Anies, Anthony Pembere, Ellis Kennedy, Ishrath Mohamed Irshadeen
Repo: https://github.com/ishrathmi/ReAgenticAI2026


## 1. Task
Extracting data from published articles. For researchers looking to build datasets from published data from multiple
sources. Target data and constraints are defined by user, values and data sources are returned by agent.
Example: target: "Peptoid aqueous solubility", constraints: "Method must be defined as experimental or
computational" -> Table: peptoid_structure, solubility, method, article_DOI


## 2. Why an agent?
* Identify data (user defined)
* Apply constraints (user defined)
* Organise data sources
* Saving a record of both good and bad sources
* Optional: Finding new data sources
<Which decisions does the model make while it runs? If you already
know every step, write "workflow". That is a fine answer.>


## 3. Tools and data (3-6 tools)
- <tool name>: <what it does>. In: <...>. Out: <...>.
  Data source: <...>. Access checked: <yes/no>
- database_logger: Checks log if paper has not yet been read. If not, reads paper and writes data/conclusion. In: Paper.
  Out: Log. 

## 4. Baseline (Milestone III, Tue 5 PM)
A single LLM call with no tools to extract data from a large number of papers.


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