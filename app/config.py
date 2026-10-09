"""Settings, read once from environment variables when the process starts."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass


def _flag(value: str) -> bool:
    """Parse a switch strictly, so that a typo cannot turn it on or off by accident."""
    choices = {"true": True, "1": True, "false": False, "0": False}
    if value.lower() not in choices:
        raise ValueError(value)
    return choices[value.lower()]


# Environment variable -> how to parse it. The settings field is the same name in lower case.
_PARSERS: dict[str, Callable[[str], object]] = {
    "MODEL_SLOTS": int,
    "QUEUE_SIZE": int,
    "MODEL_INFER_SECONDS": float,
    "MODEL_LOAD_SECONDS": float,
    "FAULT_INJECTION": _flag,
    "GIT_SHA": str,
    "HOST": str,
    "PORT": int,
}


@dataclass(frozen=True)
class Settings:
    model_slots: int = 8  # requests the model serves at the same time
    queue_size: int = 16  # requests that may wait for a slot; any more get a 429
    model_infer_seconds: float = 0.1  # how long the stand-in model holds a slot
    model_load_seconds: float = 2.0  # how long the simulated model load takes
    fault_injection: bool = False  # test only: honour the fail and delay parameters
    git_sha: str = "unknown"  # reported by the build-info metric
    # All interfaces, because the kubelet and other pods reach a container
    # through its pod IP and never through its loopback interface.
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000

    def __post_init__(self) -> None:
        if self.model_slots < 1:
            raise ValueError("MODEL_SLOTS must be at least 1")
        if self.queue_size < 0:
            raise ValueError("QUEUE_SIZE must not be negative")
        if self.model_infer_seconds < 0 or self.model_load_seconds < 0:
            raise ValueError("MODEL_INFER_SECONDS and MODEL_LOAD_SECONDS must not be negative")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Read the variables that are set; the others keep their defaults."""
        env = os.environ if env is None else env
        values = {}
        for variable, parse in _PARSERS.items():
            if variable in env:
                try:
                    values[variable.lower()] = parse(env[variable])
                except ValueError:
                    raise ValueError(f"{variable}={env[variable]!r} is not valid") from None
        return cls(**values)
