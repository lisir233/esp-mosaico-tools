from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest


TOOL = pathlib.Path(__file__).parents[1] / "tools" / "http_update_trigger.py"
SPEC = importlib.util.spec_from_file_location("http_update_trigger", TOOL)
assert SPEC is not None and SPEC.loader is not None
http_update_trigger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = http_update_trigger
SPEC.loader.exec_module(http_update_trigger)


def test_update_code_is_exact_and_preserves_leading_zero() -> None:
    assert http_update_trigger.validate_update_code("038271") == "038271"
    for value in ("", "38271", "0382710", "abcdef", "１２３４５６"):
        with pytest.raises(http_update_trigger.TriggerError):
            http_update_trigger.validate_update_code(value)


def test_trigger_uses_code_header_and_compact_exact_body(monkeypatch) -> None:
    client = http_update_trigger.RecoveryHttpClient(
        "http://mosaico-test.local:8080"
    )
    captured = {}

    def open_request(request):
        captured["request"] = request
        return {"state": "accepted", "operation_id": "ab" * 16}

    monkeypatch.setattr(client, "_open", open_request)
    result = client.trigger(
        "https://updates.example/release/manifest.json", "038271"
    )
    request = captured["request"]
    assert result["state"] == "accepted"
    assert request.full_url.endswith(http_update_trigger.UPDATE_PATH)
    assert request.method == "POST"
    assert request.data == (
        b'{"manifest_url":"https://updates.example/release/manifest.json"}'
    )
    assert request.get_header("X-mosaico-pairing-code") == "038271"
    assert "038271" not in request.full_url


def test_status_uses_only_operation_id_header(monkeypatch) -> None:
    client = http_update_trigger.RecoveryHttpClient(
        "http://mosaico-test.local:8080"
    )
    captured = {}

    def open_request(request):
        captured["request"] = request
        return {"source_state": "applying"}

    monkeypatch.setattr(client, "_open", open_request)
    operation_id = "12" * 16
    client.status(operation_id)
    request = captured["request"]
    assert request.method == "GET"
    assert request.get_header("X-mosaico-operation-id") == operation_id
    assert request.data is None


@pytest.mark.parametrize(
    "value",
    ["", "00" * 15, "00" * 16, "00" * 17, "GG" * 16, "AA" * 16],
)
def test_operation_id_is_canonical_and_bounded(value: str) -> None:
    with pytest.raises(http_update_trigger.TriggerError):
        http_update_trigger.validate_operation_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://updates.example/release/manifest.json",
        "https://updates.example",
        "https://user:password@updates.example/release/manifest.json",
        "https://updates.example/release/manifest.json#fragment",
        "https://updates.example\\release\\manifest.json",
    ],
)
def test_trigger_rejects_noncanonical_https_manifest(value: str) -> None:
    with pytest.raises(http_update_trigger.TriggerError, match="absolute HTTPS"):
        http_update_trigger.validate_manifest_url(value)


def test_wait_polls_operation_until_terminal(monkeypatch) -> None:
    client = http_update_trigger.RecoveryHttpClient(
        "http://mosaico-test.local:8080"
    )
    states = iter(
        [
            {"source_state": "waiting_network"},
            {"source_state": "applying"},
            {"source_state": "committed"},
        ]
    )
    seen = []

    def status(operation_id):
        seen.append(operation_id)
        return next(states)

    monkeypatch.setattr(client, "status", status)
    monkeypatch.setattr(http_update_trigger.time, "sleep", lambda _: None)
    result = client.wait("34" * 16, timeout=5, interval=0.01)
    assert result == {"source_state": "committed"}
    assert seen == ["34" * 16] * 3


def test_cli_reads_code_with_getpass_and_never_prints_it(monkeypatch, capsys) -> None:
    code = "038271"

    class FakeClient:
        def __init__(self, base_url, timeout):
            assert base_url == "http://mosaico-test.local:8080"
            assert timeout == 10

        def trigger(self, manifest_url, update_code):
            assert manifest_url.startswith("https://")
            assert update_code == code
            return {"operation_id": "56" * 16, "state": "accepted"}

        def wait(self, operation_id, *, timeout, interval):
            assert operation_id == "56" * 16
            assert timeout == 900
            assert interval == 0.5
            return {"operation_id": operation_id, "source_state": "committed"}

    monkeypatch.setattr(http_update_trigger, "RecoveryHttpClient", FakeClient)
    monkeypatch.setattr(http_update_trigger.getpass, "getpass", lambda _: code)
    result = http_update_trigger.main(
        [
            "--base-url",
            "http://mosaico-test.local:8080",
            "trigger",
            "https://updates.example/release/manifest.json",
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert code not in captured.out
    assert code not in captured.err


def test_cli_has_no_code_argument() -> None:
    with pytest.raises(SystemExit):
        http_update_trigger.main(
            [
                "--base-url",
                "http://mosaico-test.local:8080",
                "trigger",
                "https://updates.example/release/manifest.json",
                "--code",
                "038271",
            ]
        )
