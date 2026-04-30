from __future__ import annotations

from types import SimpleNamespace

from libs.attio.attributes import create_companies_attribute
from libs.attio.models import AttributeCreateResult


def _resp(data):
    return SimpleNamespace(data=data)


def _attr(slug: str):
    return SimpleNamespace(api_slug=slug)


class _FakeAttributes:
    def __init__(self, attrs=None):
        self.attrs = attrs or []
        self.created_attrs: list[dict[str, object]] = []

    def get_v2_target_identifier_attributes(self, *, target, identifier):
        assert target == "objects"
        assert identifier == "companies"
        return _resp(self.attrs)

    def post_v2_target_identifier_attributes(self, *, target, identifier, data):
        assert target == "objects"
        assert identifier == "companies"
        self.created_attrs.append(data)
        self.attrs.append(_attr(data["api_slug"]))
        return _resp(_attr(data["api_slug"]))


class _FakeClient:
    def __init__(self, attributes):
        self.attributes = attributes

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False


def test_create_companies_attribute_preview_reports_not_created(monkeypatch) -> None:
    fake = _FakeAttributes(attrs=[_attr("existing")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="GTM Tool Type",
        api_slug="gtm_tool_type",
        attribute_type="select",
        description="GTM stack role(s) this company serves",
        is_multiselect=True,
        apply=False,
    )

    assert isinstance(result, AttributeCreateResult)
    assert result.mode == "preview"
    assert result.attribute_title == "GTM Tool Type"
    assert result.attribute_slug == "gtm_tool_type"
    assert result.attribute_type == "select"
    assert result.attribute_exists is False
    assert result.attribute_created is False
    assert fake.created_attrs == []


def test_create_companies_attribute_apply_creates_when_missing(monkeypatch) -> None:
    fake = _FakeAttributes(attrs=[_attr("existing")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="GTM Tool Type",
        api_slug="gtm_tool_type",
        attribute_type="select",
        description="GTM stack role(s) this company serves",
        is_multiselect=True,
        apply=True,
    )

    assert result.mode == "apply"
    assert result.attribute_exists is False
    assert result.attribute_created is True
    assert len(fake.created_attrs) == 1
    assert fake.created_attrs[0]["title"] == "GTM Tool Type"
    assert fake.created_attrs[0]["api_slug"] == "gtm_tool_type"
    assert fake.created_attrs[0]["type"] == "select"
    assert fake.created_attrs[0]["is_multiselect"] is True


def test_create_companies_attribute_apply_is_noop_when_already_exists(
    monkeypatch,
) -> None:
    fake = _FakeAttributes(attrs=[_attr("gtm_tool_type")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="GTM Tool Type",
        api_slug="gtm_tool_type",
        attribute_type="select",
        description="GTM stack role(s) this company serves",
        is_multiselect=True,
        apply=True,
    )

    assert result.mode == "apply"
    assert result.attribute_exists is True
    assert result.attribute_created is False
    assert fake.created_attrs == []
