import pytest

from libs.attio.values import normalize_company_name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme, Inc.", "acme"),
        ("Acme Inc", "acme"),
        ("Acme, LLC", "acme"),
        ("Acme GmbH", "acme"),
        ("Acme Ltd", "acme"),
        ("ACME Corp.", "acme corp"),
        ("Cybrid Technology Inc.", "cybrid technology"),
        ("CORTEX TECH LIMITED", "cortex tech"),
        ("Snowflake", "snowflake"),
        ("  Whitespace Co.  ", "whitespace co"),
    ],
)
def test_normalize_company_name(raw: str, expected: str) -> None:
    assert normalize_company_name(raw) == expected
