from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = TOOL_ROOT / "tests" / "fixtures" / "workspace"
sys.path.insert(0, str(TOOL_ROOT / "tools"))

from mosaico_cli.cli import (
    _emit_error,
    build_parser,
    main,
    _normalize_globals,
)
from mosaico_cli.build_progress import (
    BuildProgressReporter,
    decode_progress,
    encode_progress,
)
from mosaico_cli.commands import (
    _device_status,
    _MonitorTextRenderer,
    _recovery_verification_status,
    install,
    list_devices,
    monitor,
    recover,
    start_system_update,
)
from mosaico_cli.errors import (
    BuildError,
    DeviceError,
    EnvironmentError,
    OperationError,
    OutcomeUnknownError,
    RecoveryRequiredError,
    SelectionError,
)
from mosaico_cli.gateway import (
    GatewaySession,
    _require_compatible_gateway,
    acquire_endpoint_maintenance_lease,
    acquire_maintenance_lease,
    enter_recovery_and_wait,
    ensure_gateway,
    ensure_iris_tools,
    iris_environment_root,
    locate_iris_tools,
    run_ota,
    run_system_update_bundle,
    select_device,
    system_inventory,
)
from mosaico_cli.host import (
    HostEnvironmentError,
    IdfEnvironment,
    _parse_idf_exports,
    prepare_idf_environment,
    resolve_idf_bootstrap_python,
    state_root,
    virtual_environment_python,
)
from mosaico_cli.project import partition_table_flash_sha256, resolve_project
from mosaico_cli.recovery import (
    _host_verification_path,
    _read_verification_record,
    _registered_recovery_ports,
    _verification_path,
    load_bundle,
    provisioning_candidate,
    recovery_is_verified,
    recovery_verification_details,
)
from mosaico_cli.registry import load_registry, select_model
from mosaico_cli.runtime import (
    RunContext,
    _build_progress_parser,
    build_application,
    run_idf_target,
)
from mosaico_cli.workspace import WorkspaceConfig, load_workspace


WORKSPACE = load_workspace(TOOL_ROOT, explicit=str(REPOSITORY))


def workspace_for(root: Path, *, default_project: Path | None = None) -> WorkspaceConfig:
    root = root.resolve()
    return replace(
        WORKSPACE,
        root=root,
        config_path=root / ".mosaico.json",
        projects_dir=root / "projects",
        default_project=default_project,
        environment_file=root / "Environment",
        run_dir=root / ".codex-runs" / "mosaico",
        idf_constraint_manifest=root / "projects" / "factory" / "main" / "idf_component.yml",
        bsp_path=root / "submodule" / "esp-mosaico-bsp",
        esp_iris_path=root / "submodule" / "esp-iris",
    )


def create_recovery_bundle(directory: Path) -> Path:
    directory.mkdir(parents=True)
    files = {
        "bootloader": ("bootloader.bin", b"bootloader"),
        "partition_table": ("partition-table.bin", b"partitions"),
        "ota_data": ("ota_data_initial.bin", b"ota-data"),
        "recovery": ("factory.bin", b"recovery-firmware"),
    }
    images: dict[str, dict[str, object]] = {}
    for index, (name, (filename, payload)) in enumerate(files.items()):
        path = directory / filename
        path.write_bytes(payload)
        images[name] = {
            "file": filename,
            "offset": hex(0x1000 + index * 0x10000),
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "target": "esp32s31",
                "profile": "recovery",
                "version": "test-recovery",
                "images": images,
            }
        ),
        encoding="utf-8",
    )
    return directory


class ParserTests(unittest.TestCase):
    def parse(self, *argv: str) -> argparse.Namespace:
        return build_parser().parse_args(_normalize_globals(argv))

    def test_install_defaults(self) -> None:
        value = self.parse("install")
        self.assertEqual(value.validation, "elf-sha256")
        self.assertEqual(value.timeout, 600)
        self.assertFalse(value.skip_build)

    def test_system_update_defaults_to_local_build(self) -> None:
        value = self.parse("system-update")
        self.assertIsNone(value.bundle)
        self.assertIsNone(value.manifest_url)
        self.assertIsNone(value.manifest_path)
        self.assertFalse(value.skip_build)
        self.assertEqual(value.timeout, 900)

    def test_system_update_accepts_local_bundle(self) -> None:
        value = self.parse("system-update", "--bundle", "release.irisfw")
        self.assertEqual(value.bundle, Path("release.irisfw"))

    def test_recover_defaults(self) -> None:
        value = self.parse("recover")
        self.assertEqual(value.source, "reviewed")
        self.assertEqual(value.timeout, 180)
        self.assertFalse(value.dry_run)

    def test_monitor_defaults(self) -> None:
        value = self.parse("monitor")
        self.assertEqual(value.timeout, 0)
        self.assertFalse(value.snapshot)
        self.assertFalse(value.force_color)
        self.assertFalse(value.disable_auto_color)

    def test_system_update_requires_valid_manifest_url(self) -> None:
        value = self.parse(
            "system-update",
            "--manifest-url",
            "https://updates.example.test/release/manifest.json",
        )
        self.assertEqual(value.command, "system-update")
        self.assertIsNone(value.device_id)
        with ExitStack() as _contexts:
            caught = _contexts.enter_context(self.assertRaises(SystemExit))
            self.parse("system-update", "--manifest-url", "file:///manifest.json")
        self.assertEqual(caught.exception.code, 2)

    def test_system_update_accepts_nand_manifest_path(self) -> None:
        value = self.parse(
            "system-update",
            "--manifest-path",
            "/nand/system-update/manifest.json",
        )
        self.assertEqual(value.manifest_path, "/nand/system-update/manifest.json")
        self.assertIsNone(value.manifest_url)
        with ExitStack() as _contexts:
            caught = _contexts.enter_context(self.assertRaises(SystemExit))
            self.parse(
                "system-update",
                "--manifest-path",
                "/nand/system-update/../manifest.json",
            )
        self.assertEqual(caught.exception.code, 2)

    def test_global_flags_work_after_command(self) -> None:
        value = self.parse("list", "--gateway-profile", "bench", "--json", "--verbose")
        self.assertTrue(value.json)
        self.assertTrue(value.verbose)
        self.assertEqual(value.gateway_profile, "bench")

    def test_list_json_returns_live_devices(self) -> None:
        result = {
            "command": "list",
            "status": "succeeded",
            "gateway_started": False,
            "gateway_profile": None,
            "devices": [{"device_id": "device-a"}],
        }
        output = io.StringIO()
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.cli.MosaicoArgumentParser.json_errors", False)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.cli.list_devices", return_value=result)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.cli.RunContext",
                    return_value=SimpleNamespace(log_path=Path("run.log")),
                )
            )
            _contexts.enter_context(redirect_stdout(output))
            self.assertEqual(
                main(["list", "--json", "--workspace", str(REPOSITORY)]), 0
            )
        self.assertEqual(
            json.loads(output.getvalue())["devices"][0]["device_id"], "device-a"
        )

    def test_doctor_is_public_and_has_no_device_options(self) -> None:
        value = self.parse("doctor", "--json")
        self.assertEqual(value.command, "doctor")
        self.assertTrue(value.json)

    def test_doctor_json_is_stable(self) -> None:
        diagnosis = {
            "command": "doctor",
            "status": "ready",
            "host": {},
            "idf": {},
            "iris": {},
            "checks": [],
            "exit_code": 0,
        }
        output = io.StringIO()
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.cli.MosaicoArgumentParser.json_errors", False)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.cli.diagnose_host", return_value=diagnosis)
            )
            _contexts.enter_context(redirect_stdout(output))
            self.assertEqual(
                main(["doctor", "--json", "--workspace", str(REPOSITORY)]), 0
            )
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")

    def test_python_below_38_is_rejected(self) -> None:
        output = io.StringIO()
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.cli.MosaicoArgumentParser.json_errors", False)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.cli.sys.version_info", (3, 7, 17))
            )
            _contexts.enter_context(redirect_stderr(output))
            self.assertEqual(
                main(["doctor", "--json", "--workspace", str(REPOSITORY)]), 3
            )
        report = json.loads(output.getvalue())
        self.assertEqual(report["error"], "environment_error")
        self.assertIn("Python 3.8", report["message"])

    def test_no_public_recovery_port(self) -> None:
        options = build_parser().format_help()
        recover_help = (
            build_parser()
            ._subparsers._group_actions[0]
            .choices["recover"]
            .format_help()
        )
        self.assertNotIn("--port", options + recover_help)

    def test_recover_has_no_confirmation_option(self) -> None:
        with ExitStack() as _contexts:
            caught = _contexts.enter_context(self.assertRaises(SystemExit))
            self.parse("recover", "--yes")
        self.assertEqual(caught.exception.code, 2)

    def test_recover_has_no_remote_gateway_option(self) -> None:
        with ExitStack() as _contexts:
            caught = _contexts.enter_context(self.assertRaises(SystemExit))
            self.parse("recover", "--gateway-profile", "remote")
        self.assertEqual(caught.exception.code, 2)

    def test_user_visible_literals_are_english_only(self) -> None:
        han_character = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
        package = TOOL_ROOT / "tools" / "mosaico_cli"
        violations: list[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and han_character.search(node.value)
                ):
                    violations.append(f"{path.name}:{node.lineno}: {node.value!r}")
        self.assertEqual(violations, [])


