"""Unit tests for ``scripts/caldotcom-bookings-backfill.py``.

The script filename is hyphenated, so it's loaded via ``importlib`` rather than
imported. Tests cover the envelope mapper, the live-gate mirror, and the
``--dry-run`` path (which must not touch the network).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from libs.caldotcom.models import BookingCreatedPayload, Webhook

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "caldotcom-bookings-backfill.py"
SAMPLE = REPO_ROOT / "api" / "samples" / "caldotcom.booking.created.redacted.json"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("caldotcom_backfill", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample_booking() -> BookingCreatedPayload:
    payload = json.loads(SAMPLE.read_text())["payload"]
    return BookingCreatedPayload.model_validate(
        {"triggerEvent": "BOOKING_CREATED", **payload},
    )


def test_envelope_round_trips_through_webhook_model() -> None:
    module = _load_module()
    booking = _sample_booking()

    envelope = module._envelope_for(booking)  # noqa: SLF001

    assert envelope["triggerEvent"] == "BOOKING_CREATED"
    assert envelope["createdAt"]  # populated from booking createdAt or start
    assert envelope["payload"]["uid"] == booking.uid
    # The live handler must accept what we POST.
    parsed = Webhook.model_validate(envelope)
    assert isinstance(parsed.payload, BookingCreatedPayload)
    assert parsed.payload.uid == booking.uid


def test_live_gate_rejects_missing_attendees_and_host() -> None:
    module = _load_module()
    good = _sample_booking()
    assert module._passes_live_gate(good)  # noqa: SLF001

    no_attendees = good.model_copy(update={"attendees": []})
    assert not module._passes_live_gate(no_attendees)  # noqa: SLF001

    no_host = good.model_copy(
        update={"hosts": [], "organizer": None, "user": None, "userPrimaryEmail": None},
    )
    assert not module._passes_live_gate(no_host)  # noqa: SLF001


def test_body_failure_detects_success_flag() -> None:
    module = _load_module()

    # HTTP 200 but the handler reports an op failure → treated as a failure.
    failed = httpx.Response(
        200,
        json={
            "success": False,
            "outcomes": [
                {"op_index": 0, "op_type": "UpsertCompany", "success": True},
                {
                    "op_index": 2,
                    "op_type": "UpsertMeeting",
                    "success": False,
                    "errors": [{"message": "Status 404 Not found"}],
                },
            ],
        },
    )
    err = module._body_failure(failed)  # noqa: SLF001
    assert err is not None
    assert "UpsertMeeting" in err

    ok = httpx.Response(200, json={"success": True, "outcomes": []})
    assert module._body_failure(ok) is None  # noqa: SLF001


def test_body_failure_unwraps_double_encoded_json_string() -> None:
    module = _load_module()
    # Body is a JSON-encoded string (double-encoded) reporting success.
    resp = httpx.Response(200, json=json.dumps({"success": True}))
    assert module._body_failure(resp) is None  # noqa: SLF001


def test_body_failure_treats_plain_rejection_string_as_failure() -> None:
    module = _load_module()
    # The handler returns a bare reason string when it rejects an invalid
    # webhook — must NOT be read as a silent success.
    resp = httpx.Response(200, json="BOOKING_CREATED missing uid/attendees")
    err = module._body_failure(resp)  # noqa: SLF001
    assert err is not None
    assert "rejected" in err


def test_post_retries_connectivity_error_in_200_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", _no_sleep)
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "outcomes": [
                        {
                            "op_type": "UpsertMeeting",
                            "success": False,
                            "errors": [
                                {"code": "connectivity_error", "message": "down"},
                            ],
                        },
                    ],
                },
            )
        return httpx.Response(200, json={"success": True})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    code, err = module._post_with_retry(client, "https://x/hook", {"a": 1})  # noqa: SLF001
    assert (code, err) == (200, None)
    assert calls["n"] == 2  # transient connectivity_error retried, then succeeded


def test_post_does_not_retry_deterministic_body_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", _no_sleep)
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "success": False,
                "outcomes": [
                    {
                        "op_type": "UpsertMeeting",
                        "success": False,
                        "errors": [{"code": "not_found", "message": "Status 404"}],
                    },
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    code, err = module._post_with_retry(client, "https://x/hook", {"a": 1})  # noqa: SLF001
    assert code == 200
    assert err is not None and "UpsertMeeting" in err
    assert calls["n"] == 1  # deterministic 404 → no retry, straight to dead-letter


def _no_sleep(_seconds: float) -> None:
    """Typed stand-in for time.sleep so retry tests don't actually wait."""


def test_post_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", _no_sleep)  # no real backoff
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"e": "rate"})
        return httpx.Response(200, json={"success": True})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    code, err = module._post_with_retry(client, "https://x/hook", {"a": 1})  # noqa: SLF001
    assert (code, err) == (200, None)
    assert calls["n"] == 2  # retried once after the 429


