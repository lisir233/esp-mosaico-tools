#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t factory_ui_start(void);

/* Open the same local authorization page used by the touch UI. This is used
 * only by the Recovery USB control RPC for physical-device test automation. */
esp_err_t factory_ui_open_http_update(void);

#ifdef __cplusplus
}
#endif
