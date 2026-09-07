from __future__ import annotations

import pathlib
import subprocess


ROOT = pathlib.Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "host_http_update_authorization"
SOURCE = ROOT / "firmware" / "recovery" / "main"


def test_http_update_authorization_state_machine(tmp_path: pathlib.Path) -> None:
    executable = tmp_path / "http_update_authorization_test"
    compile_result = subprocess.run(
        [
            "cc",
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pthread",
            "-I",
            str(FIXTURE / "stubs"),
            "-I",
            str(SOURCE),
            str(SOURCE / "factory_http_update_authorization.c"),
            str(FIXTURE / "main.c"),
            "-o",
            str(executable),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stdout
    run_result = subprocess.run(
        [str(executable)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert run_result.returncode == 0, run_result.stdout
    assert "tests passed" in run_result.stdout