class BuildDiagnosticsTests(unittest.TestCase):
    def test_build_failure_prints_bounded_diagnostic_without_verbose(self) -> None:
        expected_summary = (
            "IDF LOW-NOISE BUILD: FAILED\n"
            "category: compiler\n"
            "error: main/main.c:7:5: error: expected ';' before '}'\n"
            "log: /runs/build/raw.log"
        )
        summary = encode_progress("building 10% (10/100)") + "\n" + expected_summary
        context = mock.Mock(
            workspace=WORKSPACE,
            directory=Path("/runs"),
            log_path=Path("/runs/raw.log"),
        )
        context.run.return_value = subprocess.CompletedProcess([], 1, summary, None)
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.runtime.resolve_idf_path", return_value=Path("/idf")
                )
            )
            caught = _contexts.enter_context(self.assertRaises(BuildError))
            build_application(context, Path("/project"))

        self.assertEqual(caught.exception.details["diagnostic"], expected_summary)
        invocation = context.run.call_args
        self.assertIn("--progress", invocation.args[0])
        progress = invocation.kwargs["output_status"]
        self.assertEqual(
            progress(encode_progress("configuring (CMake)")),
            "build: configuring (CMake)",
        )
        stderr = io.StringIO()
        with ExitStack() as _contexts:
            _contexts.enter_context(redirect_stderr(stderr))
            _emit_error(caught.exception, json_output=False, verbose=False)
        output = stderr.getvalue()
        self.assertIn("mosaico: Application build failed.", output)
        self.assertIn("main/main.c:7:5: error: expected ';'", output)
        self.assertIn(f"Build logs: {Path('/runs/build')}", output)


class BuildProgressTests(unittest.TestCase):
    def test_protocol_ignores_regular_and_malformed_output(self) -> None:
        encoded = encode_progress("building 20% (2/10)")
        self.assertEqual(decode_progress(encoded), "building 20% (2/10)")
        self.assertIsNone(decode_progress("[2/10] Building C object"))
        self.assertIsNone(decode_progress("@@MOSAICO_BUILD_PROGRESS@@{"))
        self.assertEqual(
            _build_progress_parser()(encoded), "build: building 20% (2/10)"
        )

    def test_reports_phases_coarse_progress_and_heartbeats(self) -> None:
        now = [0.0]
        events: list[str] = []
        reporter = BuildProgressReporter(events.append, clock=lambda: now[0])

        reporter.consume("[0/1] Re-running CMake...\n")
        now[0] = 9.9
        reporter.heartbeat()
        now[0] = 10.0
        reporter.heartbeat()
        now[0] = 24.0
        reporter.consume("NOTE: Processing 30 dependencies:\n")
        reporter.consume("-- Configuring done (24.0s)\n")
        reporter.consume("[1/27] Building C object main.c.obj\n")
        reporter.consume("[2/27] Building C object support.c.obj\n")
        reporter.consume("[3/27] Building C object transport.c.obj\n")
        reporter.consume("[1/1] Checking bootloader image size\n")
        reporter.consume("[25/27] Linking CXX executable hello_world.elf\n")
        reporter.consume("[26/27] Generating binary image from built executable\n")
        reporter.consume("[27/27] check_sizes.py partition application.bin\n")
        reporter.complete(
            duration_seconds=30.2,
            warnings=0,
            artifact_size=1_147_936,
        )

        self.assertEqual(
            events,
            [
                "configuring (CMake)",
                "configuring (10s elapsed)",
                "resolving 30 dependencies",
                "configuration complete (24.0s)",
                "building 3% (1/27)",
                "building 11% (3/27)",
                "linking application",
                "building 92% (25/27)",
                "generating firmware image",
                "checking firmware size",
                "building 100% (27/27)",
                "complete in 30.2s, warnings: 0, firmware: 1.09 MiB",
            ],
        )
        self.assertNotIn("building 100% (1/1)", events)


class RegistryAndSelectionTests(unittest.TestCase):
    def test_registry_has_supported_default(self) -> None:
        models = load_registry(WORKSPACE)
        self.assertEqual(len(models), 1)
        self.assertEqual(select_model(WORKSPACE, None).target, "esp32s31")

    def test_device_selection(self) -> None:
        item = {"device_id": "one"}
        self.assertIs(select_device([item], None), item)
        with ExitStack() as _contexts:
            _contexts.enter_context(self.assertRaises(SelectionError))
            select_device([item, {"device_id": "two"}], None)
        with ExitStack() as _contexts:
            _contexts.enter_context(self.assertRaises(DeviceError))
            select_device([], None)

    def test_selection_error_prints_available_device_ids(self) -> None:
        error = SelectionError(
            "choose a device", details={"candidates": ["device-a", "device-b"]}
        )
        output = io.StringIO()
        with ExitStack() as _contexts:
            _contexts.enter_context(redirect_stderr(output))
            _emit_error(error, json_output=False, verbose=False)
        self.assertIn("device-a", output.getvalue())
        self.assertIn("device-b", output.getvalue())


