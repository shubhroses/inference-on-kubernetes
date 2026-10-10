"""Prints what the tests measured as Markdown: `python -m e2e.summary`.

CI appends it to the job summary. The tests leave one file each in reports/;
see save_report in e2e/load.py. A test that ran a load to its end has a row in
the first table. A test that followed something over time, the replica count
or an alert, has a table of its own below it. Every test's notes come last.
"""

import json
from pathlib import Path

REPORTS = Path(__file__).resolve().parent.parent / "reports"
PERCENTILES = ("p50", "p95", "p99", "max")


def milliseconds(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds * 1000:.0f} ms"


def print_table(rows: list[list[object]]) -> None:
    """Print a Markdown table. The first of the rows names the columns."""
    names, *others = rows
    print("| " + " | ".join(map(str, names)) + " |")
    print("|" + "---|" * len(names))
    for row in others:
        print("| " + " | ".join(map(str, row)) + " |")


def main() -> None:
    reports = [json.loads(path.read_text()) for path in sorted(REPORTS.glob("*.json"))]
    if not reports:
        print("No test got far enough to report.")
        return
    loads: list[list[object]] = [["Test", "Requests", "Failed", *PERCENTILES]]
    for report in reports:
        if load := report["load"]:
            latency = [milliseconds(load["latency_seconds"][key]) for key in PERCENTILES]
            loads.append([report["test"], load["requests"], load["failures"], *latency])
    if len(loads) > 1:
        print_table(loads)
    for report in reports:
        if report["timeline"]:
            print(f"\n**{report['test']} timeline**\n")
            print_table(report["timeline"])
    print()
    for report in reports:
        for note in report["notes"]:
            print(f"- {report['test']}: {note}")
        if load := report["load"]:
            for error, count in load["errors"].items():
                print(f"- {report['test']}: {count} requests failed on a {error}")


if __name__ == "__main__":
    main()
