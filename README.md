# ESP-Mosaico Tools

Repository-local command-line tools for ESP-Mosaico development and device
operations. The repository is intended to be pinned as a Git submodule by a
firmware workspace; it does not require the CLI package to be installed into
the user's Python environment.

The consuming repository owns a `.mosaico.json` file. All configured relative
paths are resolved from the directory containing that file. Recovery firmware
source and its reviewed bundle live under `firmware/recovery` and are resolved
from this checkout so the CLI and Recovery implementation are versioned
together. ESP-Iris is pinned as this repository's nested Git submodule, making
the tools checkout the single source of its device-side Iris implementation.

```sh
python3 submodule/esp-mosaico-tools/mosaico.py doctor
python3 submodule/esp-mosaico-tools/mosaico.py install --project projects/app
python3 submodule/esp-mosaico-tools/mosaico.py system-update --project projects/app
python3 submodule/esp-mosaico-tools/mosaico.py enter-recovery
```

`install` updates only the application OTA partition. `system-update` builds
and submits the workspace's atomic application, UI assets, and system-data
bundle by default; use `--skip-build` or `--bundle PATH` to reuse artifacts.
`enter-recovery` asks a reachable normal application to boot the retained
Recovery image without building or installing firmware. It waits for the same
Device ID to reconnect in Recovery with a new Boot ID; use `--device-id` when
more than one device is connected and `--timeout` to change the 30-second
transition limit. Both numeric Boot IDs and exact `boot_id_text` fields are
included in JSON output so 64-bit identities remain lossless for JavaScript
consumers.

The CLI searches the current directory and its parents for `.mosaico.json`.
Use `--workspace PATH` to select another workspace explicitly.

Run the self-contained tool tests with:

```sh
python3 -m unittest discover -s tests -v
```


## Recovery through an independent USB Serial/JTAG connection

When the same board has a separate USB Serial/JTAG cable connected, select its
port explicitly while the primary ESP-Iris connection is still live:

```sh
python mosaico.py recover --device-id DEVICE_ID --recovery-port COM14 --source current
```

Omit `--source current` to use the reviewed bundle. `--dry-run` resolves the live
identity and port without building, leasing, resetting or writing. This option
requires exactly one connected Espressif `303A:1001` interface, and the named
port must identify it. Selecting it asserts that this independently connected
interface belongs to the chosen board; USB serial numbers/MACs are not used to
invent a mapping to the primary Device ID. With no live primary device, use the
existing normal Recovery procedure instead.

The command prepares the complete reviewed/current Recovery bundle before
maintenance. It acquires the primary device lease (including crash evidence)
and a separate physical endpoint lease, so Gateway sessions on both interfaces
are detached and their cross-process reservations remain held during flashing.
The serial interface is enumerated again before the write; an identity change,
ambiguous endpoint, conflicting owner or lease failure prevents flashing. A
Gateway that cannot reserve both interfaces fails closed. Other applications
must release the serial port; a busy port is an error, never an invitation to
force another session open.

Only the existing `mosaico-recover-flash` target writes firmware. Its complete
bundle writes bootloader, partition table, OTA selection data and factory
Recovery; this option does **not** introduce whole-flash erase or a
Recovery-partition-only mode. Preserve the normal bundle/layout contract.
Recovery acceptance uses the original managed connection: the same Device ID,
a new Boot ID and the prepared Recovery version must be verified before the
independent endpoint lease is released. The auxiliary lease is released with
an abort action because it represents only a transport reservation, not a
second acceptance result. Failure unwinds remaining leases, with quarantine
reported if cleanup fails.

`recovery-route.json` in the operation evidence directory records both lease
IDs, the selected USB identity and the original Device/Boot IDs, without lease
tokens. Gateway records retain the detailed before/after and crash evidence.
