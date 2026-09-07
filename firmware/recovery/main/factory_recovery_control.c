#include "factory_recovery_control.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "sdkconfig.h"

#include "esp_check.h"
#include "esp_iris.h"
#include "factory_http_update_authorization.h"
#include "factory_network.h"
#include "factory_ui.h"

#define FACTORY_CONTROL_SERVICE_ID            0x1202U
#define FACTORY_CONTROL_WIFI_CONNECT_METHOD   1U
#define FACTORY_CONTROL_NETWORK_STATUS_METHOD 2U
#define FACTORY_CONTROL_HTTP_CODE_METHOD      3U

static const char *TAG = "factory_control";

static void secure_clear(void *data, size_t size)
{
    volatile uint8_t *cursor = data;
    while (size-- > 0) {
        *cursor++ = 0;
    }
}

static esp_err_t require_usb_session(void)
{
    esp_iris_status_t status = {0};
    ESP_RETURN_ON_ERROR(esp_iris_get_status(&status), TAG,
                        "read ESP-Iris transport status");
    return status.session_ready &&
                   status.transport == ESP_IRIS_TRANSPORT_KIND_USB
        ? ESP_OK : ESP_ERR_NOT_ALLOWED;
}

static esp_err_t wifi_connect_rpc(const esp_iris_rpc_request_t *request,
                                  uint8_t *response,
                                  size_t response_capacity,
                                  size_t *response_size, void *user_ctx)
{
    (void)response;
    (void)response_capacity;
    (void)user_ctx;
    ESP_RETURN_ON_ERROR(require_usb_session(), TAG,
                        "Wi-Fi provisioning requires USB");
    ESP_RETURN_ON_FALSE(request != NULL && response_size != NULL &&
                            request->payload != NULL &&
                            request->payload_size >= 2U,
                        ESP_ERR_INVALID_SIZE, TAG,
                        "invalid Wi-Fi provisioning payload");
    const size_t ssid_size = request->payload[0];
    const size_t password_size = request->payload[1];
    ESP_RETURN_ON_FALSE(
        ssid_size > 0 && ssid_size < FACTORY_NETWORK_SSID_BYTES &&
            password_size < FACTORY_NETWORK_PASSWORD_BYTES &&
            request->payload_size == 2U + ssid_size + password_size,
        ESP_ERR_INVALID_SIZE, TAG, "invalid Wi-Fi credential sizes");
    char ssid[FACTORY_NETWORK_SSID_BYTES] = {0};
    char password[FACTORY_NETWORK_PASSWORD_BYTES] = {0};
    memcpy(ssid, request->payload + 2U, ssid_size);
    memcpy(password, request->payload + 2U + ssid_size, password_size);
    const esp_err_t err = factory_network_connect(ssid, password);
    secure_clear(password, sizeof(password));
    secure_clear(ssid, sizeof(ssid));
    ESP_RETURN_ON_ERROR(err, TAG, "connect factory Wi-Fi");
    *response_size = 0;
    return ESP_OK;
}

static esp_err_t network_status_rpc(const esp_iris_rpc_request_t *request,
                                    uint8_t *response,
                                    size_t response_capacity,
                                    size_t *response_size, void *user_ctx)
{
    (void)user_ctx;
    ESP_RETURN_ON_ERROR(require_usb_session(), TAG,
                        "network status requires USB");
    ESP_RETURN_ON_FALSE(request != NULL && request->payload_size == 0 &&
                            response != NULL && response_size != NULL,
                        ESP_ERR_INVALID_SIZE, TAG,
                        "invalid network status request");
    factory_network_snapshot_t snapshot = {0};
    ESP_RETURN_ON_ERROR(factory_network_get_snapshot(&snapshot), TAG,
                        "read factory network status");
    const int written = snprintf(
        (char *)response, response_capacity,
        "{\"state\":%u,\"connected\":%s,"
        "\"ip\":\"%s\",\"hostname\":\"%s\",\"error\":%ld}",
        (unsigned)snapshot.state,
        snapshot.state == FACTORY_NETWORK_CONNECTED ? "true" : "false",
        snapshot.ip, snapshot.hostname, (long)snapshot.last_error);
    ESP_RETURN_ON_FALSE(written >= 0 && (size_t)written < response_capacity,
                        ESP_ERR_INVALID_SIZE, TAG,
                        "network status response is too large");
    *response_size = (size_t)written;
    return ESP_OK;
}

#if CONFIG_IRIS_FACTORY_HTTP_TRIGGER_SERVER
static esp_err_t http_code_rpc(const esp_iris_rpc_request_t *request,
                               uint8_t *response, size_t response_capacity,
                               size_t *response_size, void *user_ctx)
{
    (void)user_ctx;
    ESP_RETURN_ON_ERROR(require_usb_session(), TAG,
                        "HTTP Update code requires USB");
    ESP_RETURN_ON_FALSE(request != NULL && request->payload_size == 0 &&
                            response != NULL && response_size != NULL,
                        ESP_ERR_INVALID_SIZE, TAG,
                        "invalid HTTP Update code request");
    ESP_RETURN_ON_ERROR(factory_ui_open_http_update(), TAG,
                        "open HTTP Update screen");
    factory_http_update_code_snapshot_t snapshot;
    ESP_RETURN_ON_ERROR(factory_http_update_code_get_snapshot(&snapshot), TAG,
                        "read HTTP Update code");
    ESP_RETURN_ON_FALSE(
        snapshot.state == FACTORY_HTTP_UPDATE_CODE_AVAILABLE,
        ESP_ERR_INVALID_STATE, TAG, "HTTP Update code is unavailable");
    const int written = snprintf(
        (char *)response, response_capacity,
        "{\"code\":\"%s\",\"expires_in_ms\":%lu,"
        "\"remaining_attempts\":%u,\"http_port\":%u}",
        snapshot.code, (unsigned long)snapshot.remaining_ms,
        snapshot.remaining_attempts,
        CONFIG_IRIS_FACTORY_HTTP_TRIGGER_SERVER_PORT);
    secure_clear(snapshot.code, sizeof(snapshot.code));
    ESP_RETURN_ON_FALSE(written >= 0 && (size_t)written < response_capacity,
                        ESP_ERR_INVALID_SIZE, TAG,
                        "HTTP Update code response is too large");
    *response_size = (size_t)written;
    return ESP_OK;
}
#endif

esp_err_t factory_recovery_control_register(void)
{
    ESP_RETURN_ON_ERROR(
        esp_iris_rpc_register(FACTORY_CONTROL_SERVICE_ID,
                              FACTORY_CONTROL_WIFI_CONNECT_METHOD,
                              wifi_connect_rpc, NULL),
        TAG, "register Recovery Wi-Fi control");
    ESP_RETURN_ON_ERROR(
        esp_iris_rpc_register(FACTORY_CONTROL_SERVICE_ID,
                              FACTORY_CONTROL_NETWORK_STATUS_METHOD,
                              network_status_rpc, NULL),
        TAG, "register Recovery network status");
#if CONFIG_IRIS_FACTORY_HTTP_TRIGGER_SERVER
    ESP_RETURN_ON_ERROR(
        esp_iris_rpc_register(FACTORY_CONTROL_SERVICE_ID,
                              FACTORY_CONTROL_HTTP_CODE_METHOD,
                              http_code_rpc, NULL),
        TAG, "register Recovery HTTP Update code control");
#endif
    return ESP_OK;
}
