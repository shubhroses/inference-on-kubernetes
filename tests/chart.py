"""What the unit tests read from the chart: its values and its alert rules."""

import re
from pathlib import Path

CHART = Path(__file__).parent.parent / "chart"
VALUES = (CHART / "values.yaml").read_text()
RULES = (CHART / "templates" / "prometheusrule.yaml").read_text()


def numbers(key: str) -> list[float]:
    """The numbers values.yaml sets under this key, wherever in the file the key appears."""
    return [float(number) for number in re.findall(rf"^ *{key}: (\S+)$", VALUES, re.MULTILINE)]


def scraped_series(text: str) -> set[str]:
    """The names in the text of series that Prometheus would have to scrape from the service."""
    return {name for name in service_series(text) if ":" not in name}


def service_series(text: str) -> set[str]:
    # A series name is made of letters, digits, underscores and colons. The
    # service's all have `inference_` in them.
    return {name for name in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", text) if "inference_" in name}
