#pragma once

#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define FACTORY_HTTP_UPDATE_CODE_DIGITS 6U
#define FACTORY_HTTP_UPDATE_CODE_BYTES  7U

typedef enum {
    FACTORY_HTTP_UPDATE_CODE_DISABLED = 0,
    FACTORY_HTTP_UPDATE_CODE_AVAILABLE,
    FACTORY_HTTP_UPDATE_CODE_EXPIRED,
    FACTORY_HTTP_UPDATE_CODE_LOCKED,
    FACTORY_HTTP_UPDATE_CODE_CONSUMED,
    FACTORY_HTTP_UPDATE_CODE_UPDATE_RUNNING,
    FACTORY_HTTP_UPDATE_CODE_START_FAILED,
} factory_http_update_code_state_t;

typedef enum {
    FACTORY_HTTP_UPDATE_CODE_ACCEPTED = 0,
    FACTORY_HTTP_UPDATE_CODE_REJECTED,
    FACTORY_HTTP_UPDATE_CODE_UNAVAILABLE,
} factory_http_update_code_result_t;

typedef struct {
    factory_http_update_code_state_t state;
    char code[FACTORY_HTTP_UPDATE_CODE_BYTES];
    uint32_t remaining_ms;
    uint8_t remaining_attempts;
} factory_http_update_code_snapshot_t;

/* Generate a new, uniformly distributed six-digit code and start a fresh
 * rotation interval. This is called only from a local UI action or a USB-only
 * Recovery control RPC. */
esp_err_t factory_http_update_code_generate(void);

/* Cancel a code which has not yet authorized an update. Consumed/running
 * authorization cannot be rolled back into an available code. */
void factory_http_update_code_cancel(void);

/* Validate and atomically consume the supplied code. An elapsed interval is
 * rotated before validation. Missing and malformed values count as failed
 * attempts while a code is available. */
factory_http_update_code_result_t factory_http_update_code_consume(
    const char *supplied_code);

/* Move the already-consumed authorization through the update admission and
 * execution states without ever restoring the code. */
void factory_http_update_code_mark_running(void);
void factory_http_update_code_mark_start_failed(void);
void factory_http_update_code_mark_update_failed(void);

/* Return the current display state, rotating an elapsed available/locked
 * interval first. A locked snapshot reports its time until automatic reset. */
esp_err_t factory_http_update_code_get_snapshot(
    factory_http_update_code_snapshot_t *snapshot);

#ifdef __cplusplus
}
#endif
