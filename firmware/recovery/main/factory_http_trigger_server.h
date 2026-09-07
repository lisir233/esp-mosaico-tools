#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Start the display-code-authorized Recovery HTTP control plane. The server
 * owns only trigger and status traffic; firmware bytes continue through the
 * existing HTTP System Update source and product Flash-policy backend. */
esp_err_t factory_http_trigger_server_start(void);

#ifdef __cplusplus
}
#endif
