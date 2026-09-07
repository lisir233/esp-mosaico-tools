#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Register USB-only Recovery control methods used to configure Wi-Fi and to
 * open/read the physical HTTP Update authorization screen during testing. */
esp_err_t factory_recovery_control_register(void);

#ifdef __cplusplus
}
#endif
