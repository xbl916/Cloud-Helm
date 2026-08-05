import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "miniprogram"
    problems: list[str] = []
    for path in root.rglob("*.json"):
        try:
            json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            problems.append(f"invalid JSON {path.relative_to(root)}: {exc}")
    for path in root.rglob("*.wxml"):
        try:
            source = path.read_text()
            if "&amp;&amp;" in source:
                problems.append(
                    f"unsupported escaped logical expression {path.relative_to(root)}: "
                    "precompute or nest wx:if conditions instead"
                )
            source = source.replace("wx:", "wx_")
            ET.fromstring(f"<root>{source}</root>")
        except (OSError, ET.ParseError) as exc:
            problems.append(f"invalid WXML structure {path.relative_to(root)}: {exc}")
    for path in root.rglob("*.wxss"):
        try:
            source = path.read_text()
        except OSError as exc:
            problems.append(f"cannot read WXSS {path.relative_to(root)}: {exc}")
            continue
        if source.count("{") != source.count("}"):
            problems.append(f"unbalanced WXSS braces {path.relative_to(root)}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("Mini Program JSON/WXML/WXSS checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
