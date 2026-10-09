"""What the unit tests read from the chart: its values, its alert rules and its dashboard."""

import json
import re
from pathlib import Path

CHART = Path(__file__).parent.parent / "chart"
VALUES = (CHART / "values.yaml").read_text()
RULES = (CHART / "templates" / "prometheusrule.yaml").read_text()
DASHBOARD = json.loads((CHART / "dashboards" / "inference.json").read_text())
PANELS = {panel["title"]: panel for panel in DASHBOARD["panels"]}


def numbers(key: str) -> list[float]:
    """The numbers values.yaml sets under this key, wherever in the file the key appears."""
    return [float(number) for number in re.findall(rf"^ *{key}: (\S+)$", VALUES, re.MULTILINE)]


def dashboard_queries() -> str:
    """Every query the dashboard sends to Prometheus: its panels' and its variables'."""
    queries = [target["expr"] for panel in DASHBOARD["panels"] for target in panel["targets"]]
    queries += [variable["query"] for variable in DASHBOARD["templating"]["list"]]
    return " ".join(queries)


def scraped_series(text: str) -> set[str]:
    """The names in the text of series that Prometheus would have to scrape from the service."""
    return {name for name in service_series(text) if ":" not in name}


def recorded_series(text: str) -> set[str]:
    """The names in the text of series that a recording rule makes: those are named with colons."""
    return {name for name in service_series(text) if ":" in name}


def service_series(text: str) -> set[str]:
    # A series name is made of letters, digits, underscores and colons. The
    # service's all have `inference_` in them.
    return {name for name in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", text) if "inference_" in name}
