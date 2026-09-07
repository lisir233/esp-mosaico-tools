#!/usr/bin/env python3
"""Authorize one ESP-Mosaico Recovery HTTP system update."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


UPDATE_PATH = "/api/v1/system-update"
STATUS_PATH = "/api/v1/system-update/status"
HEALTH_PATH = "/api/v1/health"
CODE_HEADER = "X-Mosaico-Pairing-Code"
OPERATION_HEADER = "X-Mosaico-Operation-ID"
TERMINAL_STATES = {"committed", "failed"}


class TriggerError(RuntimeError):
    """A Recovery HTTP update request failed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def validate_update_code(value: str) -> str:
    if re.fullmatch(r"[0-9]{6}", value) is None:
        raise TriggerError("HTTP update code must be exactly six digits")
    return value


def validate_operation_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{32}", value) is None
        or value == "0" * 32
    ):
        raise TriggerError("Recovery returned an invalid operation ID")
    return value


def validate_manifest_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.fragment
        or "\\" in value
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise TriggerError("manifest URL must be an absolute HTTPS URL")
    return value


def _base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TriggerError("base URL must be an absolute HTTP(S) origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise TriggerError("base URL must not contain a path, query or fragment")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _json_response(response: Any) -> dict[str, Any]:
    media_type = response.headers.get_content_type()
    if media_type != "application/json":
        raise TriggerError(f"Recovery returned unexpected Content-Type {media_type!r}")
    try:
        value = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TriggerError("Recovery returned invalid JSON") from error
    if not isinstance(value, dict):
        raise TriggerError("Recovery response must be a JSON object")
    return value


class RecoveryHttpClient:
    def __init__(self, base_url: str, timeout: float = 10):
        self.base_url = _base_url(base_url)
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _open(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return _json_response(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = {"error": "invalid_error_response"}
            raise TriggerError(
                f"Recovery HTTP request failed with {error.code}: "
                f"{detail.get('error', 'unknown_error')}"
            ) from error
        except urllib.error.URLError as error:
            raise TriggerError(f"Recovery HTTP request failed: {error.reason}") from error

    def health(self) -> dict[str, Any]:
        return self._open(urllib.request.Request(self.base_url + HEALTH_PATH))

    def trigger(self, manifest_url: str, update_code: str) -> dict[str, Any]:
        manifest_url = validate_manifest_url(manifest_url)
        update_code = validate_update_code(update_code)
        body = json.dumps(
            {"manifest_url": manifest_url},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + UPDATE_PATH,
            data=body,
            headers={
                "Content-Type": "application/json",
                CODE_HEADER: update_code,
            },
            method="POST",
        )
        return self._open(request)

    def status(self, operation_id: str) -> dict[str, Any]:
        operation_id = validate_operation_id(operation_id)
        request = urllib.request.Request(
            self.base_url + STATUS_PATH,
            headers={OPERATION_HEADER: operation_id},
            method="GET",
        )
        return self._open(request)

    def wait(self, operation_id: str, *, timeout: float, interval: float) -> dict[str, Any]:
        operation_id = validate_operation_id(operation_id)
        deadline = time.monotonic() + timeout
        while True:
            status = self.status(operation_id)
            state = status.get("source_state")
            if state in TERMINAL_STATES:
                return status
            if time.monotonic() >= deadline:
                raise TriggerError("timed out waiting for the Recovery system update")
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--request-timeout", type=float, default=10)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    trigger = commands.add_parser("trigger")
    trigger.add_argument("manifest_url")
    trigger.add_argument("--wait-timeout", type=float, default=900)
    trigger.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args(argv)

    try:
        client = RecoveryHttpClient(args.base_url, timeout=args.request_timeout)
        if args.command == "health":
            result = client.health()
        else:
            update_code = validate_update_code(
                getpass.getpass("HTTP update code: ")
            )
            accepted = client.trigger(args.manifest_url, update_code)
            operation_id = validate_operation_id(accepted.get("operation_id"))
            result = client.wait(
                operation_id,
                timeout=args.wait_timeout,
                interval=args.poll_interval,
            )
    except (OSError, TriggerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("source_state") != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
