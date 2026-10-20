"""Run the literature agent from the repository root without installing the package.

  python run_agent.py --profile peptide_solubility --dry-run
  python run_agent.py --profile peptide_solubility
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from litagent.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
