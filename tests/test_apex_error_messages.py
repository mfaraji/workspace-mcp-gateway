"""Apex error bodies and input-validation failures reach the client as short plain text."""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from gateway.providers.apex import client as apex_client
from gateway.providers.registry import ToolError, _validation_message


def _resp(status: int, **kwargs) -> httpx.Response:
    return httpx.Response(status, **kwargs)


def test_message_is_extracted_from_the_json_body():
    body = {"message": "Date range may not exceed 10 years", "error": "Bad Request"}
    assert apex_client.apex_error_message(_resp(400, json=body)) == (
        "Date range may not exceed 10 years"
    )


def test_message_lists_are_joined():
    body = {"message": ["a is required", "b must be a date"]}
    assert apex_client.apex_error_message(_resp(400, json=body)) == (
        "a is required; b must be a date"
    )


def test_internal_route_hints_are_removed():
    hint = "Look ids up with GET /api/transactions/categories."
    body = {"message": f'Unknown category id "x". {hint}'}
    text = apex_client.apex_error_message(_resp(400, json=body))
    assert text == 'Unknown category id "x".'


@pytest.mark.parametrize(
    "response", [_resp(502, text="<html>bad gateway</html>"), _resp(400, json={})]
)
def test_bodies_without_a_message_never_echo_the_body(response):
    assert apex_client.apex_error_message(response) == "no details were returned"


@pytest.mark.parametrize(
    "status,code",
    [(400, "invalid_input"), (401, "unauthorized"), (403, "forbidden"), (404, "not_found")],
)
def test_status_maps_to_a_stable_code_with_a_clean_message(status, code):
    body = {"message": "nope", "error": "X", "statusCode": status}
    with pytest.raises(ToolError) as exc_info:
        apex_client._raise_for_apex_status(_resp(status, json=body))
    assert exc_info.value.error_code == code
    assert str(exc_info.value) == f"apex request failed ({status}): nope"


def test_success_does_not_raise():
    apex_client._raise_for_apex_status(_resp(200, json={}))


class _Model(BaseModel):
    workspace_id: str
    count: int


def test_validation_message_names_fields_and_hides_values():
    with pytest.raises(ValidationError) as exc_info:
        _Model(workspace_id="top-secret-value", count="not-a-number")
    message = _validation_message(exc_info.value)
    assert message.startswith("count: ")
    for leaked in ("top-secret-value", "not-a-number", "pydantic", "http"):
        assert leaked not in message


def test_missing_field_is_reported_by_name():
    with pytest.raises(ValidationError) as exc_info:
        _Model(count=1)
    assert _validation_message(exc_info.value) == "workspace_id: Field required"


def test_non_validation_errors_get_a_generic_message():
    assert "schema" in _validation_message(TypeError("boom with /secret/path"))
    assert "secret" not in _validation_message(TypeError("boom with /secret/path"))