class GatewayTests(unittest.TestCase):
    def test_general_gateway_compatibility_does_not_require_inventory(self) -> None:
        health = {
            "gateway_api": {"major": 1, "minor": 1},
            "capabilities": ["device-maintenance-lease/v1"],
        }
        self.assertIs(_require_compatible_gateway(health), health)

    def test_system_inventory_requires_partition_preflight_capability(self) -> None:
        with mock.patch(
            "mosaico_cli.gateway.gateway_json",
            return_value={"capabilities": ["device-maintenance-lease/v1"]},
        ), self.assertRaises(EnvironmentError) as caught:
            system_inventory(mock.Mock(), mock.Mock(), "device-a")
        self.assertIn("partition-table preflight", str(caught.exception))

    def test_system_inventory_validates_and_normalizes_partition_hash(self) -> None:
        context = mock.Mock()
        session = mock.Mock()
        with mock.patch(
            "mosaico_cli.gateway.gateway_json",
            side_effect=[
                {"capabilities": ["system-inventory/v1"]},
                {"inventory": {"partition_table_sha256": "AB" * 32}},
            ],
        ) as request:
            inventory = system_inventory(context, session, "device-a")

        self.assertEqual(inventory["partition_table_sha256"], "ab" * 32)
        self.assertEqual(
            [call.args[2:] for call in request.call_args_list],
            [("health",), ("system-inventory", "device-a")],
        )

    def test_system_inventory_rejects_missing_partition_hash(self) -> None:
        with mock.patch(
            "mosaico_cli.gateway.gateway_json",
            side_effect=[
                {"capabilities": ["system-inventory/v1"]},
                {"inventory": {"layout_version": 4}},
            ],
        ), self.assertRaises(DeviceError):
            system_inventory(mock.Mock(), mock.Mock(), "device-a")

    def test_enter_recovery_waits_through_rpc_disconnect(self) -> None:
        context = mock.Mock(log_path=Path("/runs/raw.log"))
        session = mock.Mock()
        with ExitStack() as contexts:
            request = contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway.gateway_json",
                    side_effect=[
                        DeviceError("USB disconnected"),
                        {"device": {"firmware_mode": "normal", "boot_id": 10}},
                        {"device": {"firmware_mode": "recovery", "boot_id": 11}},
                    ],
                )
            )
            contexts.enter_context(mock.patch("mosaico_cli.gateway.time.sleep"))
            status = enter_recovery_and_wait(
                context,
                session,
                "device-a",
                previous_boot_id=10,
                timeout=30,
            )

        self.assertEqual(status["firmware_mode"], "recovery")
        self.assertEqual(status["boot_id"], 11)
        self.assertEqual(request.call_args_list[0].args[2:], ("factory", "device-a"))
        self.assertEqual(request.call_args_list[-1].args[2:], ("status", "device-a"))

    def test_local_system_update_builds_and_submits_atomic_bundle(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            project = root / "project"
            build_dir = project / "build"
            build_dir.mkdir(parents=True)
            bundle = build_dir / "edge_agent-system-update.irisfw"
            bundle.write_bytes(b"bundle")
            artifacts = SimpleNamespace(
                build_dir=build_dir,
                project_name="edge_agent",
            )
            arguments = argparse.Namespace(
                gateway_profile=None,
                device_id=None,
                manifest_url=None,
                manifest_path=None,
                bundle=None,
                project=str(project),
                skip_build=False,
                timeout=900,
            )
            context = mock.Mock(
                workspace=workspace_for(root, default_project=project),
                log_path=Path("run.log"),
            )
            session = GatewaySession(Path("python"), Path("iris"), (), None, False)
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.resolve_project", return_value=project)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.resolve_idf_path", return_value=Path("/idf"))
            )
            build = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.run_idf_target")
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.discover_artifacts",
                    return_value=artifacts,
                )
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.connected_devices",
                    return_value=[{"device_id": "device-a", "firmware_mode": "normal"}],
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.gateway_json",
                    return_value={"device": {"firmware_mode": "normal"}},
                )
            )
            submit = _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.run_system_update_bundle",
                    return_value={"operation": {"status": "succeeded"}},
                )
            )
            result = start_system_update(arguments, context)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["source"], "local_build")
        self.assertEqual(result["bundle"], str(bundle))
        self.assertEqual(build.call_args.kwargs["target"], "system-update-bundle")
        submit.assert_called_once_with(
            context,
            session,
            device_id="device-a",
            bundle=bundle,
            timeout=900,
        )

    def test_list_devices_reads_the_selected_gateway(self) -> None:
        context = mock.Mock()
        session = GatewaySession(
            Path("python"), Path("iris"), ("--profile", "bench"), "bench", False
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.gateway_devices",
                    return_value=[
                        {
                            "device_id": "device-b",
                            "connected": False,
                            "transport_name": "TCP",
                        },
                        {
                            "device_id": "device-a",
                            "connected": True,
                            "transport_name": "USB Highspeed",
                        },
                    ],
                )
            )
            result = list_devices(context, "bench")
        self.assertEqual(
            [item["device_id"] for item in result["devices"]],
            ["device-a", "device-b"],
        )
        self.assertEqual(result["gateway_profile"], "bench")
        self.assertTrue(result["devices"][0]["online"])
        self.assertEqual(result["devices"][0]["connection"], "USB Highspeed")
        self.assertFalse(result["devices"][1]["online"])

    def test_http_system_update_uses_recovery_rpc_and_hides_url(self) -> None:
        context = mock.Mock(log_path=Path("run.log"))
        session = GatewaySession(
            Path("python"), Path("iris"), ("--profile", "bench"), "bench", False
        )
        arguments = argparse.Namespace(
            gateway_profile="bench",
            device_id="device-a",
            manifest_url="https://updates.example.test/release/manifest.json?token=secret",
            manifest_path=None,
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.connected_devices",
                    return_value=[
                        {"device_id": "device-a", "firmware_mode": "recovery"}
                    ],
                )
            )
            gateway = _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.gateway_json",
                    side_effect=[
                        {"device": {"firmware_mode": "recovery", "boot_id": "boot-a"}},
                        {"response": {"payload_hex": ""}},
                    ],
                )
            )
            result = start_system_update(arguments, context)

        self.assertEqual(result["status"], "accepted")
        rpc_call = gateway.call_args_list[1]
        self.assertEqual(
            rpc_call.args[2:7], ("rpc-raw", "device-a", "0x1201", "1", "--payload")
        )
        self.assertTrue(rpc_call.kwargs["sensitive_output"])

    def test_nand_system_update_uses_recovery_rpc_method_two(self) -> None:
        context = mock.Mock(log_path=Path("run.log"))
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        arguments = argparse.Namespace(
            gateway_profile=None,
            device_id="device-a",
            manifest_url=None,
            manifest_path="/nand/system-update/manifest.json",
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.connected_devices",
                    return_value=[
                        {"device_id": "device-a", "firmware_mode": "recovery"}
                    ],
                )
            )
            gateway = _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.gateway_json",
                    side_effect=[
                        {"device": {"firmware_mode": "recovery", "boot_id": "boot-a"}},
                        {"response": {"payload_hex": ""}},
                    ],
                )
            )
            result = start_system_update(arguments, context)

        self.assertEqual(result["source"], "nand")
        rpc_call = gateway.call_args_list[1]
        self.assertEqual(rpc_call.args[5], "2")
        self.assertEqual(rpc_call.args[7], "/nand/system-update/manifest.json")

    def test_http_system_update_rejects_normal_firmware(self) -> None:
        context = mock.Mock(log_path=Path("run.log"))
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        arguments = argparse.Namespace(
            gateway_profile=None,
            device_id="device-a",
            manifest_url="https://updates.example.test/manifest.json",
            manifest_path=None,
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.connected_devices",
                    return_value=[{"device_id": "device-a", "firmware_mode": "normal"}],
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.gateway_json",
                    return_value={"device": {"firmware_mode": "normal"}},
                )
            )
            _contexts.enter_context(self.assertRaises(RecoveryRequiredError))
            start_system_update(arguments, context)

    def test_iris_tools_are_found_only_in_the_pinned_submodule(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            repository = Path(temporary)
            component = (
                repository / "submodule" / "esp-iris" / "components" / "esp_iris"
            )
            (component / "tools").mkdir(parents=True)
            script = component / "tools" / "esp_iris.py"
            script.write_text("# test\n", encoding="utf-8")
            python = virtual_environment_python(
                repository / "submodule" / "esp-iris" / ".venv"
            )
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            competing = (
                repository
                / "projects"
                / "aaa"
                / "managed_components"
                / "esp_iris"
                / "tools"
            )
            competing.mkdir(parents=True)
            (competing / "esp_iris.py").write_text("# wrong\n", encoding="utf-8")

            with mock.patch(
                "mosaico_cli.gateway._python_major_minor",
                return_value=(sys.version_info.major, sys.version_info.minor),
            ):
                discovered_python, discovered = locate_iris_tools(
                    workspace_for(repository)
                )

            self.assertEqual(discovered_python, python)
            self.assertEqual(discovered, script)

    def test_iris_environment_tracks_the_active_python_version(self) -> None:
        source = Path("/source/esp-iris")
        selected = iris_environment_root(source)
        self.assertIn("esp-mosaico", str(selected))
        self.assertEqual(
            selected.name, f"py{sys.version_info.major}.{sys.version_info.minor}"
        )

    def test_iris_lock_change_reinstalls_into_the_active_environment(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            repository = Path(temporary)
            source = repository / "submodule" / "esp-iris"
            tools = source / "components" / "esp_iris" / "tools"
            tools.mkdir(parents=True)
            script = tools / "esp_iris.py"
            script.touch()
            lock = tools / "requirements.lock"
            lock.write_text('aiohttp==3.10.11 ; python_version < "3.10"\n')
            lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
            environment_root = iris_environment_root(source)
            python = virtual_environment_python(environment_root)
            python.parent.mkdir(parents=True)
            python.touch()
            marker = environment_root / ".mosaico-requirements"
            marker.write_text("stale\n", encoding="utf-8")
            context = mock.Mock(
                workspace=workspace_for(repository), repository=repository
            )
            context.run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway._python_major_minor",
                    return_value=(sys.version_info.major, sys.version_info.minor),
                )
            )
            selected_python, selected_script = ensure_iris_tools(context)
            marker_value = marker.read_text()
        self.assertEqual((selected_python, selected_script), (python, script))
        install = context.run.call_args.args[0]
        self.assertEqual(install[:4], [python, "-m", "pip", "install"])
        self.assertIn(lock_hash, marker_value)

    def test_unreachable_explicit_profile_never_starts_local_gateway(self) -> None:
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway.ensure_iris_tools",
                    return_value=(Path("python"), Path("iris")),
                )
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.gateway._probe", return_value=False)
            )
            start = _contexts.enter_context(
                mock.patch("mosaico_cli.gateway.subprocess.Popen")
            )
            with ExitStack() as _contexts:
                _contexts.enter_context(self.assertRaises(DeviceError))
                ensure_gateway(context, "remote")
        start.assert_not_called()

    def test_maintenance_lease_requires_local_gateway_and_suppresses_token_log(
        self,
    ) -> None:
        context = mock.Mock()
        context.run.side_effect = [
            subprocess.CompletedProcess(
                [],
                0,
                json.dumps(
                    {
                        "gateway_api": {"major": 1, "minor": 1},
                        "capabilities": ["device-maintenance-lease/v1"],
                    }
                ),
                "",
            ),
            subprocess.CompletedProcess(
                [],
                0,
                json.dumps(
                    {
                        "lease": {
                            "lease_id": "lease-1",
                            "token": "secret-token",
                            "endpoint": {"path": "/dev/serial/by-path/device"},
                        }
                    }
                ),
                "",
            ),
        ]
        local = GatewaySession(
            Path("python"),
            Path("iris"),
            ("--url", "http://127.0.0.1:8443"),
            None,
            False,
        )
        lease = acquire_maintenance_lease(
            context,
            local,
            device_id="device-a",
            expected_version="1.0.0-recovery",
            timeout=180,
        )
        self.assertEqual(lease["lease_id"], "lease-1")
        self.assertTrue(context.run.call_args.kwargs["sensitive_output"])
        remote = GatewaySession(Path("python"), Path("iris"), (), "remote", False)
        with ExitStack() as _contexts:
            _contexts.enter_context(self.assertRaises(DeviceError))
            acquire_maintenance_lease(
                context,
                remote,
                device_id="device-a",
                expected_version="1.0.0-recovery",
                timeout=180,
            )

    def test_endpoint_maintenance_lease_uses_local_gateway_and_suppresses_token(
        self,
    ) -> None:
        context = mock.Mock()
        context.run.side_effect = [
            subprocess.CompletedProcess(
                [],
                0,
                json.dumps(
                    {
                        "capabilities": [
                            "device-maintenance-lease/v1",
                            "physical-endpoint-maintenance-lease/v1",
                        ]
                    }
                ),
                "",
            ),
            subprocess.CompletedProcess(
                [],
                0,
                json.dumps(
                    {
                        "lease": {
                            "lease_id": "endpoint-lease",
                            "token": "secret-token",
                            "endpoint": {
                                "endpoint": "usb:location=1-1:1.0",
                                "path": "/dev/serial/by-path/device",
                            },
                        }
                    }
                ),
                "",
            ),
        ]
        local = GatewaySession(
            Path("python"),
            Path("iris"),
            ("--url", "http://127.0.0.1:8443"),
            None,
            False,
        )
        lease = acquire_endpoint_maintenance_lease(
            context,
            local,
            endpoint="/dev/serial/by-path/device",
            expected_version="2.1.1-recovery",
            timeout=180,
        )
        self.assertEqual(lease["lease_id"], "endpoint-lease")
        acquire_call = context.run.call_args_list[1]
        self.assertIn("maintenance-acquire-endpoint", acquire_call.args[0])
        self.assertTrue(acquire_call.kwargs["sensitive_output"])

    def test_wrong_revision_local_gateway_is_not_stopped_or_replaced(self) -> None:
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway.ensure_iris_tools",
                    return_value=(Path("python"), Path("iris")),
                )
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.gateway._probe", return_value=True)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway._gateway_health",
                    return_value={
                        "status": "ok",
                        "instance_id": "older",
                        "gateway_api": {"major": 1, "minor": 1},
                        "capabilities": ["device-maintenance-lease/v1"],
                        "esp_iris_revision": "wrong-revision",
                    },
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway._pinned_source_revision",
                    return_value="pinned-revision",
                )
            )
            start = _contexts.enter_context(
                mock.patch("mosaico_cli.gateway.subprocess.Popen")
            )
            _contexts.enter_context(self.assertRaises(EnvironmentError))
            ensure_gateway(context, None)
        start.assert_not_called()

    def test_unusable_running_local_gateway_is_not_replaced(self) -> None:
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway.ensure_iris_tools",
                    return_value=(Path("python"), Path("iris")),
                )
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.gateway._probe", return_value=False)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway._pinned_source_revision",
                    return_value="pinned-revision",
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.gateway._health_instance", return_value="existing"
                )
            )
            start = _contexts.enter_context(
                mock.patch("mosaico_cli.gateway.subprocess.Popen")
            )
            _contexts.enter_context(self.assertRaises(DeviceError))
            ensure_gateway(context, None)
        start.assert_not_called()

    def test_unknown_ota_result_is_not_replayed(self) -> None:
        context = mock.Mock()
        context.log_path = Path("run.log")
        context.run.return_value = subprocess.CompletedProcess(
            [], 1, json.dumps({"operation": {"status": "outcome_unknown"}}), ""
        )
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        with ExitStack() as _contexts:
            _contexts.enter_context(self.assertRaises(OutcomeUnknownError))
            run_ota(
                context,
                session,
                device_id="device",
                image=Path("app.bin"),
                elf=Path("app.elf"),
                map_file=Path("app.map"),
                validation="elf-sha256",
                timeout=600,
            )
        self.assertEqual(context.run.call_count, 1)

    def test_known_ota_failure_is_operation_error(self) -> None:
        context = mock.Mock()
        context.log_path = Path("run.log")
        context.run.return_value = subprocess.CompletedProcess(
            [], 1, json.dumps({"operation": {"status": "failed"}}), ""
        )
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        with ExitStack() as _contexts:
            _contexts.enter_context(self.assertRaises(OperationError))
            run_ota(
                context,
                session,
                device_id="device",
                image=Path("app.bin"),
                elf=Path("app.elf"),
                map_file=Path("app.map"),
                validation="version",
                timeout=600,
            )

    def test_ota_progress_is_polled_and_reported(self) -> None:
        context = mock.Mock()
        context.log_path = Path("run.log")
        context.run.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "operation": {
                        "operation_id": "operation-1",
                        "status": "running",
                        "progress": {
                            "stage": "waiting_recovery",
                            "progress_permille": 50,
                        },
                    }
                }
            ),
            "",
        )
        completed = {
            "operation": {
                "operation_id": "operation-1",
                "status": "succeeded",
                "progress": {
                    "stage": "succeeded",
                    "progress_permille": 1000,
                },
            }
        }
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        with ExitStack() as _contexts:
            poll = _contexts.enter_context(
                mock.patch("mosaico_cli.gateway.gateway_json", return_value=completed)
            )
            _contexts.enter_context(mock.patch("mosaico_cli.gateway.time.sleep"))
            result = run_ota(
                context,
                session,
                device_id="device",
                image=Path("app.bin"),
                elf=Path("app.elf"),
                map_file=Path("app.map"),
                validation="elf-sha256",
                timeout=600,
            )
        self.assertEqual(result["operation"]["status"], "succeeded")
        ota_argv = context.run.call_args.args[0]
        self.assertIn("--execution-mode", ota_argv)
        self.assertNotIn("--validation-mode", ota_argv)
        poll.assert_called_once_with(context, session, "ota-status", "operation-1")
        messages = [call.args[0] for call in context.status.call_args_list]
        self.assertTrue(any("waiting_recovery" in message for message in messages))
        self.assertTrue(any("succeeded" in message for message in messages))

    def test_system_update_bundle_is_submitted_and_polled(self) -> None:
        context = mock.Mock(log_path=Path("run.log"))
        context.run.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "operation": {
                        "operation_id": "system-operation-1",
                        "status": "running",
                        "progress": {
                            "stage": "validating_plan",
                            "progress_permille": 50,
                        },
                    }
                }
            ),
            "",
        )
        completed = {
            "operation": {
                "operation_id": "system-operation-1",
                "status": "succeeded",
                "progress": {
                    "stage": "succeeded",
                    "progress_permille": 1000,
                },
            }
        }
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        with ExitStack() as _contexts:
            poll = _contexts.enter_context(
                mock.patch("mosaico_cli.gateway.gateway_json", return_value=completed)
            )
            _contexts.enter_context(mock.patch("mosaico_cli.gateway.time.sleep"))
            result = run_system_update_bundle(
                context,
                session,
                device_id="device",
                bundle=Path("release.irisfw"),
                timeout=900,
            )

        self.assertEqual(result["operation"]["status"], "succeeded")
        argv = context.run.call_args.args[0]
        self.assertIn("system-update", argv)
        self.assertIn("release.irisfw", argv)
        poll.assert_called_once_with(
            context, session, "ota-status", "system-operation-1"
        )
        messages = [call.args[0] for call in context.status.call_args_list]
        self.assertTrue(any("validating_plan" in message for message in messages))

    def test_system_update_failure_exposes_gateway_error(self) -> None:
        context = mock.Mock(log_path=Path("/runs/raw.log"))
        context.run.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "operation": {
                        "operation_id": "system-operation-1",
                        "status": "running",
                    }
                }
            ),
            "",
        )
        failed = {
            "operation": {
                "operation_id": "system-operation-1",
                "status": "failed",
                "error": "device partition-table SHA-256 is not authorized by the bundle",
            }
        }
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        with ExitStack() as contexts:
            contexts.enter_context(
                mock.patch("mosaico_cli.gateway.gateway_json", return_value=failed)
            )
            contexts.enter_context(mock.patch("mosaico_cli.gateway.time.sleep"))
            caught = contexts.enter_context(self.assertRaises(OperationError))
            run_system_update_bundle(
                context,
                session,
                device_id="device",
                bundle=Path("release.irisfw"),
                timeout=900,
            )

        self.assertIn("partition-table SHA-256", str(caught.exception))
        self.assertEqual(
            caught.exception.details["diagnostic"],
            "device partition-table SHA-256 is not authorized by the bundle",
        )

    def test_monitor_forces_unbuffered_child_output(self) -> None:
        arguments = argparse.Namespace(
            gateway_profile=None,
            device_id=None,
            snapshot=True,
            timeout=1,
            grep=None,
        )
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        process = mock.Mock()
        process.stdout = io.StringIO("")
        process.poll.return_value = 0
        process.wait.return_value = 0
        try:
            with ExitStack() as _contexts:
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.ensure_gateway", return_value=session
                    )
                )
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.connected_devices",
                        return_value=[{"device_id": "device"}],
                    )
                )
                start = _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.subprocess.Popen", return_value=process
                    )
                )
                self.assertEqual(monitor(arguments, context, False), 0)
            self.assertEqual(start.call_args.kwargs["env"]["PYTHONUNBUFFERED"], "1")
            self.assertIn("--json", start.call_args.args[0])
        finally:
            process.stdout.close()

    def test_monitor_removes_transport_blank_lines(self) -> None:
        arguments = argparse.Namespace(
            gateway_profile=None,
            device_id=None,
            snapshot=True,
            timeout=1,
            grep=None,
            force_color=False,
            disable_auto_color=True,
        )
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        session = GatewaySession(Path("python"), Path("iris"), (), None, False)
        payload = "".join(
            json.dumps({"text": text}, separators=(",", ":")) + "\n"
            for text in ("I (123) demo: first\r\n", "W (124) demo: second\r\n")
        )
        process = mock.Mock()
        process.stdout = io.StringIO(payload)
        process.poll.return_value = 0
        process.wait.return_value = 0
        output = io.StringIO()
        try:
            with ExitStack() as _contexts:
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.ensure_gateway", return_value=session
                    )
                )
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.connected_devices",
                        return_value=[{"device_id": "device"}],
                    )
                )
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.subprocess.Popen", return_value=process
                    )
                )
                _contexts.enter_context(redirect_stdout(output))
                self.assertEqual(monitor(arguments, context, False), 0)
        finally:
            process.stdout.close()
        self.assertEqual(
            output.getvalue(),
            "I (123) demo: first\r\nW (124) demo: second\r\n",
        )


