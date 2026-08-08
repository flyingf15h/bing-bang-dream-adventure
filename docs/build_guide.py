"""Regenerate docs/CALIBRATION.md from the in-app guide text.

The dashboard shows the guide as HTML; this keeps the repository copy in
step so the two can never disagree.  Run:  python docs/build_guide.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from bbda.guide import GUIDE_HTML


def to_markdown(html: str) -> str:
    text = re.sub(r"<h3>(.*?)</h3>", r"\n## \1\n", html)
    text = text.replace("<p>", "\n").replace("</p>", "\n")
    text = text.replace("<ol>", "\n").replace("</ol>", "\n")
    text = text.replace("<li>", "1. ").replace("</li>", "")
    text = text.replace("<b>", "**").replace("</b>", "**")
    text = text.replace("<i>", "*").replace("</i>", "*")
    for entity, plain in (("&mdash;", "--"), ("&plusmn;", "+/-"), ("&nbsp;", " "),
                          ("&ndash;", "-"), ("&deg;", " deg")):
        text = text.replace(entity, plain)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "CALIBRATION.md"
    out.write_text(
        "# Calibration and axis alignment\n\n"
        "> Generated from `dashboard/bbda/guide.py`, which is also what the\n"
        "> dashboard shows behind *Calibrate -> Show the step-by-step guide*.\n"
        "> Edit that file, then re-run `python docs/build_guide.py`.\n\n"
        + to_markdown(GUIDE_HTML) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out}")
