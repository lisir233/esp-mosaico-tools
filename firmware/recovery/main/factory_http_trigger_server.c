#include "factory_http_trigger_server.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "sdkconfig.h"

#if CONFIG_IRIS_FACTORY_HTTP_TRIGGER_SERVER

#include "cJSON.h"
#include "esp_check.h"
#include "esp_http_server.h"
#include "esp_iris.h"
#include "esp_log.h"
#include "factory_http_update_authorization.h"
#include "factory_system_update.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define HTTP_TRIGGER_REQUEST_BODY_BYTES \
    (FACTORY_SYSTEM_UPDATE_URL_BYTES + 96U)
#define HTTP_TRIGGER_CODE_HEADER_BYTES 16U
#define HTTP_TRIGGER_OPERATION_HEX_BYTES \
    (ESP_IRIS_SYSTEM_OPERATION_ID_BYTES * 2U + 1U)

#define HTTP_TRIGGER_UPDATE_PATH "/api/v1/system-update"
#define HTTP_TRIGGER_STATUS_PATH "/api/v1/system-update/status"
#define HTTP_TRIGGER_HEALTH_PATH "/api/v1/health"
#define HTTP_TRIGGER_CODE_HEADER "X-Mosaico-Pairing-Code"
#define HTTP_TRIGGER_OPERATION_HEADER "X-Mosaico-Operation-ID"

static const char *TAG = "factory_http_trigger";
static httpd_handle_t s_server;

static bool constant_time_equal(const uint8_t *left, const uint8_t *right,
                                size_t size)
{
    uint8_t difference = 0;
    for (size_t i = 0; i < size; ++i) {
        difference |= left[i] ^ right[i];
    }
    return difference == 0;
}

static void secure_clear(void *data, size_t size)
{
    volatile uint8_t *cursor = data;
    while (size-- > 0) {
        *cursor++ = 0;
    }
}

static void hex_encode(const uint8_t *input, size_t size, char *output)
{
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < size; ++i) {
        output[i * 2U] = hex[input[i] >> 4U];
        output[i * 2U + 1U] = hex[input[i] & 0x0fU];
    }
    output[size * 2U] = '\0';
}

static bool hex_decode_canonical(const char *input, size_t output_size,
                                 uint8_t *output)
{
    if (input == NULL || strlen(input) != output_size * 2U) {
        return false;
    }
    for (size_t i = 0; i < output_size; ++i) {
        uint8_t value = 0;
        for (size_t nibble = 0; nibble < 2U; ++nibble) {
            const char character = input[i * 2U + nibble];
            uint8_t digit;
            if (character >= '0' && character <= '9') {
                digit = (uint8_t)(character - '0');
            } else if (character >= 'a' && character <= 'f') {
                digit = (uint8_t)(character - 'a' + 10);
            } else {
                secure_clear(output, output_size);
                return false;
            }
            value = (uint8_t)((value << 4U) | digit);
        }
        output[i] = value;
    }
    return true;
}

static bool operation_id_nonzero(
    const uint8_t operation_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES])
{
    uint8_t nonzero = 0;
    for (size_t i = 0; i < ESP_IRIS_SYSTEM_OPERATION_ID_BYTES; ++i) {
        nonzero |= operation_id[i];
    }
    return nonzero != 0;
}

