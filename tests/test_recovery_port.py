from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_cli import REPOSITORY, WORKSPACE

# isort: split
# The shared fixture module adds the repository-local tools package to sys.path.
from mosaico_cli import commands
from mosaico_cli.cli import build_parser
from mosaico_cli.errors import DeviceError, OperationError, SelectionError
from mosaico_cli.recovery_port import serial_jtag_candidate


def _check_explicit_port_rejects_missing_ambiguous_or_wrong_interface(ports, requested, error):
    with mock.patch("serial.tools.list_ports.comports", return_value=ports), unittest.TestCase().assertRaises(error):
        serial_jtag_candidate(requested)


def _check_explicit_port_records_usb_identity():
    port = SimpleNamespace(device="COM14", vid=0x303A, pid=0x1001, serial_number="usb-serial", location="1-2")
    with mock.patch("serial.tools.list_ports.comports", return_value=[port]):
        assert serial_jtag_candidate("COM14") == {
            "path": "COM14", "vid": 0x303A, "pid": 0x1001,
            "serial_number": "usb-serial", "location": "1-2"}
    assert build_parser().parse_args(["recover", "--recovery-port", "COM14"]).recovery_port == "COM14"


def _check_independent_recovery_holds_both_leases_and_verifies_original_device(tmp_path, monkeypatch, failure):
    arguments = SimpleNamespace(model=None, source="reviewed", device_id="device-a", gateway_profile=None,
                                timeout=180, dry_run=failure == "dry_run", recovery_port="COM14")
    context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY, directory=tmp_path,
                        log_path=tmp_path / "run.log")
    session = SimpleNamespace(started_local=False)
    manifest = {"version": "2.1.1-recovery", "images": {"recovery": {}}}
    primary = {"lease_id": "primary", "token": "SECRET", "endpoint": {"path": "COM13"},
               "evidence": {"device_before": {"device_id": "device-a", "boot_id": "old"}}}
    auxiliary = {"lease_id": "auxiliary", "token": "SECRET2", "endpoint": {"path": "COM14"},
                 "evidence": {"expected_device_id": "other" if failure == "wrong_owner" else None}}
    status = {"device_id": "other" if failure == "wrong_device" else "device-a",
              "boot_id": "old" if failure == "old_boot" else "new", "firmware_mode": "recovery",
              "app_version": "2.1.1-recovery"}
    identity = {"path": "COM14", "vid": 0x303A, "pid": 0x1001, "serial_number": "serial", "location": "1-2"}
    order = []

    def acquire(*args, **kwargs):
        order.append("primary")
        return primary

    def acquire_aux(*args, **kwargs):
        order.append("auxiliary")
        if failure == "lease_failure":
            raise DeviceError("busy")
        return auxiliary

    def target(*args, **kwargs):
        order.append(kwargs["target"])
        if kwargs["target"] == "mosaico-recover-flash":
            assert kwargs["port"] == "COM14"
            assert "primary" in order and "auxiliary" in order
            if failure == "flash":
                raise OperationError("flash failed")

    def finish(*args, **kwargs):
        order.append((args[2]["lease_id"], kwargs["abort"]))
        return {"state": "released", "evidence": {"verification": status}}

    patches = {
        "load_bundle": mock.Mock(return_value=manifest),
        "ensure_gateway": mock.Mock(return_value=session),
        "connected_devices": mock.Mock(return_value=[] if failure == "no_live" else [{"device_id": "device-a", "boot_id": "old"}]),
        "gateway_json": mock.Mock(return_value={"device_id": "device-a", "boot_id": "old"}),
        "serial_jtag_candidate": mock.Mock(side_effect=[identity, {**identity, "serial_number": "changed"} if failure == "changed_port" else identity]),
        "resolve_idf_path": mock.Mock(return_value=Path("/idf")),
        "run_idf_target": mock.Mock(side_effect=target),
        "acquire_maintenance_lease": mock.Mock(side_effect=acquire),
        "acquire_endpoint_maintenance_lease": mock.Mock(side_effect=acquire_aux),
        "renew_maintenance_lease": mock.Mock(),
        "finish_maintenance_lease": mock.Mock(side_effect=finish),
        "record_recovery_verification": mock.Mock(),
    }
    for name, replacement in patches.items():
        monkeypatch.setattr(commands, name, replacement)
    if failure == "dry_run":
        result = commands.recover(arguments, context)
        assert result["status"] == "dry_run"
        assert order == []
        patches["acquire_maintenance_lease"].assert_not_called()
    elif failure:
        with unittest.TestCase().assertRaises((DeviceError, OperationError)):
            commands.recover(arguments, context)
        patches["record_recovery_verification"].assert_not_called()
        if failure not in {"no_live", "wrong_device", "old_boot"}:
            assert ("primary", True) in order
        if failure not in {"no_live", "lease_failure"}:
            assert ("auxiliary", True) in order
        if failure in {"changed_port", "wrong_owner", "no_live", "lease_failure"}:
            assert "mosaico-recover-flash" not in order
    else:
        result = commands.recover(arguments, context)
        assert result["status"] == "succeeded"
        assert result["device_id"] == "device-a"
        assert result["recovery_endpoint_lease_id"] == "auxiliary"
        assert order == ["mosaico-recover-prepare", "primary", "auxiliary",
                         "mosaico-recover-flash", ("primary", False), ("auxiliary", True)]
        evidence = (tmp_path / "recovery-route.json").read_text()
        assert "COM14" in evidence and "device-a" in evidence
        assert "SECRET" not in evidence


class RecoveryPortTests(unittest.TestCase):
    def test_identity_and_parser(self):
        _check_explicit_port_records_usb_identity()


def _port_test(ports, requested, error):
    def check(self):
        _check_explicit_port_rejects_missing_ambiguous_or_wrong_interface(ports, requested, error)
    return check


for _name, _ports, _requested, _error in [
    ("missing", [], "COM14", SelectionError),
    ("ambiguous", [SimpleNamespace(device="COM14", vid=0x303A, pid=0x1001),
                   SimpleNamespace(device="COM15", vid=0x303A, pid=0x1001)], "COM14", SelectionError),
    ("wrong_path", [SimpleNamespace(device="COM14", vid=0x303A, pid=0x1001)], "COM15", DeviceError),
    ("wrong_pid", [SimpleNamespace(device="COM14", vid=0x303A, pid=0x1002)], "COM14", SelectionError),
]:
    setattr(RecoveryPortTests, "test_port_" + _name, _port_test(_ports, _requested, _error))


def _recovery_test(failure):
    def check(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            patcher = SimpleNamespace(setattr=lambda obj, name, value: stack.enter_context(mock.patch.object(obj, name, value)))
            _check_independent_recovery_holds_both_leases_and_verifies_original_device(Path(folder), patcher, failure)
    return check


for _failure in [None, "dry_run", "no_live", "changed_port", "wrong_owner", "flash", "wrong_device", "old_boot", "lease_failure"]:
    setattr(RecoveryPortTests, "test_recovery_" + str(_failure or "success"), _recovery_test(_failure))
