"""Prints what the load tests measured as Markdown: `python -m e2e.summary`.

CI appends it to the job summary. The tests leave one file per load run in
reports/; see save_report in e2e/load.py.
"""

import json
from pathlib import Path

REPORTS = Path(__file__).resolve().parent.parent / "reports"
PERCENTILES = ("p50", "p95", "p99", "max")


def milliseconds(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds * 1000:.0f} ms"


def main() -> None:
    reports = [json.loads(path.read_text()) for path in sorted(REPORTS.glob("*.json"))]
    if not reports:
        print("No load test got far enough to report.")
        return
    print("| Test | Requests | Failed | " + " | ".join(PERCENTILES) + " |")
    print("|---|---|---|---|---|---|---|")
    for report in reports:
        load = report["load"]
        latency = [milliseconds(load["latency_seconds"][key]) for key in PERCENTILES]
        cells = [report["test"], load["requests"], load["failures"], *latency]
        print("| " + " | ".join(map(str, cells)) + " |")
    print()
    for report in reports:
        for note in report["notes"]:
            print(f"- {report['test']}: {note}")
        for error, count in report["load"]["errors"].items():
            print(f"- {report['test']}: {count} requests failed on a {error}")


if __name__ == "__main__":
    main()
