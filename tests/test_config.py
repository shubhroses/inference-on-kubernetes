"""Settings: defaults, environment variables and validation."""

import pytest

from app.config import Settings


def test_defaults_apply_when_nothing_is_set():
    settings = Settings.from_env({})

    assert settings == Settings()
    assert (settings.model_slots, settings.queue_size) == (8, 16)
    assert settings.keep_alive_seconds == 5
    assert settings.fault_injection is False
    assert settings.git_sha == "unknown"


def test_every_setting_can_come_from_the_environment():
    settings = Settings.from_env(
        {
            "MODEL_SLOTS": "4",
            "QUEUE_SIZE": "0",
            "MODEL_INFER_SECONDS": "0.25",
            "MODEL_LOAD_SECONDS": "5",
            "KEEP_ALIVE_SECONDS": "15",
            "FAULT_INJECTION": "true",
            "GIT_SHA": "0123abc",
            "HOST": "127.0.0.1",
            "PORT": "9000",
        }
    )

    assert settings == Settings(
        model_slots=4,
        queue_size=0,
        model_infer_seconds=0.25,
        model_load_seconds=5.0,
        keep_alive_seconds=15,
        fault_injection=True,
        git_sha="0123abc",
        host="127.0.0.1",
        port=9000,
    )


def test_the_process_environment_is_read_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL_SLOTS", "3")

    assert Settings.from_env().model_slots == 3


@pytest.mark.parametrize(
    ("value", "enabled"),
    [("true", True), ("TRUE", True), ("1", True), ("false", False), ("0", False)],
)
def test_fault_injection_is_a_strict_switch(value: str, enabled: bool):
    assert Settings.from_env({"FAULT_INJECTION": value}).fault_injection is enabled


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("FAULT_INJECTION", "yes"),
        ("FAULT_INJECTION", ""),
        ("MODEL_SLOTS", "eight"),
        ("MODEL_INFER_SECONDS", "fast"),
        ("KEEP_ALIVE_SECONDS", "2.5"),
        ("PORT", ""),
    ],
)
def test_a_value_that_does_not_parse_is_reported_with_its_variable(variable: str, value: str):
    with pytest.raises(ValueError, match=f"{variable}='{value}' is not valid"):
        Settings.from_env({variable: value})


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MODEL_SLOTS", "0"),
        ("QUEUE_SIZE", "-1"),
        ("MODEL_INFER_SECONDS", "-0.1"),
        ("MODEL_LOAD_SECONDS", "-1"),
        ("KEEP_ALIVE_SECONDS", "0"),
    ],
)
def test_values_that_make_no_sense_are_refused(variable: str, value: str):
    with pytest.raises(ValueError, match=variable):
        Settings.from_env({variable: value})
