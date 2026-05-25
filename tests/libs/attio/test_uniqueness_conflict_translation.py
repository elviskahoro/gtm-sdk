"""Verifies that SDK uniqueness-conflict errors get translated to
AttioConflictError with no `__cause__` chain.

The Attio SDK raises ResponseValidationError(__cause__=pydantic.ValidationError)
for uniqueness conflicts because its generated Code Literal doesn't list
"uniqueness_conflict". We re-raise as AttioConflictError(...) `from None` so
Modal logs only the clean conflict — not the noisy pydantic traceback.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from libs.attio.companies import add_company
from libs.attio.errors import AttioConflictError
from libs.attio.models import CompanyInput
from libs.attio.people import add_person
from libs.attio.models import PersonInput


class _FakeResponseValidationError(Exception):
    """Mimics attio.errors.ResponseValidationError shape (has .body)."""

    def __init__(self, body: str) -> None:
        super().__init__("Response validation failed: pydantic noise here")
        self.body = body


_UNIQUENESS_BODY = (
    '{"status_code": 400, "type": "invalid_request_error",'
    ' "code": "uniqueness_conflict", "message": "duplicate",'
    ' "data": {"existing_record": {"id": {"record_id": "rec_existing"}}}}'
)


@contextmanager
def _mock_client_raising(exc: Exception):
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.records.post_v2_objects_object_records.side_effect = exc
    with patch("libs.attio.companies.get_client", return_value=client):
        with patch("libs.attio.people.get_client", return_value=client):
            yield


def test_add_company_translates_uniqueness_conflict_without_cause_chain() -> None:
    pydantic_like = ValueError("pydantic.ValidationError noise")
    sdk_err = _FakeResponseValidationError(_UNIQUENESS_BODY)
    sdk_err.__cause__ = pydantic_like

    with _mock_client_raising(sdk_err):
        with pytest.raises(AttioConflictError) as excinfo:
            add_company(CompanyInput(name="Example", domain="example.com"))

    assert excinfo.value.__cause__ is None
    assert excinfo.value.existing_record_id == "rec_existing"
    assert "Company already exists" in str(excinfo.value)


def test_add_person_translates_uniqueness_conflict_without_cause_chain() -> None:
    pydantic_like = ValueError("pydantic.ValidationError noise")
    sdk_err = _FakeResponseValidationError(_UNIQUENESS_BODY)
    sdk_err.__cause__ = pydantic_like

    with _mock_client_raising(sdk_err):
        with pytest.raises(AttioConflictError) as excinfo:
            add_person(PersonInput(email="dup@example.com"))

    assert excinfo.value.__cause__ is None
    assert "Person already exists" in str(excinfo.value)