static esp_err_t respond_json(httpd_req_t *request, const char *status,
                              const char *body)
{
    httpd_resp_set_status(request, status);
    httpd_resp_set_type(request, "application/json");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    httpd_resp_set_hdr(request, "Pragma", "no-cache");
    httpd_resp_set_hdr(request, "X-Content-Type-Options", "nosniff");
    return httpd_resp_send(request, body, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t respond_error(httpd_req_t *request, const char *status,
                               const char *code)
{
    char body[128];
    const int written = snprintf(body, sizeof(body),
                                 "{\"error\":\"%s\"}", code);
    if (written < 0 || (size_t)written >= sizeof(body)) {
        return ESP_FAIL;
    }
    return respond_json(request, status, body);
}

static esp_err_t request_header(httpd_req_t *request, const char *name,
                                char *output, size_t capacity)
{
    const size_t length = httpd_req_get_hdr_value_len(request, name);
    if (length == 0 || length >= capacity) {
        return ESP_ERR_INVALID_SIZE;
    }
    return httpd_req_get_hdr_value_str(request, name, output, capacity);
}

static esp_err_t read_request_body(httpd_req_t *request, uint8_t *output,
                                   size_t capacity, size_t *output_size)
{
    if (request->content_len == 0 || request->content_len >= capacity) {
        return ESP_ERR_INVALID_SIZE;
    }
    size_t received = 0;
    while (received < request->content_len) {
        const int result = httpd_req_recv(
            request, (char *)output + received,
            request->content_len - received);
        if (result == HTTPD_SOCK_ERR_TIMEOUT) {
            continue;
        }
        if (result <= 0) {
            return ESP_FAIL;
        }
        received += (size_t)result;
    }
    output[received] = '\0';
    *output_size = received;
    return ESP_OK;
}

static bool json_content_type(httpd_req_t *request)
{
    char content_type[48];
    if (request_header(request, "Content-Type", content_type,
                       sizeof(content_type)) != ESP_OK) {
        return false;
    }
    static const char expected[] = "application/json";
    return strncmp(content_type, expected, sizeof(expected) - 1U) == 0 &&
           (content_type[sizeof(expected) - 1U] == '\0' ||
            content_type[sizeof(expected) - 1U] == ';');
}

static bool manifest_url_is_https(const char *url)
{
    static const char scheme[] = "https://";
    if (url == NULL || strncmp(url, scheme, sizeof(scheme) - 1U) != 0) {
        return false;
    }
    const char *authority = url + sizeof(scheme) - 1U;
    const char *cursor = authority;
    while (*cursor != '\0' && *cursor != '/' && *cursor != '?' &&
           *cursor != '#') {
        const unsigned char character = (unsigned char)*cursor;
        if (character <= 0x20U || character == 0x7fU || character == '@' ||
            character == '\\') {
            return false;
        }
        ++cursor;
    }
    if (cursor == authority || *cursor != '/') {
        return false;
    }
    for (; *cursor != '\0'; ++cursor) {
        const unsigned char character = (unsigned char)*cursor;
        if (character <= 0x20U || character == 0x7fU || character == '#' ||
            character == '\\') {
            return false;
        }
    }
    return true;
}

static esp_err_t parse_manifest_url(const uint8_t *body, size_t body_size,
                                    char output[FACTORY_SYSTEM_UPDATE_URL_BYTES])
{
    /* cJSON represents strings as NUL-terminated values. Reject the JSON
     * escape which could otherwise introduce an embedded NUL. */
    if (body_size >= 6U) {
        for (size_t i = 0; i + 6U <= body_size; ++i) {
            if (memcmp(body + i, "\\u0000", 6U) == 0) {
                return ESP_ERR_INVALID_ARG;
            }
        }
    }
    const char *parse_end = NULL;
    cJSON *root = cJSON_ParseWithLengthOpts(
        (const char *)body, body_size + 1U, &parse_end, true);
    if (root == NULL || parse_end != (const char *)body + body_size ||
        !cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return ESP_ERR_INVALID_ARG;
    }
    const cJSON *item = root->child;
    const bool valid = item != NULL && item->next == NULL &&
        item->string != NULL && strcmp(item->string, "manifest_url") == 0 &&
        cJSON_IsString(item) && item->valuestring != NULL;
    if (!valid) {
        cJSON_Delete(root);
        return ESP_ERR_INVALID_ARG;
    }
    const size_t url_size = strnlen(item->valuestring,
                                    FACTORY_SYSTEM_UPDATE_URL_BYTES);
    if (url_size == 0 || url_size >= FACTORY_SYSTEM_UPDATE_URL_BYTES ||
        !manifest_url_is_https(item->valuestring)) {
        cJSON_Delete(root);
        return ESP_ERR_INVALID_ARG;
    }
    strlcpy(output, item->valuestring, FACTORY_SYSTEM_UPDATE_URL_BYTES);
    cJSON_Delete(root);
    return ESP_OK;
}

static esp_err_t require_update_code(httpd_req_t *request)
{
    char supplied[HTTP_TRIGGER_CODE_HEADER_BYTES] = {0};
    const char *candidate = NULL;
    if (request_header(request, HTTP_TRIGGER_CODE_HEADER, supplied,
                       sizeof(supplied)) == ESP_OK) {
        candidate = supplied;
    }
    const factory_http_update_code_result_t result =
        factory_http_update_code_consume(candidate);
    secure_clear(supplied, sizeof(supplied));
    if (result == FACTORY_HTTP_UPDATE_CODE_ACCEPTED) {
        return ESP_OK;
    }
    if (CONFIG_IRIS_FACTORY_HTTP_TRIGGER_FAILURE_DELAY_MS > 0) {
        vTaskDelay(pdMS_TO_TICKS(
            CONFIG_IRIS_FACTORY_HTTP_TRIGGER_FAILURE_DELAY_MS));
    }
    httpd_resp_set_hdr(request, "WWW-Authenticate", "Mosaico-Pairing-Code");
    (void)respond_error(request, "401 Unauthorized", "unauthorized");
    return ESP_ERR_INVALID_CRC;
}

static esp_err_t update_handler(httpd_req_t *request)
{
    if (!json_content_type(request)) {
        return respond_error(request, "415 Unsupported Media Type",
                             "content_type_must_be_application_json");
    }
    uint8_t body[HTTP_TRIGGER_REQUEST_BODY_BYTES + 1U];
    size_t body_size = 0;
    const esp_err_t read_err = read_request_body(
        request, body, sizeof(body), &body_size);
    if (read_err != ESP_OK) {
        return respond_error(request, "413 Content Too Large",
                             "invalid_request_body_size");
    }
    if (require_update_code(request) != ESP_OK) {
        secure_clear(body, sizeof(body));
        return ESP_OK;
    }

    factory_http_update_code_mark_running();
    char manifest_url[FACTORY_SYSTEM_UPDATE_URL_BYTES] = {0};
    const esp_err_t parse_err = parse_manifest_url(body, body_size,
                                                    manifest_url);
    secure_clear(body, sizeof(body));
    if (parse_err != ESP_OK) {
        factory_http_update_code_mark_start_failed();
        return respond_error(request, "400 Bad Request",
                             "invalid_manifest_url_request");
    }
    uint8_t operation_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES];
    const esp_err_t start_err = factory_system_update_start_http_with_id(
        manifest_url, operation_id);
    secure_clear(manifest_url, sizeof(manifest_url));
    if (start_err != ESP_OK) {
        factory_http_update_code_mark_start_failed();
    }
    if (start_err == ESP_ERR_INVALID_STATE) {
        return respond_error(request, "409 Conflict", "system_update_busy");
    }
    if (start_err == ESP_ERR_INVALID_ARG ||
        start_err == ESP_ERR_INVALID_SIZE) {
        return respond_error(request, "400 Bad Request",
                             "manifest_url_not_allowed");
    }
    if (start_err != ESP_OK) {
        return respond_error(request, "503 Service Unavailable",
                             "could_not_start_system_update");
    }

    char operation_hex[HTTP_TRIGGER_OPERATION_HEX_BYTES];
    char response[256];
    hex_encode(operation_id, sizeof(operation_id), operation_hex);
    const int written = snprintf(
        response, sizeof(response),
        "{\"operation_id\":\"%s\",\"state\":\"accepted\","
        "\"status_url\":\"%s\"}",
        operation_hex, HTTP_TRIGGER_STATUS_PATH);
    secure_clear(operation_id, sizeof(operation_id));
    if (written < 0 || (size_t)written >= sizeof(response)) {
        return respond_error(request, "500 Internal Server Error",
                             "response_too_large");
    }
    return respond_json(request, "202 Accepted", response);
}

static const char *http_source_state_name(factory_http_update_state_t state)
{
    switch (state) {
    case FACTORY_HTTP_UPDATE_IDLE:
        return "idle";
    case FACTORY_HTTP_UPDATE_WAITING_NETWORK:
        return "waiting_network";
    case FACTORY_HTTP_UPDATE_FETCHING_MANIFEST:
        return "fetching_manifest";
    case FACTORY_HTTP_UPDATE_APPLYING:
        return "applying";
    case FACTORY_HTTP_UPDATE_COMMITTED:
        return "committed";
    case FACTORY_HTTP_UPDATE_FAILED:
        return "failed";
    default:
        return "unknown";
    }
}

static const char *phase_name(esp_iris_system_update_phase_t phase)
{
    switch (phase) {
    case ESP_IRIS_SYSTEM_UPDATE_PHASE_IDLE:
        return "idle";
    case ESP_IRIS_SYSTEM_UPDATE_PHASE_PREPARED:
        return "prepared";
    case ESP_IRIS_SYSTEM_UPDATE_PHASE_RECEIVING:
        return "receiving";
    case ESP_IRIS_SYSTEM_UPDATE_PHASE_COMPONENT_VERIFIED:
        return "component_verified";
    case ESP_IRIS_SYSTEM_UPDATE_PHASE_COMMITTING:
        return "committing";
    case ESP_IRIS_SYSTEM_UPDATE_PHASE_COMMITTED:
        return "committed";
    case ESP_IRIS_SYSTEM_UPDATE_PHASE_CANCELLED:
        return "cancelled";
    case ESP_IRIS_SYSTEM_UPDATE_PHASE_FAILED:
        return "failed";
    default:
        return "unknown";
    }
}

static esp_err_t status_handler(httpd_req_t *request)
{
    if (request->content_len != 0) {
        return respond_error(request, "400 Bad Request",
                             "status_request_must_be_empty");
    }
    char operation_hex[HTTP_TRIGGER_OPERATION_HEX_BYTES] = {0};
    uint8_t requested_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES] = {0};
    if (request_header(request, HTTP_TRIGGER_OPERATION_HEADER, operation_hex,
                       sizeof(operation_hex)) != ESP_OK ||
        !hex_decode_canonical(operation_hex, sizeof(requested_id),
                              requested_id) ||
        !operation_id_nonzero(requested_id)) {
        return respond_error(request, "404 Not Found", "operation_not_found");
    }

    factory_http_update_snapshot_t source;
    factory_system_update_status_t backend;
    if (factory_http_update_get_snapshot(&source) != ESP_OK ||
        factory_system_update_get_status(&backend) != ESP_OK) {
        secure_clear(requested_id, sizeof(requested_id));
        return respond_error(request, "503 Service Unavailable",
                             "status_unavailable");
    }
    if (!constant_time_equal(requested_id, source.operation_id,
                             sizeof(requested_id))) {
        secure_clear(requested_id, sizeof(requested_id));
        return respond_error(request, "404 Not Found", "operation_not_found");
    }

    const bool backend_matches =
        backend.owner == FACTORY_SYSTEM_UPDATE_OWNER_HTTP &&
        constant_time_equal(requested_id, backend.update.operation_id,
                            sizeof(requested_id));
    secure_clear(requested_id, sizeof(requested_id));
    char response[640];
    int written;
    if (backend_matches) {
        written = snprintf(
            response, sizeof(response),
            "{\"operation_id\":\"%s\",\"source_state\":\"%s\","
            "\"source_result\":%ld,\"phase\":\"%s\",\"result\":%ld,"
            "\"component_count\":%u,\"completed_components\":%u,"
            "\"active_component_id\":%u,\"component_received\":%lu,"
            "\"component_size\":%lu}",
            operation_hex, http_source_state_name(source.state),
            (long)source.result, phase_name(backend.update.phase),
            (long)backend.update.result, backend.update.component_count,
            backend.update.completed_components,
            backend.update.active_component_id,
            (unsigned long)backend.update.component_received,
            (unsigned long)backend.update.component_size);
    } else {
        /* A later NAND/USB transaction may replace the shared backend status.
         * Return only the retained HTTP-source state rather than leaking it. */
        written = snprintf(
            response, sizeof(response),
            "{\"operation_id\":\"%s\",\"source_state\":\"%s\","
            "\"source_result\":%ld}",
            operation_hex, http_source_state_name(source.state),
            (long)source.result);
    }
    if (written < 0 || (size_t)written >= sizeof(response)) {
        return respond_error(request, "500 Internal Server Error",
                             "response_too_large");
    }
    return respond_json(request, "200 OK", response);
}