def test_post_does_not_retry_other_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", _no_sleep)
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(422, json={"e": "bad"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    code, err = module._post_with_retry(client, "https://x/hook", {"a": 1})  # noqa: SLF001
    assert code == 422
    assert err is not None
    assert calls["n"] == 1  # deterministic 4xx → no retry


def test_unknown_filter_values_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd --status / --lifecycle must error, not silently narrow to zero."""
    module = _load_module()

    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run", "--status", "acepted"])
    with pytest.raises(SystemExit):
        module.main()

    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run", "--lifecycle", "passt"])
    with pytest.raises(SystemExit):
        module.main()


class _FakeClient:
    """Stand-in for CalcomClient: returns canned bookings, no network."""

    bookings: list[BookingCreatedPayload] = []

    @classmethod
    def from_env(cls) -> _FakeClient:
        return cls()

    def list_bookings(self, **_kwargs: Any) -> list[BookingCreatedPayload]:
        return list(type(self).bookings)

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_dry_run_writes_envelopes_and_skips_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()

    accepted = _sample_booking()
    cancelled = accepted.model_copy(
        update={"uid": "cancelled-1", "status": "cancelled"},
    )
    no_attendees = accepted.model_copy(
        update={"uid": "gateless-1", "attendees": []},
    )
    _FakeClient.bookings = [accepted, cancelled, no_attendees]

    monkeypatch.setattr(module, "CalcomClient", _FakeClient)
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "out")

    # If the send path were reached, this would raise and fail the test.
    def _no_url(_name: str) -> str:
        pytest.fail("dry-run must not resolve the webhook URL")

    monkeypatch.setattr(module, "modal_url_for_app", _no_url)
    # Explicit --status accepted: narrows to confirmed meetings only.
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run", "--status", "accepted"])

    rc = module.main()
    assert rc == 0

    lines = (tmp_path / "out" / "bookings.jsonl").read_text().strip().splitlines()
    envelopes = [json.loads(line) for line in lines]
    uids = {e["payload"]["uid"] for e in envelopes}
    # Only the accepted booking survives: cancelled fails the RSVP filter,
    # gateless fails the live gate.
    assert uids == {accepted.uid}


def test_default_status_all_keeps_cancelled_but_gate_still_drops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()

    accepted = _sample_booking()
    cancelled = accepted.model_copy(update={"uid": "c-1", "status": "cancelled"})
    no_attendees = accepted.model_copy(update={"uid": "g-1", "attendees": []})
    _FakeClient.bookings = [accepted, cancelled, no_attendees]

    monkeypatch.setattr(module, "CalcomClient", _FakeClient)
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "out")
    # No --status flag → exercises the default (all), mirroring live behavior.
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run"])

    assert module.main() == 0
    lines = (tmp_path / "out" / "bookings.jsonl").read_text().strip().splitlines()
    uids = {json.loads(line)["payload"]["uid"] for line in lines}
    # Default (all) keeps cancelled; the gateless one is still dropped by the gate.
    assert uids == {accepted.uid, "c-1"}


def test_unknown_status_normalized_to_accepted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unfamiliar status string normalizes to 'accepted' (as the webhook does),
    so `--status accepted` includes it rather than silently dropping it."""
    module = _load_module()
    weird = _sample_booking().model_copy(update={"uid": "w-1", "status": "brand_new"})
    _FakeClient.bookings = [weird]

    monkeypatch.setattr(module, "CalcomClient", _FakeClient)
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run", "--status", "accepted"])

    assert module.main() == 0
    uids = {
        json.loads(line)["payload"]["uid"]
        for line in (tmp_path / "out" / "bookings.jsonl")
        .read_text()
        .strip()
        .splitlines()
    }
    assert uids == {"w-1"}


def test_local_validation_failure_dead_letters_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Records that fail local Webhook validation must land in failures.jsonl
    and force a non-zero exit — never silently dropped."""
    module = _load_module()
    _FakeClient.bookings = [_sample_booking()]

    class _RaisingWebhook:
        @staticmethod
        def model_validate(_env: object) -> object:
            raise ValueError("synthetic validation failure")

    monkeypatch.setattr(module, "CalcomClient", _FakeClient)
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(module, "Webhook", _RaisingWebhook)
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run"])

    rc = module.main()
    assert rc == 1  # non-zero: silent gap is not allowed

    failures = (tmp_path / "out" / "failures.jsonl").read_text().strip().splitlines()
    assert len(failures) == 1
    assert "local_validation" in json.loads(failures[0])["error"]
    # Nothing valid to send, so the envelope file is empty.
    assert (tmp_path / "out" / "bookings.jsonl").read_text().strip() == ""