class MonitorRenderingTests(unittest.TestCase):
    def test_fragmented_crlf_is_reassembled_without_extra_newline(self) -> None:
        output = io.StringIO()
        renderer = _MonitorTextRenderer(output, grep=None, color_enabled=False)
        renderer.feed("I (123) demo: hel")
        renderer.feed("lo\r")
        renderer.feed("\n\r\n")
        renderer.finish()
        self.assertEqual(output.getvalue(), "I (123) demo: hello\r\n\r\n")

    def test_colors_match_idf_monitor(self) -> None:
        output = io.StringIO()
        renderer = _MonitorTextRenderer(output, grep=None, color_enabled=True)
        renderer.feed(
            "I (1) tag: info\nW (2) tag: warning\nE (3) tag: error\nD (4) tag: debug\n"
        )
        renderer.finish()
        self.assertEqual(
            output.getvalue(),
            "\033[0;32mI (1) tag: info\033[0m\n"
            "\033[0;33mW (2) tag: warning\033[0m\n"
            "\033[1;31mE (3) tag: error\033[0m\n"
            "D (4) tag: debug\n",
        )


class HostCompatibilityTests(unittest.TestCase):
    def test_platform_state_directories(self) -> None:
        home = Path("/users/example")
        self.assertEqual(
            state_root(
                "esp-mosaico",
                environment={"LOCALAPPDATA": "C:/Users/example/AppData/Local"},
                home=home,
                os_name="nt",
                sys_platform="win32",
            ),
            Path("C:/Users/example/AppData/Local/esp-mosaico"),
        )
        self.assertEqual(
            state_root(
                "esp-mosaico",
                environment={},
                home=home,
                os_name="posix",
                sys_platform="darwin",
            ),
            home / "Library" / "Application Support" / "esp-mosaico",
        )
        self.assertEqual(
            state_root(
                "esp-mosaico",
                environment={"XDG_STATE_HOME": "/state"},
                home=home,
                os_name="posix",
                sys_platform="linux",
            ),
            Path("/state/esp-mosaico"),
        )

    def test_virtual_environment_python_layouts(self) -> None:
        root = Path("C:/workspace/.venv")
        self.assertEqual(
            virtual_environment_python(root, os_name="nt"),
            root / "Scripts" / "python.exe",
        )
        self.assertEqual(
            virtual_environment_python(root, os_name="posix"), root / "bin" / "python"
        )

    def test_idf_key_value_export_preserves_inherited_path(self) -> None:
        self.assertEqual(
            _parse_idf_exports(
                "IDF_PYTHON_ENV_PATH=C:/ESP Python\nPATH=C:/tools;%PATH%\n",
                {"PATH": "C:/Windows/System32"},
            ),
            {
                "IDF_PYTHON_ENV_PATH": "C:/ESP Python",
                "PATH": "C:/tools;C:/Windows/System32",
            },
        )

    def test_idf_environment_uses_idf_python_without_a_shell(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            idf = Path(temporary) / "ESP IDF"
            (idf / "tools").mkdir(parents=True)
            (idf / "tools" / "idf.py").touch()
            (idf / "tools" / "idf_tools.py").touch()
            python_env = Path(temporary) / "Python Env"
            python = virtual_environment_python(python_env)
            python.parent.mkdir(parents=True)
            python.touch()
            exported = f"IDF_PYTHON_ENV_PATH={python_env}\nPATH={idf / 'tools'}:$PATH\n"
            probe = subprocess.CompletedProcess(
                [], 0, '["/bootstrap/python", 3, 10]\n', ""
            )
            completed = subprocess.CompletedProcess([], 0, exported, "")
            idf_python_probe = subprocess.CompletedProcess(
                [], 0, f'["{python}", 3, 12]\n', ""
            )
            with ExitStack() as _contexts:
                run = _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.host.subprocess.run",
                        side_effect=[probe, completed, idf_python_probe],
                    )
                )
                prepared = prepare_idf_environment(
                    idf,
                    base_environment={"PATH": "/usr/bin"},
                    bootstrap_python=Path("/bootstrap/python"),
                )
            self.assertEqual(prepared.python, python)
            self.assertEqual(prepared.values["PATH"], f"{idf / 'tools'}:/usr/bin")
            argv = run.call_args_list[1].args[0]
            self.assertEqual(argv[0], str(Path("/bootstrap/python")))
            self.assertNotIn("bash", argv)

    def test_python38_runtime_hands_idf_to_a_newer_python(self) -> None:
        active = Path("/idf-python")
        with ExitStack() as _contexts:
            probe = _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.host._probe_python",
                    return_value=(active, (3, 12)),
                )
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.host.sys.executable", "/python38")
            )
            selected = resolve_idf_bootstrap_python(
                Path("/idf"),
                {
                    "IDF_PYTHON_ENV_PATH": "/idf-env",
                    "PATH": "",
                },
            )
        self.assertEqual(selected, active)
        self.assertEqual(probe.call_args_list[0].args[0], ["/idf-env/bin/python"])

    def test_explicit_idf_python_environment_takes_priority(self) -> None:
        configured = Path("/configured/python")
        with mock.patch(
            "mosaico_cli.host._probe_python",
            return_value=(configured, (3, 11)),
        ) as probe:
            selected = resolve_idf_bootstrap_python(
                Path("/idf"),
                {
                    "MOSAICO_IDF_PYTHON": str(configured),
                    "IDF_PYTHON_ENV_PATH": "/active-idf-env",
                    "PATH": "",
                },
            )
        self.assertEqual(selected, configured)
        probe.assert_called_once_with([str(configured)], mock.ANY)

    def test_exported_idf_environment_rejects_python38(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            idf = Path(temporary) / "idf"
            (idf / "tools").mkdir(parents=True)
            (idf / "tools" / "idf.py").touch()
            (idf / "tools" / "idf_tools.py").touch()
            python_env = Path(temporary) / "idf-python"
            python = virtual_environment_python(python_env)
            python.parent.mkdir(parents=True)
            python.touch()
            exported = f"IDF_PYTHON_ENV_PATH={python_env}\nPATH=/idf/bin:$PATH\n"
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.host._probe_python",
                    side_effect=[
                        (Path("/bootstrap-python"), (3, 12)),
                        (python, (3, 8)),
                    ],
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.host.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, exported, ""),
                )
            )
            caught = _contexts.enter_context(self.assertRaises(HostEnvironmentError))
            prepare_idf_environment(
                idf,
                base_environment={"PATH": "/usr/bin"},
                bootstrap_python=Path("/bootstrap-python"),
            )
        self.assertIn("selected ESP-IDF environment", str(caught.exception))
        self.assertIn("Python 3.10", str(caught.exception))

    def test_missing_idf_python_reports_the_separate_requirement(self) -> None:
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.host._probe_python", return_value=None)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.host.shutil.which", return_value=None)
            )
            caught = _contexts.enter_context(self.assertRaises(HostEnvironmentError))
            resolve_idf_bootstrap_python(Path("/idf"), {"PATH": ""})
        self.assertIn("mosaico.py supports Python 3.8", str(caught.exception))
        self.assertIn("ESP-IDF", str(caught.exception))