static esp_err_t health_handler(httpd_req_t *request)
{
    char device_id[33];
    char response[192];
    ESP_RETURN_ON_ERROR(esp_iris_format_device_id(device_id), TAG,
                        "format HTTP health Device ID");
    const int written = snprintf(
        response, sizeof(response),
        "{\"status\":\"ok\",\"mode\":\"recovery\","
        "\"device_id\":\"%s\",\"authorization\":"
        "\"display-code/v1\"}", device_id);
    if (written < 0 || (size_t)written >= sizeof(response)) {
        return respond_error(request, "500 Internal Server Error",
                             "response_too_large");
    }
    return respond_json(request, "200 OK", response);
}

esp_err_t factory_http_trigger_server_start(void)
{
    if (s_server != NULL) {
        return ESP_OK;
    }
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = CONFIG_IRIS_FACTORY_HTTP_TRIGGER_SERVER_PORT;
    config.stack_size =
        CONFIG_IRIS_FACTORY_HTTP_TRIGGER_SERVER_TASK_STACK;
    config.max_uri_handlers = 3;
    config.max_open_sockets = 2;
    config.backlog_conn = 2;
    config.lru_purge_enable = true;
    config.recv_wait_timeout = 5;
    config.send_wait_timeout = 5;
    ESP_RETURN_ON_ERROR(httpd_start(&s_server, &config), TAG,
                        "start display-code HTTP trigger server");

    const httpd_uri_t handlers[] = {
        {.uri = HTTP_TRIGGER_UPDATE_PATH, .method = HTTP_POST,
         .handler = update_handler},
        {.uri = HTTP_TRIGGER_STATUS_PATH, .method = HTTP_GET,
         .handler = status_handler},
        {.uri = HTTP_TRIGGER_HEALTH_PATH, .method = HTTP_GET,
         .handler = health_handler},
    };
    for (size_t i = 0; i < sizeof(handlers) / sizeof(handlers[0]); ++i) {
        const esp_err_t err = httpd_register_uri_handler(s_server,
                                                         &handlers[i]);
        if (err != ESP_OK) {
            (void)httpd_stop(s_server);
            s_server = NULL;
            return err;
        }
    }
    ESP_LOGI(TAG, "display-code HTTP update trigger listening on port %u",
             CONFIG_IRIS_FACTORY_HTTP_TRIGGER_SERVER_PORT);
    return ESP_OK;
}

#else

esp_err_t factory_http_trigger_server_start(void)
{
    return ESP_OK;
}

#endif
