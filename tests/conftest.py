import pytest

from tests.helpers import Gate


@pytest.fixture
def gate() -> Gate:
    """The gate the model under test sleeps on. Closed until the test opens it."""
    return Gate()
