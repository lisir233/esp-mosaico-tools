from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT / "tools"))

from mosaico_cli.errors import EnvironmentError
from mosaico_cli.workspace import CONFIG_NAME, load_workspace


def configuration() -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace": {
            "projects_dir": "apps",
            "default_project": "apps/demo",
            "environment_file": "Environment",
            "run_dir": ".runs",
            "idf_constraint_manifest": "recovery/main/idf_component.yml",
        },
        "dependencies": {
            "bsp": "third_party/bsp",
            "esp_iris": "third_party/esp-iris",
        },
        "build": {"runner": "builtin"},
        "devices": [
            {
                "id": "board",
                "name": "Board",
                "target": "esp32s31",
                "status": "supported",
                "default": True,
                "preview_target": True,
                "recovery_project": "recovery",
                "bsp_path": "third_party/bsp",
                "recovery_dir": "recovery/prebuilt",
                "recovery_usb_ids": [],
            }
        ],
    }


class WorkspaceTests(unittest.TestCase):
    def test_discovers_workspace_above_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "apps" / "demo" / "main"
            nested.mkdir(parents=True)
            (root / CONFIG_NAME).write_text(
                json.dumps(configuration()), encoding="utf-8"
            )

            workspace = load_workspace(TOOL_ROOT, start=nested)

            self.assertEqual(workspace.root, root.resolve())
            self.assertEqual(workspace.projects_dir, (root / "apps").resolve())
            self.assertEqual(
                workspace.esp_iris_path, (root / "third_party" / "esp-iris").resolve()
            )
            self.assertTrue(str(workspace.build_runner).startswith(str(TOOL_ROOT)))

    def test_explicit_workspace_accepts_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "custom.json"
            path.write_text(json.dumps(configuration()), encoding="utf-8")

            workspace = load_workspace(
                TOOL_ROOT, start=Path("/"), explicit=str(path)
            )

            self.assertEqual(workspace.config_path, path.resolve())
            self.assertEqual(workspace.root, root.resolve())

    def test_rejects_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = configuration()
            value["schema_version"] = 99
            (root / CONFIG_NAME).write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(EnvironmentError):
                load_workspace(TOOL_ROOT, explicit=str(root))


if __name__ == "__main__":
    unittest.main()
