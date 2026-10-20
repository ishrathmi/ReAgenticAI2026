
"""Section 2 — Raw tool calling for EELS literature analysis.

Question: What preprocessing methods are commonly reported
for white-line EELS?

Run:
    uv run demos/d02_eels_preprocessing.py
"""

import asyncio
import csv
import json
from collections import defaultdict
from pathlib import Path

from sci_mcp.client.llm import LLM


DATA_PATH = Path("eels_literature.csv")

MISSING = {"", "not reported", "n/a", "nan", "none"}

ENERGY_FIELDS = [
    "M4_L2_onset_eV",
    "M5_L3_onset_eV",
    "M4_L2_maximum_eV",
    "M5_L3_maximum_eV",
    "chemical_shift_eV",
]


# 1. THE SCIENTIFIC TOOL

def summarize_white_line_preprocessing(
    element: str = "all",
    processing_type: str = "background_method",
) -> dict:
    """Summarize reported EELS preprocessing and energy values."""

    if processing_type not in [
        "background_method",
        "deconvolution_method",
    ]:
        return {"error": "Invalid processing type."}

    if not DATA_PATH.exists():
        return {"error": f"Dataset not found: {DATA_PATH}"}

    with DATA_PATH.open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:
        records = list(csv.DictReader(file))

    required = [
        "paper_id", "ln_species", processing_type,
        *ENERGY_FIELDS,
    ]

    if not records:
        return {"error": "The dataset is empty."}

    missing_columns = [
        col for col in required if col not in records[0]
    ]

    if missing_columns:
        return {"error": f"Missing columns: {missing_columns}"}

    # Filter by element, retaining the original ln_species field.
    if element.lower() != "all":
        records = [
            row for row in records
            if row["ln_species"].strip().lower()
            == element.lower()
        ]

    if not records:
        return {"error": "No matching records."}

    # Group by method and count distinct papers.
    grouped = defaultdict(list)
    papers = set()

    for row in records:
        paper_id = row["paper_id"].strip()
        method = (row[processing_type] or "").strip()

        if method.lower() in MISSING:
            method = "Not reported"

        papers.add(paper_id)
        grouped[method].append(row)

    results = []

    for method, rows in grouped.items():
        method_papers = {
            row["paper_id"].strip() for row in rows
        }

        measurements = {}

        for field in ENERGY_FIELDS:
            values = [
                row[field].strip()
                for row in rows
                if (row[field] or "").strip().lower()
                not in MISSING
            ]

            measurements[field] = {
                "records_reporting_value": len(values),
                "reported_values": values,
            }

        results.append({
            "method": method,
            "paper_count": len(method_papers),
            "percentage_of_papers": round(
                100 * len(method_papers) / len(papers), 1
            ),
            "paper_ids": sorted(method_papers),
            "measurements": measurements,
        })

    results.sort(
        key=lambda item: item["paper_count"],
        reverse=True,
    )

    return {
        "element": element,
        "processing_type": processing_type,
        "total_papers": len(papers),
        "results": results,
        "limitations": (
            "Methods are grouped by their exact reported "
            "descriptions. Equivalent terminology may be "
            "counted separately. Multiple methods can occur "
            "in one paper. Reported energy values are not "
            "necessarily directly comparable."
        ),
    }


# 2. DESCRIBE THE TOOL TO THE MODEL

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "summarize_white_line_preprocessing",
            "description": (
                "Analyze preprocessing methods reported in "
                "EELS literature. Count distinct papers using "
                "each background subtraction or deconvolution "
                "method and identify associated white-line "
                "onset energies, maxima, and chemical shifts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element": {
                        "type": "string",
                        "description": (
                            "Element symbol such as Mn, Fe, Ce, "
                            "or 'all' for the entire dataset."
                        ),
                    },
                    "processing_type": {
                        "type": "string",
                        "enum": [
                            "background_method",
                            "deconvolution_method",
                        ],
                    },
                },
                "required": ["processing_type"],
            },
        },
    }
]


# 3. RAW TOOL-CALLING CONVERSATION

async def main() -> None:

    llm = LLM()

    messages = [{
        "role": "user",
        "content": (
            "What background subtraction methods are most "
            "commonly reported for white-line EELS in my "
            "literature dataset? Which white-line energy "
            "measurements are reported alongside them? "
            "Distinguish missing information from evidence "
            "that a method was not performed."
        ),
    }]

    # First model response: request a tool.
    msg = await llm.chat(messages, TOOLS)

    print("\n1) MODEL REPLY:")
    print(json.dumps(
        msg.model_dump(exclude_none=True),
        indent=2,
    ))

    messages.append(msg.model_dump(exclude_none=True))

    # Python executes the requested tool.
    for call in msg.tool_calls or []:

        args = json.loads(call.function.arguments)

        if call.function.name == "summarize_white_line_preprocessing":
            result = summarize_white_line_preprocessing(**args)
        else:
            result = {"error": "Unknown tool."}

        print("\n2) TOOL RESULT:")
        print(json.dumps(result, indent=2))

        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result),
        })

    # Second model response: interpret the results.
    final = await llm.chat(messages, TOOLS)

    print("\n3) FINAL ANSWER:")
    print(final.content)


if __name__ == "__main__":
    asyncio.run(main())
