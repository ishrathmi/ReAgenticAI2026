
import argparse
from dotenv import load_dotenv

# Load API key and model settings from .env
load_dotenv()

from agent import run_research


def main():
    parser = argparse.ArgumentParser(
        description="CO2 Materials Literature Extraction Agent"
    )

    parser.add_argument("--query", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--constraints", default="")
    parser.add_argument("--max-papers", type=int, default=10)
    parser.add_argument("--db", default="co2_materials.db")
    parser.add_argument(
        "--csv",
        default="extracted_measurements_openrouter.csv"
    )
    parser.add_argument("--reprocess", action="store_true")

    args = parser.parse_args()

    print("Starting CO2 Materials Literature Agent...")
    print(f"Query: {args.query}")
    print(f"Target: {args.target}")
    print(f"Maximum papers: {args.max_papers}")
    print("Language model: OpenRouter")

    result = run_research(
        query=args.query,
        target=args.target,
        constraints=args.constraints,
        max_papers=args.max_papers,
        db_path=args.db,
        csv_path=args.csv,
        reprocess=args.reprocess,
    )

    print("\nResearch run finished.")
    print(f"Papers processed: {result.get('processed_count', 0)}")
    print(f"Measurements extracted: {result.get('measurement_count', 0)}")
    print(f"CSV output: {args.csv}")
    print(f"Database: {args.db}")

    errors = result.get("errors", [])
    if errors:
        print("\nWarnings/errors:")
        for error in errors:
            print(f"- {error}")


if __name__ == "__main__":
    main()
