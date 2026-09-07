# Recovery display-code HTTP System Update trigger

ESP-Mosaico Recovery exposes a small HTTP control plane on port `8080` by
default. It accepts only an absolute HTTPS URL for an exploded
`esp-iris-system-update/v1` bundle. Firmware bytes continue through the
Recovery HTTPS downloader and the existing product Flash-policy backend; they
are never uploaded to the control server.

This is a physical-presence recovery authorization for trusted local networks,
not durable pairing or proof of device ownership. The six-digit code crosses
the control connection in cleartext HTTP. A local observer can read or race it,
and the current product manifest policy remains unsigned.

## Authorization lifecycle

The operator opens **HTTP Update** on the device. Recovery generates one
uniformly random six-digit code, keeps it only in RAM, and displays its 60
second countdown and remaining attempts. Leaving the page or pressing Cancel
invalidates an unused code. Three failed attempts lock the code until a person
generates a new one on the screen.

A correct code is atomically consumed before parsing the manifest URL or
admitting the update. Malformed requests, a busy writer, allocation failure, or
download failure never restore it. No code remains valid while an update runs.

## Endpoints

| Method | Path | Authorization | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/v1/health` | none | Recovery identity and readiness |
| `POST` | `/api/v1/system-update` | six-digit display code | Start an asynchronous update |
| `GET` | `/api/v1/system-update/status` | 128-bit operation ID | Read only that HTTP operation |

Trigger one update:

```http
POST /api/v1/system-update HTTP/1.1
Content-Type: application/json
X-Mosaico-Pairing-Code: 038271

{"manifest_url":"https://updates.example/mosaico/1.2.0/manifest.json"}
```

Successful admission returns `202 Accepted`:

```json
{
  "operation_id": "9f7c37a23c9549582d578d58804f133f",
  "state": "accepted",
  "status_url": "/api/v1/system-update/status"
}
```

Subsequent reads use only the returned capability:

```http
GET /api/v1/system-update/status HTTP/1.1
X-Mosaico-Operation-ID: 9f7c37a23c9549582d578d58804f133f
```

An unknown, stale, malformed, or all-zero operation ID returns `404`. The
endpoint never substitutes status from a NAND, USB, or different HTTP update.
Only the current/most-recent HTTP operation is retained in RAM; a later HTTP
operation replaces it.

The update server must provide `manifest.json` and every root-level
`components[].file` in the same directory. Recovery verifies the HTTPS server
with the ESP-IDF certificate bundle and never follows redirects. A concurrent
HTTP, NAND, or ESP-Iris System Update returns `409 Conflict` after consuming a
correct code.

## Reference client

The client reads the code with hidden terminal input. There is deliberately no
`--code` option:

```sh
python submodule/esp-mosaico-tools/tools/http_update_trigger.py \
  --base-url http://mosaico-abcdef.local:8080 trigger \
  https://updates.example/mosaico/1.2.0/manifest.json
```

It stores the operation ID only in process memory and polls until the HTTP
source commits or fails. A connection loss during Recovery reboot is not by
itself proof of success; end-to-end validation still correlates the same Device
ID, a new Boot ID, and the intended normal firmware behavior.

For a reachable normal application, enter retained Recovery without rebuilding
or installing it:

```sh
python mosaico.py enter-recovery --device-id DEVICE_ID
```

The command waits for the same Device ID to reconnect in Recovery with a new
Boot ID. Its JSON result includes exact `boot_id_before_text` and
`boot_id_text` values in addition to numeric compatibility fields.

For physical-device automation, `mosaico.py recovery-wifi` then securely
prompts for Wi-Fi credentials, and `mosaico.py http-update-code` opens the same
device page and reads its code through the active USB ESP-Iris session. The
firmware denies these Recovery control methods over TCP.
