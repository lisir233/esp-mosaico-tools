"""Argument parsing and stable output for the public mosaico.py command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, NoReturn, Sequence
from urllib.parse import urlsplit

from . import __version__
from .commands import (
    configure_recovery_network,
    enter_recovery,
    install,
    list_devices,
    monitor,
    read_http_update_code,
    recover,
    start_system_update,
)
from .doctor import diagnose_host, print_diagnosis
from .errors import MosaicoError
from .runtime import RunContext
from .workspace import load_workspace


TOOL_ROOT = Path(__file__).resolve().parents[2]


class MosaicoArgumentParser(argparse.ArgumentParser):
    json_errors = False

    def error(self, message: str) -> NoReturn:
        if self.json_errors:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "selection_error",
                        "message": message,
                        "exit_code": 2,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            raise SystemExit(2)
        super().error(message)


def positive_timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return result


def hardware_mac(value: str) -> str:
    compact = value.replace(":", "").replace("-", "").lower()
    if len(compact) != 12 or any(
        character not in "0123456789abcdef" for character in compact
    ):
        raise argparse.ArgumentTypeError(
            "hardware MAC must contain exactly 12 hexadecimal digits"
        )
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def monitor_timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if result < 0:
        raise argparse.ArgumentTypeError("must not be less than 0")
    return result


def http_manifest_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("must be an absolute HTTP(S) URL")
    if not parsed.path or parsed.path.endswith("/"):
        raise argparse.ArgumentTypeError("must name a manifest file")
    if len(value.encode("utf-8")) >= 512:
        raise argparse.ArgumentTypeError("must be shorter than 512 UTF-8 bytes")
    return value


def nand_manifest_path(value: str) -> str:
    if not value.startswith("/nand/") or value.endswith("/") or "\\" in value:
        raise argparse.ArgumentTypeError("must be an absolute file below /nand")
    segments = value[len("/nand/") :].split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise argparse.ArgumentTypeError("must not contain empty, '.' or '..' segments")
    if len(value.encode("utf-8")) >= 256:
        raise argparse.ArgumentTypeError("must be shorter than 256 UTF-8 bytes")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = MosaicoArgumentParser(
        prog="mosaico.py",
        description="Unified ESP-Mosaico device command-line tool",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit stable JSON; monitor emits NDJSON"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show internal stages and full log paths"
    )
    parser.add_argument(
        "--workspace",
        help="Workspace directory or .mosaico.json path; discovered from cwd by default",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "doctor",
        help="Check the host environment without building or writing a device",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    install_parser = commands.add_parser(
        "install",
        help="Install a normal application through ESP-Iris",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    install_parser.add_argument(
        "--project", help="ESP-IDF application path; selected automatically by default"
    )
    install_parser.add_argument(
        "--device-id", help="Target Device ID; selected automatically when only one is available"
    )
    install_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the current profile by default"
    )
    install_parser.add_argument(
        "--skip-build", action="store_true", help="Reuse a complete existing build"
    )
    install_parser.add_argument(
        "--validation",
        choices=("elf-sha256", "version"),
        default="elf-sha256",
        help="Firmware identity validation method after installation",
    )
    install_parser.add_argument(
        "--timeout", type=positive_timeout, default=600.0, help="Installation timeout in seconds"
    )

    system_update_parser = commands.add_parser(
        "system-update",
        help="Install a validated multi-image or Recovery self-update bundle",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    system_update_source = system_update_parser.add_mutually_exclusive_group()
    system_update_source.add_argument(
        "--bundle",
        type=Path,
        help="Reuse an existing local .irisfw bundle instead of building one",
    )
    system_update_source.add_argument(
        "--manifest-url",
        type=http_manifest_url,
        help="Absolute URL of the exploded bundle manifest.json",
    )
    system_update_source.add_argument(
        "--manifest-path",
        type=nand_manifest_path,
        help="Absolute NAND LittleFS path of the exploded bundle manifest.json",
    )
    system_update_parser.add_argument(
        "--device-id",
        help="Target Device ID; selected automatically when only one is available",
    )
    system_update_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the current profile by default"
    )
    system_update_parser.add_argument(
        "--project", help="ESP-IDF application path; selected automatically by default"
    )
    system_update_parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse the default bundle from a complete existing build",
    )
    system_update_parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=900.0,
        help="System Update timeout in seconds",
    )

    recover_parser = commands.add_parser(
        "recover",
        help="Restore the device base firmware",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    recover_parser.add_argument(
        "--model", help="Device model; selected automatically when only one is supported"
    )
    recover_parser.add_argument(
        "--source",
        choices=("reviewed", "current"),
        default="reviewed",
        help="Reviewed base bundle or current-source candidate bundle",
    )
    recover_identity = recover_parser.add_mutually_exclusive_group()
    recover_identity.add_argument(
        "--device-id", help="Device ID used to correlate identity before and after recovery"
    )
    recover_identity.add_argument(
        "--hardware-mac", type=hardware_mac,
        help="Factory eFuse Base MAC used to select a managed or ROM-mode device",
    )
    recover_parser.add_argument(
        "--recovery-port", help="Explicit independent USB Serial/JTAG 303A:1001 port; requires a live managed device"
    )
    recover_parser.add_argument(
        "--timeout", type=positive_timeout, default=180.0,
        help="Recovery and validation timeout in seconds",
    )
    recover_parser.add_argument(
        "--dry-run", action="store_true", help="Check only; do not build or write firmware"
    )

    enter_recovery_parser = commands.add_parser(
        "enter-recovery",
        help="Enter retained Recovery without installing firmware",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    enter_recovery_parser.add_argument(
        "--device-id", help="Target Device ID; selected automatically when only one is available"
    )
    enter_recovery_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    enter_recovery_parser.add_argument(
        "--timeout", type=positive_timeout, default=30.0,
        help="Recovery transition timeout in seconds",
    )

    recovery_wifi_parser = commands.add_parser(
        "recovery-wifi",
        help="Configure Recovery Wi-Fi through the active USB session",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    recovery_wifi_parser.add_argument(
        "--ssid", required=True, help="Wi-Fi SSID; password is read without echo"
    )
    recovery_wifi_parser.add_argument(
        "--device-id", help="Target Device ID; selected automatically when only one is available"
    )
    recovery_wifi_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    recovery_wifi_parser.add_argument(
        "--timeout", type=positive_timeout, default=30.0,
        help="Wi-Fi connection timeout in seconds",
    )

    update_code_parser = commands.add_parser(
        "http-update-code",
        help="Open Recovery's HTTP Update page and read its code over USB",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    update_code_parser.add_argument(
        "--device-id", help="Target Device ID; selected automatically when only one is available"
    )
    update_code_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )

    monitor_parser = commands.add_parser(
        "monitor",
        help="View retained ESP-Iris logs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    monitor_parser.add_argument(
        "--device-id", help="Target Device ID; selected automatically when only one is available"
    )
    monitor_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the current profile by default"
    )
    monitor_parser.add_argument(
        "--timeout", type=monitor_timeout, default=0.0,
        help="Follow duration in seconds; 0 means no limit",
    )
    monitor_parser.add_argument(
        "--snapshot", action="store_true", help="Print retained logs and exit"
    )
    monitor_parser.add_argument("--grep", help="Client-side text filter")
    monitor_colors = monitor_parser.add_mutually_exclusive_group()
    monitor_colors.add_argument(
        "--force-color", action="store_true", help="Always emit ANSI log colors"
    )
    monitor_colors.add_argument(
        "--disable-auto-color",
        action="store_true",
        help="Disable automatic log coloring",
    )

    list_parser = commands.add_parser(
        "list",
        help="List devices visible through ESP-Iris",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    list_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    list_parser.add_argument(
        "--details",
        action="store_true",
        help="Show endpoint, ESP-IDF version, session, and capabilities",
    )
    return parser


def _normalize_globals(argv: Sequence[str]) -> list[str]:
    globals_found: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in {"--json", "--verbose", "--version"}:
            globals_found.append(value)
        elif value == "--workspace":
            globals_found.append(value)
            if index + 1 < len(argv):
                index += 1
                globals_found.append(argv[index])
        elif value.startswith("--workspace="):
            globals_found.append(value)
        else:
            rest.append(value)
        index += 1
    return [*globals_found, *rest]


def _print_device_table(result: dict[str, Any], details: bool) -> None:
    fields = [
        "device_id",
        "hardware_mac",
        "online",
        "connection",
        "alias",
        "project_name",
        "app_version",
        "firmware_mode",
        "boot_id",
    ]
    if details:
        fields.extend(["endpoint", "idf_version", "session_id", "capability_names"])
    headers = [field.upper() for field in fields]

    def cell(item: dict[str, Any], field: str) -> str:
        if field == "online":
            return "yes" if item.get(field) else "no"
        if field == "alias":
            return str(item.get("alias") or item.get("suggested_alias") or "")
        if field == "capability_names":
            values = item.get(field, [])
            return ",".join(str(value) for value in values) if isinstance(values, list) else ""
        return str(item.get(field, ""))

    rows = [
        [cell(item, field) for field in fields]
        for item in result["devices"]
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(fields))
    ]
    print("  ".join(headers[index].ljust(widths[index]) for index in range(len(fields))))
    for row in rows:
        print("  ".join(row[index].ljust(widths[index]) for index in range(len(fields))))


def _emit_error(error: MosaicoError, json_output: bool, verbose: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": error.category,
                    "message": str(error),
                    "exit_code": error.exit_code,
                    "details": error.details,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    else:
        print(f"mosaico: {error}", file=sys.stderr)
        candidates = error.details.get("candidates")
        if isinstance(candidates, list) and candidates:
            print("Available Device IDs:", file=sys.stderr)
            for candidate in candidates:
                print(f"  {candidate}", file=sys.stderr)
        diagnostic = error.details.get("diagnostic")
        if diagnostic:
            print(diagnostic, file=sys.stderr)
        build_log_dir = error.details.get("build_log_dir")
        if build_log_dir:
            print(f"Build logs: {build_log_dir}", file=sys.stderr)
        log = error.details.get("log")
        if log and not build_log_dir:
            print(f"Log: {log}", file=sys.stderr)
        if verbose and error.details:
            print(json.dumps(error.details, ensure_ascii=False, indent=2), file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    tool_root: Path | None = None,
) -> int:
    raw = list(argv or sys.argv[1:])
    MosaicoArgumentParser.json_errors = "--json" in raw
    arguments = build_parser().parse_args(_normalize_globals(raw))
    if sys.version_info < (3, 8):
        message = "mosaico.py requires Python 3.8 or newer."
        if arguments.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "environment_error",
                        "message": message,
                        "exit_code": 3,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        else:
            print(f"mosaico: {message}", file=sys.stderr)
        return 3
    try:
        workspace = load_workspace(
            (tool_root or TOOL_ROOT).resolve(), explicit=arguments.workspace
        )
    except MosaicoError as error:
        _emit_error(error, arguments.json, arguments.verbose)
        return error.exit_code

    if arguments.command == "list":
        context = RunContext(
            workspace,
            arguments.command,
            arguments.verbose,
            arguments.json,
        )
        try:
            result = list_devices(context, arguments.gateway_profile)
        except MosaicoError as error:
            error.details.setdefault("log", str(context.log_path))
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code
        if arguments.json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        else:
            _print_device_table(result, arguments.details)
        return 0

    if arguments.command == "doctor":
        try:
            result = diagnose_host(workspace)
        except MosaicoError as error:
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code
        if arguments.json:
            print(
                json.dumps(
                    {"ok": result["exit_code"] == 0, **result},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print_diagnosis(result)
        return int(result["exit_code"])

    context = RunContext(
        workspace,
        arguments.command,
        arguments.verbose,
        arguments.json,
    )
    if arguments.verbose and not arguments.json:
        print(f"Run log: {context.log_path}", file=sys.stderr)
    try:
        if arguments.command == "install":
            result = install(arguments, context)
        elif arguments.command == "system-update":
            result = start_system_update(arguments, context)
        elif arguments.command == "recover":
            result = recover(arguments, context)
        elif arguments.command == "enter-recovery":
            result = enter_recovery(arguments, context)
        elif arguments.command == "recovery-wifi":
            result = configure_recovery_network(arguments, context)
        elif arguments.command == "http-update-code":
            result = read_http_update_code(arguments, context)
        else:
            return monitor(arguments, context, arguments.json)
    except MosaicoError as error:
        error.details.setdefault("log", str(context.log_path))
        _emit_error(error, arguments.json, arguments.verbose)
        return error.exit_code
    except KeyboardInterrupt:
        return 0 if arguments.command == "monitor" else 5
    if arguments.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    else:
        status = result.get("status", "succeeded")
        print(f"{arguments.command}: {status}")
        if arguments.command == "http-update-code":
            authorization = result.get("authorization", {})
            print(f"HTTP update code: {authorization.get('code', '')}")
            print(
                "Expires in: "
                f"{int(authorization.get('expires_in_ms', 0)) // 1000}s"
            )
            print(f"HTTP endpoint: {result.get('base_url', '')}")
        if arguments.command == "recover" and status == "dry_run":
            print("Checks passed; no firmware was built or written.")
        if arguments.verbose:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
