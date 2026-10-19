# ChemScraper, Team ReAgenticAI2026


Members: Xu (Charlie) Chen, Filip Anies, Anthony Pembere, Ellis Kennedy, Ishrath Mohamed Irshadeen
Repo: https://github.com/ishrathmi/ReAgenticAI2026


## 1. Task
Multiple agents on retrieving relevant literatures, extracting data, and build dynamically updating datasets from published data
sources. Target data and constraints are defined by user, values and data sources are returned by agent.
Example: target: "Peptoid aqueous solubility", constraints: "Method must be defined as experimental or
computational" -> Table: peptoid_structure, solubility, method, article_DOI


## 2. Why an agent?
* Define target data and constraints (user defined)
* Organise data sources and data quality evaluation
* Making decisions for including or dropping data
* Saving a record of both good and bad sources
* Optional: Finding new data sources


## 3. Tools and data (3-6 tools)
- database_logger: Check log if paper has already been read. If not, invoke data_extractor and write data/conclusion. If structure has already been reported, decide whether to skip, replace, or append. In: Source ID, log. Out: Updated log. Data source: n/a. Access checked: n/a.

- peptoid_structure_extractor: Identify text or image naming or describing the labelled peptoid, extract the structure of the peptoid. In: Paper. Out: peptoid structure (SMILES). Data source: n/a. Access checked: n/a.

- data_extractor: Extract required data from the paper and validate the results to avoid hallucination.
In: Paper, target, constraints. Out: structured data. Source: Paper. Access checked: Yes.

- source_searcher: Extract sources from a paper which have a high likelihood of containing additional relevant data. In: Paper. Out: List of sources. Data source: n/a. Access checked: n/a.


## 4. Baseline (Milestone III, Tue 5 PM)
A single LLM call with no tools to extract data from a large number of papers. (Expected to see unstructured data with hallucination and limited on the context window.)


## 5. Evaluation (Milestone IV, Wed 11 AM)
- Test cases: <10 paper set, with a pass/fail check on the extracted properties/ Structures/ Sequences>
- Metric: <the number of correctly extracted data>
- Runs per case: <Run the experiment five times and summarize the variability in the results.>
- Comparison: <baseline vs agent: Output Structure, Reliability, and Overall Processing Capacity; Opus 5.5 vs. Sonnet 5.5.>


## 6. Demo (Milestone V, Wed 1 PM)
<A quick literature retrieval demo on any topic, focusing on preprints published within the last 7 days. Use a small collection of approximately 10 papers to demonstrate data extraction, including chemical structures, solubility, and other relevant properties.>


## 7. Risks, limits, roles
- Biggest risk: Difficult to extract chemical structures.
- Fallback if you are behind on Tuesday at noon: Smaller version of initial plan with fewer papers included and simple “applicable” / “not applicable” classification instead of pulling out specific characteristics/values.
- Limits: <max steps, max tokens, expected spend from your keys>
- Roles:  
All: Collect 10-20 papers from own fields + label data  
Charlie:  
Filip:  
Anthony:  
Ellis:  
Ishrath:  	

