# ESP-Mosaico Tools

Repository-local command-line tools for ESP-Mosaico development and device
operations. The repository is intended to be pinned as a Git submodule by a
firmware workspace; it does not require the CLI package to be installed into
the user's Python environment.

The consuming repository owns a `.mosaico.json` file. All configured relative
paths are resolved from the directory containing that file, while built-in
tool resources are resolved from this checkout.

```sh
python3 submodule/esp-mosaico-tools/mosaico.py doctor
python3 submodule/esp-mosaico-tools/mosaico.py install --project projects/app
python3 submodule/esp-mosaico-tools/mosaico.py system-update --project projects/app
```

`install` updates only the application OTA partition. `system-update` builds
and submits the workspace's atomic application, UI assets, and system-data
bundle by default; use `--skip-build` or `--bundle PATH` to reuse artifacts.

The CLI searches the current directory and its parents for `.mosaico.json`.
Use `--workspace PATH` to select another workspace explicitly.

Run the self-contained tool tests with:

```sh
python3 -m unittest discover -s tests -v
```
