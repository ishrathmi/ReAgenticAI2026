from pathlib import Path

RULES_MD = Path(__file__).resolve().parents[2] / "RULES.md"


def rules_section(heading, until):
    """Text of RULES.md from '## heading' up to '## until', so prompts follow the rules file."""
    text = RULES_MD.read_text(encoding="utf-8")
    start = text.index(f"## {heading}")
    end = text.index(f"## {until}", start)
    return text[start:end].strip()
