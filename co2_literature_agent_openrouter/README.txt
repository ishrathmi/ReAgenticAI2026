AI-powered research agent designed to search scientific literature, extract information about CO₂ adsorption materials, save the extracted measurements in a database, and export the results to a CSV file that you can open in Excel.
It combines four main stages:
•	OpenAlex and arXiv: search for scientific papers.
•	run a local language model to extract information .
•	Pydantic: validate the structure of extracted data.
•	LangGraph, SQLite and pandas: coordinate the workflow, store the results, and export them.

Overall workflow
1. Search scientific literature
OpenAlex and arXiv
2. Record papers in SQLite
Track paper metadata and processing status
3. Retrieve paper text
Try the PDF first, then fall back to the title and abstract
4. Extract scientific measurements
5. Validate and save results
Pydantic validation, evidence quotes and SQLite storage
6. Export to CSV
Open the results in Excel for further analysis
For example, the agent might find a paper about a material and attempt to extract its CO₂ adsorption capacity, temperature, pressure, units, material composition, method and supporting quotation.
Important: the agent can only extract information from the text it successfully retrieves. If it obtains only the abstract, it may miss important details from tables, figures, experimental methods and supplementary information.
