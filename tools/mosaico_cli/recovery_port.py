"""Explicit independent USB Serial/JTAG routing, without inferred board identity."""

from __future__ import annotations

import os
from typing import Any

from .errors import DeviceError, SelectionError


def same_port(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def serial_jtag_candidate(requested: str) -> dict[str, Any]:
    from serial.tools import list_ports

    candidates = [port for port in list_ports.comports()
                  if port.vid == 0x303A and port.pid == 0x1001]
    if len(candidates) != 1:
        raise SelectionError("--recovery-port requires exactly one connected USB Serial/JTAG 303A:1001 interface.")
    port = candidates[0]
    if not same_port(requested, str(port.device)):
        raise DeviceError("--recovery-port does not identify the unique USB Serial/JTAG 303A:1001 interface.")
    return {"path": str(port.device), "vid": port.vid, "pid": port.pid,
            "serial_number": str(getattr(port, "serial_number", None) or ""),
            "location": str(getattr(port, "location", None) or "")}


def lease_port(lease: dict[str, Any]) -> str:
    endpoint = lease.get("endpoint")
    if not isinstance(endpoint, dict):
        raise DeviceError("The maintenance lease did not include a physical endpoint.")
    path = str(endpoint.get("path") or "")
    raw = str(endpoint.get("endpoint") or "")
    path = path or (raw[4:] if raw.startswith("usb:") else "")
    if not path:
        raise DeviceError("The maintenance lease did not include a writable local port.")
    return path