class ProjectTests(unittest.TestCase):
    def _project(self, root: Path, name: str) -> Path:
        path = root / "projects" / name
        path.mkdir(parents=True)
        (path / "CMakeLists.txt").write_text("project(example)\n", encoding="utf-8")
        return path

    def test_single_user_project_is_selected(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            expected = self._project(root, "demo")
            self._project(root, "factory")
            self.assertEqual(resolve_project(workspace_for(root), None, root), expected)

    def test_factory_is_not_implicitly_selected(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            self._project(root, "factory")
            with ExitStack() as _contexts:
                _contexts.enter_context(self.assertRaises(SelectionError))
                resolve_project(workspace_for(root), None, root)

    def test_factory_cannot_be_selected_explicitly(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            factory = self._project(root, "factory")
            with ExitStack() as _contexts:
                caught = _contexts.enter_context(self.assertRaises(SelectionError))
                resolve_project(workspace_for(root), str(factory), root)
            self.assertIn("Recovery firmware only", str(caught.exception))

    def test_factory_cannot_be_selected_from_its_working_directory(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            factory = self._project(root, "factory")
            with ExitStack() as _contexts:
                caught = _contexts.enter_context(self.assertRaises(SelectionError))
                resolve_project(workspace_for(root), None, factory)
            self.assertIn("Recovery firmware only", str(caught.exception))

    def test_multiple_projects_require_selection(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            self._project(root, "a")
            self._project(root, "b")
            with ExitStack() as _contexts:
                caught = _contexts.enter_context(self.assertRaises(SelectionError))
                resolve_project(workspace_for(root), None, root)
            self.assertEqual(len(caught.exception.details["candidates"]), 2)

    def test_partition_table_hash_covers_erased_flash_sector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "partition-table.bin"
            path.write_bytes(b"partition-table")
            expected = hashlib.sha256(
                b"partition-table" + b"\xff" * (0x1000 - len(b"partition-table"))
            ).hexdigest()
            self.assertEqual(partition_table_flash_sha256(path), expected)

    def test_partition_table_larger_than_flash_sector_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "partition-table.bin"
            path.write_bytes(b"x" * (0x1000 + 1))
            with self.assertRaises(BuildError):
                partition_table_flash_sha256(path)


class RecoveryBundleTests(unittest.TestCase):
    def test_primary_verification_record_is_host_global(self) -> None:
        path = _verification_path("device")
        self.assertEqual(path, _host_verification_path("device"))
        self.assertIn("esp-mosaico", str(path))

    def test_reviewed_bundle_hashes(self) -> None:
        model = select_model(WORKSPACE, None)
        with tempfile.TemporaryDirectory() as temporary:
            directory = create_recovery_bundle(Path(temporary) / "recovery")
            value = load_bundle(directory, model.target)
        self.assertEqual(value["schema_version"], 2)
        self.assertEqual(
            set(value["images"]),
            {"bootloader", "partition_table", "ota_data", "recovery"},
        )

    def test_corrupt_bundle_is_rejected(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            target = create_recovery_bundle(Path(temporary) / "recovery")
            with ExitStack() as _contexts:
                stream = _contexts.enter_context(
                    (target / "ota_data_initial.bin").open("ab")
                )
                stream.write(b"corrupt")
            with ExitStack() as _contexts:
                _contexts.enter_context(self.assertRaises(EnvironmentError))
                load_bundle(target, "esp32s31")

    def test_manifest_hashes_match_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = create_recovery_bundle(Path(temporary) / "recovery")
            value = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
            for item in value["images"].values():
                digest = hashlib.sha256(
                    (directory / item["file"]).read_bytes()
                ).hexdigest()
                self.assertEqual(digest, item["sha256"])

    def test_live_recovery_must_match_reviewed_version(self) -> None:
        status = {
            "firmware_mode": "recovery",
            "app_version": "2.0.0-recovery",
            "capability_names": ["ota"],
        }
        self.assertFalse(recovery_is_verified("device", status, "2.1.0-recovery"))
        status["app_version"] = "2.1.0-recovery"
        self.assertTrue(recovery_is_verified("device", status, "2.1.0-recovery"))

    def test_verification_record_read_retries_a_windows_sharing_error(self) -> None:
        record = {
            "device_id": "device",
            "recovery_version": "2.1.1-recovery",
        }
        with ExitStack() as _contexts:
            read_text = _contexts.enter_context(
                mock.patch.object(
                    Path,
                    "read_text",
                    side_effect=[
                        PermissionError("sharing violation"),
                        json.dumps(record),
                    ],
                )
            )
            sleep = _contexts.enter_context(
                mock.patch("mosaico_cli.recovery.time.sleep")
            )
            self.assertEqual(_read_verification_record(Path("record.json")), record)
        self.assertEqual(read_text.call_count, 2)
        sleep.assert_called_once()

    def test_verification_details_report_a_persistent_read_error(self) -> None:
        diagnostics: list[dict[str, object]] = []
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch.object(
                    Path, "read_text", side_effect=PermissionError("access denied")
                )
            )
            _contexts.enter_context(mock.patch("mosaico_cli.recovery.time.sleep"))
            self.assertIsNone(
                _read_verification_record(Path("record.json"), diagnostics)
            )
        self.assertEqual(diagnostics[0]["state"], "read_failed")
        self.assertIn("PermissionError", str(diagnostics[0]["error"]))

    def test_verification_details_report_live_recovery_mismatch(self) -> None:
        verified, details = recovery_verification_details(
            "device",
            {
                "firmware_mode": "recovery",
                "app_version": "old-recovery",
                "capability_names": [],
            },
            "2.1.1-recovery",
        )
        self.assertFalse(verified)
        self.assertEqual(details["source"], "live_recovery")
        self.assertEqual(details["actual_version"], "old-recovery")


class RecoveryCommandTests(unittest.TestCase):
    def test_non_mapping_gateway_status_normalizes_for_host_verification(self) -> None:
        self.assertEqual(_device_status(None), {})
        self.assertEqual(_device_status([]), {})
        self.assertEqual(_device_status({"device": None}), {})
        self.assertEqual(
            _device_status({"firmware_mode": "normal"}), {"firmware_mode": "normal"}
        )

    def test_normal_device_snapshot_overrides_stale_recovery_status(self) -> None:
        value = _recovery_verification_status(
            {"firmware_mode": "normal", "boot_id": 2},
            {
                "firmware_mode": "recovery",
                "app_version": "old-recovery",
                "capability_names": [],
                "boot_id": 1,
            },
        )
        self.assertEqual(value["firmware_mode"], "normal")

    def test_recovery_device_snapshot_keeps_detailed_live_status(self) -> None:
        status = {
            "firmware_mode": "recovery",
            "app_version": "2.1.1-recovery",
            "capability_names": ["ota"],
        }
        self.assertEqual(
            _recovery_verification_status({"firmware_mode": "recovery"}, status),
            status,
        )

    def test_install_refreshes_record_from_verified_live_recovery(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            image = project / "app.bin"
            image.write_bytes(b"firmware")
            partition_table = project / "partition-table.bin"
            partition_table.write_bytes(b"partition-table")
            layout_sha256 = partition_table_flash_sha256(partition_table)
            artifacts = SimpleNamespace(
                image=image,
                elf=project / "app.elf",
                map_file=project / "app.map",
                partition_table=partition_table,
                project_name="app",
                project_version="1.0.0",
                target="esp32s31",
            )
            arguments = SimpleNamespace(
                project=str(project),
                skip_build=True,
                gateway_profile=None,
                device_id=None,
                validation="elf-sha256",
                timeout=30,
            )
            context = RunContext(workspace_for(root), "install-test", json_output=True)
            session = SimpleNamespace(started_local=False)
            status = {
                "device_id": "device",
                "firmware_mode": "recovery",
                "app_version": "2.1.1-recovery",
                "capability_names": ["ota"],
                "boot_id": 123,
            }
            with ExitStack() as _contexts:
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.resolve_project", return_value=project
                    )
                )
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.discover_artifacts",
                        return_value=artifacts,
                    )
                )
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.load_bundle",
                        return_value={"version": "2.1.1-recovery"},
                    )
                )
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.ensure_gateway", return_value=session
                    )
                )
                _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.connected_devices", return_value=[status]
                    )
                )
                _contexts.enter_context(
                    mock.patch("mosaico_cli.commands.gateway_json", return_value=status)
                )
                inventory = _contexts.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.system_inventory",
                        return_value={"partition_table_sha256": layout_sha256},
                    )
                )
                ota = _contexts.enter_context(
                    mock.patch("mosaico_cli.commands.run_ota", return_value={})
                )
                record = _contexts.enter_context(
                    mock.patch("mosaico_cli.commands.record_recovery_verification")
                )
                result = install(arguments, context)
            self.assertEqual(result["status"], "succeeded")
            record.assert_called_once_with("device", "2.1.1-recovery", 123)
            inventory.assert_called_once_with(context, session, "device")
            ota.assert_called_once()

    def test_install_enters_recovery_before_partition_preflight(self) -> None:
        with ExitStack() as contexts:
            temporary = contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            image = project / "app.bin"
            image.write_bytes(b"firmware")
            partition_table = project / "partition-table.bin"
            partition_table.write_bytes(b"partition-table")
            layout_sha256 = partition_table_flash_sha256(partition_table)
            artifacts = SimpleNamespace(
                image=image,
                elf=project / "app.elf",
                map_file=project / "app.map",
                partition_table=partition_table,
                project_name="app",
                project_version="1.0.0",
                target="esp32s31",
            )
            arguments = SimpleNamespace(
                project=str(project),
                skip_build=True,
                gateway_profile=None,
                device_id=None,
                validation="elf-sha256",
                timeout=30,
            )
            context = RunContext(
                workspace_for(root), "normal-install-test", json_output=True
            )
            session = SimpleNamespace(started_local=False)
            normal = {
                "device_id": "device",
                "firmware_mode": "normal",
                "boot_id": 10,
            }
            recovery = {
                "device_id": "device",
                "firmware_mode": "recovery",
                "app_version": "2.1.1-recovery",
                "capability_names": ["ota"],
                "boot_id": 11,
            }
            order: list[str] = []
            with ExitStack() as patches:
                patches.enter_context(
                    mock.patch("mosaico_cli.commands.resolve_project", return_value=project)
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.discover_artifacts", return_value=artifacts
                    )
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.load_bundle",
                        return_value={"version": "2.1.1-recovery"},
                    )
                )
                patches.enter_context(
                    mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
                )
                patches.enter_context(
                    mock.patch("mosaico_cli.commands.connected_devices", return_value=[normal])
                )
                patches.enter_context(
                    mock.patch("mosaico_cli.commands.gateway_json", return_value=normal)
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.recovery_verification_details",
                        side_effect=[
                            (True, {"source": "persistent_record"}),
                            (True, {"source": "live_recovery"}),
                        ],
                    )
                )
                enter = patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.enter_recovery_and_wait",
                        side_effect=lambda *args, **kwargs: (
                            order.append("recovery") or recovery
                        ),
                    )
                )
                inventory = patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.system_inventory",
                        side_effect=lambda *args, **kwargs: (
                            order.append("inventory")
                            or {"partition_table_sha256": layout_sha256}
                        ),
                    )
                )
                ota = patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.run_ota",
                        side_effect=lambda *args, **kwargs: order.append("ota") or {},
                    )
                )
                record = patches.enter_context(
                    mock.patch("mosaico_cli.commands.record_recovery_verification")
                )
                result = install(arguments, context)

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(order, ["recovery", "inventory", "ota"])
            enter.assert_called_once_with(
                context,
                session,
                "device",
                previous_boot_id=10,
                timeout=30,
            )
            inventory.assert_called_once_with(context, session, "device")
            ota.assert_called_once()
            record.assert_called_once_with("device", "2.1.1-recovery", 11)

    def test_install_rejects_mismatched_partition_table_before_ota(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            image = project / "app.bin"
            image.write_bytes(b"firmware")
            partition_table = project / "partition-table.bin"
            partition_table.write_bytes(b"target-layout")
            artifacts = SimpleNamespace(
                image=image,
                elf=project / "app.elf",
                map_file=project / "app.map",
                partition_table=partition_table,
                project_name="app",
                project_version="1.0.0",
                target="esp32s31",
            )
            arguments = SimpleNamespace(
                project=str(project),
                skip_build=True,
                gateway_profile=None,
                device_id=None,
                validation="elf-sha256",
                timeout=30,
            )
            context = RunContext(
                workspace_for(root), "install-layout-test", json_output=True
            )
            session = SimpleNamespace(started_local=False)
            status = {
                "device_id": "device",
                "firmware_mode": "recovery",
                "app_version": "2.1.1-recovery",
                "capability_names": ["ota"],
                "boot_id": 123,
            }
            with ExitStack() as patches:
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.resolve_project", return_value=project
                    )
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.discover_artifacts",
                        return_value=artifacts,
                    )
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.load_bundle",
                        return_value={"version": "2.1.1-recovery"},
                    )
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.ensure_gateway", return_value=session
                    )
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.connected_devices",
                        return_value=[status],
                    )
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.gateway_json", return_value=status
                    )
                )
                patches.enter_context(
                    mock.patch(
                        "mosaico_cli.commands.system_inventory",
                        return_value={"partition_table_sha256": "00" * 32},
                    )
                )
                ota = patches.enter_context(
                    mock.patch("mosaico_cli.commands.run_ota")
                )
                patches.enter_context(
                    mock.patch("mosaico_cli.commands.record_recovery_verification")
                )
                caught = patches.enter_context(self.assertRaises(DeviceError))
                install(arguments, context)

            self.assertIn("does not match", str(caught.exception))
            self.assertEqual(
                caught.exception.details["actual_partition_table_sha256"],
                "00" * 32,
            )
            ota.assert_not_called()

    def test_registered_esp32s31_recovery_device_is_detected(self) -> None:
        port = SimpleNamespace(
            device="/dev/ttyACM-test",
            vid=0x303A,
            pid=0x0020,
            product="ESP32-S31",
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("serial.tools.list_ports.comports", return_value=[port])
            )
            self.assertEqual(
                _registered_recovery_ports(select_model(WORKSPACE, None)), ["/dev/ttyACM-test"]
            )

    def test_registered_recovery_device_without_product_is_detected(self) -> None:
        port = SimpleNamespace(
            device="COM16",
            vid=0x303A,
            pid=0x0020,
            product=None,
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("serial.tools.list_ports.comports", return_value=[port])
            )
            self.assertEqual(_registered_recovery_ports(select_model(WORKSPACE, None)), ["COM16"])

    def test_unregistered_serial_device_is_rejected(self) -> None:
        port = SimpleNamespace(
            device="/dev/ttyACM9",
            vid=0x1234,
            pid=0x5678,
            product="Other device",
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("serial.tools.list_ports.comports", return_value=[port])
            )
            self.assertEqual(_registered_recovery_ports(select_model(WORKSPACE, None)), [])

    def test_registered_recovery_is_preferred_over_debug_serial(self) -> None:
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        context.run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "devices": [
                        {"path": "/dev/debug", "transport": "usb_serial_jtag"},
                        {"path": "/dev/rom", "transport": "rom"},
                    ]
                }
            ),
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.recovery.locate_iris_tools",
                    return_value=(Path("python"), Path("esp_iris.py")),
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.recovery._registered_recovery_ports",
                    return_value=["/dev/rom"],
                )
            )
            self.assertEqual(
                provisioning_candidate(context, select_model(WORKSPACE, None)), "/dev/rom"
            )

    def test_current_dry_run_does_not_build_or_flash(self) -> None:
        arguments = argparse.Namespace(
            model=None,
            source="current",
            device_id=None,
            gateway_profile=None,
            timeout=180,
            dry_run=True,
            yes=False,
        )
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        context.log_path = REPOSITORY / ".codex-runs" / "test.log"
        context.note = mock.Mock()
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.ensure_gateway",
                    side_effect=DeviceError("offline"),
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.provisioning_candidate",
                    return_value="internal-device",
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.resolve_idf_path", return_value=Path("/idf")
                )
            )
            target = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.run_idf_target")
            )
            result = recover(arguments, context)
        self.assertEqual(result["status"], "dry_run")
        target.assert_not_called()
        messages = [call.args[0] for call in context.status.call_args_list]
        self.assertTrue(
            any(message.startswith("recovery: model=") for message in messages)
        )
        self.assertTrue(
            any(
                message.startswith("device: recovery interface") for message in messages
            )
        )
        self.assertTrue(any(message.startswith("validation:") for message in messages))

    def test_managed_recovery_uses_device_lease_without_stopping_gateway(self) -> None:
        arguments = SimpleNamespace(
            model=None,
            source="reviewed",
            device_id="device-a",
            gateway_profile=None,
            timeout=180,
            dry_run=False,
        )
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        context.log_path = REPOSITORY / ".codex-runs" / "test.log"
        session = GatewaySession(
            Path("python"),
            Path("iris"),
            ("--url", "http://127.0.0.1:8443"),
            None,
            False,
        )
        manifest = {"version": "2.1.1-recovery", "images": {"recovery": {}}}
        verification = {
            "device_id": "device-a",
            "boot_id": "boot-new",
            "firmware_mode": "recovery",
            "app_version": "2.1.1-recovery",
            "capability_names": ["ota"],
        }
        lease = {
            "lease_id": "lease-1",
            "token": "secret",
            "endpoint": {"path": "/dev/serial/by-path/device-a"},
        }
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.load_bundle", return_value=manifest)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.connected_devices",
                    return_value=[{"device_id": "device-a", "boot_id": "boot-old"}],
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.resolve_idf_path", return_value=Path("/idf")
                )
            )
            target = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.run_idf_target")
            )
            acquire = _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.acquire_maintenance_lease", return_value=lease
                )
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.renew_maintenance_lease")
            )
            finish = _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.finish_maintenance_lease",
                    return_value={
                        "state": "released",
                        "evidence": {"verification": verification},
                    },
                )
            )
            record = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.record_recovery_verification")
            )
            result = recover(arguments, context)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["maintenance_lease_id"], "lease-1")
        self.assertEqual(
            [call.kwargs["target"] for call in target.call_args_list],
            ["mosaico-recover-prepare", "mosaico-recover-flash"],
        )
        self.assertEqual(
            target.call_args_list[0].kwargs["definitions"],
            {"MOSAICO_RECOVERY_SOURCE": "reviewed"},
        )
        self.assertEqual(
            target.call_args_list[1].kwargs["definitions"],
            {"MOSAICO_RECOVERY_SOURCE": "reviewed"},
        )
        self.assertEqual(
            target.call_args_list[1].kwargs["port"], "/dev/serial/by-path/device-a"
        )
        acquire.assert_called_once()
        finish.assert_called_once_with(
            context, session, lease, abort=False, timeout=180
        )
        record.assert_called_once_with("device-a", "2.1.1-recovery", "boot-new")

    def test_unmanaged_recovery_leases_physical_endpoint_before_flash(self) -> None:
        arguments = SimpleNamespace(
            model=None,
            source="reviewed",
            device_id=None,
            gateway_profile=None,
            timeout=180,
            dry_run=False,
        )
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        context.log_path = REPOSITORY / ".codex-runs" / "test.log"
        session = GatewaySession(
            Path("python"),
            Path("iris"),
            ("--url", "http://127.0.0.1:8443"),
            None,
            False,
        )
        manifest = {"version": "2.1.1-recovery", "images": {"recovery": {}}}
        lease = {
            "lease_id": "endpoint-lease",
            "token": "secret",
            "endpoint": {"path": "/dev/serial/by-path/recovery"},
        }
        verification = {
            "device_id": "device-after-recovery",
            "boot_id": "boot-new",
            "firmware_mode": "recovery",
            "app_version": "2.1.1-recovery",
            "capability_names": ["ota"],
        }
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.load_bundle", return_value=manifest)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.connected_devices", return_value=[])
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.provisioning_candidate",
                    return_value="/dev/serial/by-path/recovery",
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.resolve_idf_path", return_value=Path("/idf")
                )
            )
            target = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.run_idf_target")
            )
            acquire = _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.acquire_endpoint_maintenance_lease",
                    return_value=lease,
                )
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.renew_maintenance_lease")
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.finish_maintenance_lease",
                    return_value={
                        "state": "released",
                        "evidence": {"verification": verification},
                    },
                )
            )
            record = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.record_recovery_verification")
            )
            result = recover(arguments, context)
        acquire.assert_called_once_with(
            context,
            session,
            endpoint="/dev/serial/by-path/recovery",
            expected_version="2.1.1-recovery",
            timeout=180,
        )
        self.assertEqual(
            target.call_args_list[1].kwargs["port"], lease["endpoint"]["path"]
        )
        self.assertEqual(result["device_id"], "device-after-recovery")
        record.assert_called_once_with(
            "device-after-recovery", "2.1.1-recovery", "boot-new"
        )

    def test_remote_recovery_is_rejected_before_gateway_or_flash(self) -> None:
        arguments = SimpleNamespace(gateway_profile="remote")
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        with ExitStack() as _contexts:
            gateway = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway")
            )
            target = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.run_idf_target")
            )
            _contexts.enter_context(self.assertRaises(DeviceError))
            recover(arguments, context)
        gateway.assert_not_called()
        target.assert_not_called()

    def test_failed_flash_aborts_device_maintenance_lease(self) -> None:
        arguments = SimpleNamespace(
            model=None,
            source="reviewed",
            device_id="device-a",
            gateway_profile=None,
            timeout=180,
            dry_run=False,
        )
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        context.log_path = REPOSITORY / ".codex-runs" / "test.log"
        session = GatewaySession(
            Path("python"),
            Path("iris"),
            ("--url", "http://127.0.0.1:8443"),
            None,
            False,
        )
        manifest = {"version": "2.1.1-recovery", "images": {"recovery": {}}}
        lease = {
            "lease_id": "lease-1",
            "token": "secret",
            "endpoint": {"path": "/dev/serial/by-path/device-a"},
        }
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.load_bundle", return_value=manifest)
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.ensure_gateway", return_value=session)
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.connected_devices",
                    return_value=[{"device_id": "device-a", "boot_id": "boot-old"}],
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.resolve_idf_path", return_value=Path("/idf")
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.run_idf_target",
                    side_effect=[None, BuildError("flash failed")],
                )
            )
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.commands.acquire_maintenance_lease", return_value=lease
                )
            )
            _contexts.enter_context(
                mock.patch("mosaico_cli.commands.renew_maintenance_lease")
            )
            finish = _contexts.enter_context(
                mock.patch("mosaico_cli.commands.finish_maintenance_lease")
            )
            _contexts.enter_context(self.assertRaises(BuildError))
            recover(arguments, context)
        finish.assert_called_once_with(context, session, lease, abort=True, timeout=30)

    def test_idf_wrapper_invokes_only_named_target(self) -> None:
        context = mock.Mock()
        context.workspace = WORKSPACE
        context.log_path = Path("run.log")
        context.run.return_value = subprocess.CompletedProcess([], 0, "", "")
        prepared = IdfEnvironment(
            Path("/idf"),
            Path("/idf-python"),
            Path("/idf/tools/idf.py"),
            {"PATH": "idf-tools"},
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.runtime.prepare_idf_environment", return_value=prepared
                )
            )
            run_idf_target(
                context,
                idf_path=Path("/idf"),
                project=Path("/project"),
                build_dir=Path("/build"),
                target="mosaico-recover-flash",
                definitions={"MOSAICO_RECOVERY_SOURCE": "reviewed"},
                port="internal-device",
                timeout=180,
            )
        argv = context.run.call_args.args[0]
        environment = context.run.call_args.kwargs["env"]
        self.assertIn("mosaico-recover-flash", argv)
        self.assertNotIn("-p", argv)
        self.assertEqual(environment["ESPPORT"], "internal-device")
        self.assertNotIn("esptool", " ".join(str(item) for item in argv))
        self.assertNotIn("erase", " ".join(str(item) for item in argv))
        progress = context.run.call_args.kwargs["output_status"]
        self.assertEqual(progress("[10/100] compiling\n"), "idf: building 10% (10/100)")
        self.assertEqual(
            progress("Writing at 0x00002000... ( 50 % )\n"),
            "flash: writing 50% at 0x00002000",
        )

    def test_idf_wrapper_reports_bounded_cmake_diagnostic(self) -> None:
        context = mock.Mock(repository=REPOSITORY, log_path=Path("/runs/raw.log"))
        context.run.return_value = subprocess.CompletedProcess(
            [],
            1,
            "noise before failure\n"
            "CMake Error at tools/cmake/build.cmake:392 (message):\n"
            "  Failed to resolve component 'missing_component' required by component\n"
            "  'main': unknown name.\n"
            "Call Stack (most recent call first):\n"
            "  CMakeLists.txt:35 (project)\n",
            None,
        )
        prepared = IdfEnvironment(
            Path("/idf"),
            Path("/idf-python"),
            Path("/idf/tools/idf.py"),
            {"PATH": "idf-tools"},
        )
        with ExitStack() as contexts:
            contexts.enter_context(
                mock.patch(
                    "mosaico_cli.runtime.prepare_idf_environment", return_value=prepared
                )
            )
            caught = contexts.enter_context(self.assertRaises(BuildError))
            run_idf_target(
                context,
                idf_path=Path("/idf"),
                project=Path("/project"),
                build_dir=Path("/build"),
                target="system-update-bundle",
                timeout=180,
            )

        diagnostic = caught.exception.details["diagnostic"]
        self.assertIn("Build diagnostic (cmake)", diagnostic)
        self.assertIn("Failed to resolve component 'missing_component'", diagnostic)
        self.assertNotIn("noise before failure", diagnostic)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _emit_error(caught.exception, json_output=False, verbose=False)
        rendered = stderr.getvalue()
        self.assertIn("Failed to resolve component 'missing_component'", rendered)
        self.assertIn("Log: /runs/raw.log", rendered)

    def test_busy_recovery_port_is_reported_without_retry(self) -> None:
        context = mock.Mock(repository=REPOSITORY, log_path=Path("run.log"))
        context.run.return_value = subprocess.CompletedProcess(
            [], 1, "Could not exclusively lock port: port is busy", ""
        )
        prepared = IdfEnvironment(
            Path("/idf"),
            Path("/idf-python"),
            Path("/idf/tools/idf.py"),
            {"PATH": "idf-tools"},
        )
        with ExitStack() as _contexts:
            _contexts.enter_context(
                mock.patch(
                    "mosaico_cli.runtime.prepare_idf_environment", return_value=prepared
                )
            )
            _contexts.enter_context(self.assertRaises(DeviceError))
            run_idf_target(
                context,
                idf_path=Path("/idf"),
                project=Path("/project"),
                build_dir=Path("/build"),
                target="mosaico-recover-flash",
                port="internal-device",
                timeout=180,
            )
        self.assertEqual(context.run.call_count, 1)

    def test_run_context_streams_selected_child_status(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            context = RunContext(workspace_for(Path(temporary)), "stream-test")
            with ExitStack() as _contexts:
                status = _contexts.enter_context(mock.patch.object(context, "status"))
                result = context.run(
                    [sys.executable, "-c", "print('streamed child output')"],
                    output_status=lambda line: f"child: {line.strip()}",
                )
            self.assertEqual(result.returncode, 0)
            status.assert_called_once_with("child: streamed child output")
            self.assertIn("streamed child output", context.log_path.read_text())

    def test_run_context_hides_sensitive_command_and_output(self) -> None:
        with ExitStack() as _contexts:
            temporary = _contexts.enter_context(tempfile.TemporaryDirectory())
            context = RunContext(workspace_for(Path(temporary)), "sensitive-test")
            result = context.run(
                [sys.executable, "-c", "print('secret-url')"],
                sensitive_output=True,
            )
            self.assertEqual(result.returncode, 0)
            log = context.log_path.read_text()
            self.assertNotIn("secret-url", log)
            self.assertIn("[sensitive command omitted]", log)


if __name__ == "__main__":
    unittest.main()
