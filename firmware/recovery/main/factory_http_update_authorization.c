#include "factory_http_update_authorization.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "sdkconfig.h"

#if CONFIG_IRIS_FACTORY_HTTP_TRIGGER_SERVER

#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"

#define FACTORY_HTTP_UPDATE_CODE_RANGE 1000000U

typedef struct {
    factory_http_update_code_state_t state;
    char code[FACTORY_HTTP_UPDATE_CODE_BYTES];
    int64_t expires_us;
    uint8_t failed_attempts;
} factory_http_update_code_context_t;

static factory_http_update_code_context_t s_code;
static portMUX_TYPE s_code_lock = portMUX_INITIALIZER_UNLOCKED;

static void secure_clear(void *data, size_t size)
{
    volatile uint8_t *cursor = data;
    while (size-- > 0) {
        *cursor++ = 0;
    }
}

static void expire_locked(int64_t now)
{
    if (s_code.state == FACTORY_HTTP_UPDATE_CODE_AVAILABLE &&
        now >= s_code.expires_us) {
        secure_clear(s_code.code, sizeof(s_code.code));
        s_code.state = FACTORY_HTTP_UPDATE_CODE_EXPIRED;
        s_code.expires_us = 0;
    }
}

static uint32_t uniform_code_value(void)
{
    /* Accept an exact multiple of one million values from the uint32_t range
     * so every displayed code, including 000000, has equal probability. */
    const uint32_t limit = UINT32_MAX -
        (UINT32_MAX % FACTORY_HTTP_UPDATE_CODE_RANGE);
    uint32_t value;
    do {
        esp_fill_random(&value, sizeof(value));
    } while (value >= limit);
    return value % FACTORY_HTTP_UPDATE_CODE_RANGE;
}

esp_err_t factory_http_update_code_generate(void)
{
    const uint32_t value = uniform_code_value();
    char code[FACTORY_HTTP_UPDATE_CODE_BYTES];
    const int written = snprintf(code, sizeof(code), "%06lu",
                                 (unsigned long)value);
    if (written != FACTORY_HTTP_UPDATE_CODE_DIGITS) {
        secure_clear(code, sizeof(code));
        return ESP_FAIL;
    }

    esp_err_t result = ESP_OK;
    taskENTER_CRITICAL(&s_code_lock);
    if (s_code.state == FACTORY_HTTP_UPDATE_CODE_CONSUMED ||
        s_code.state == FACTORY_HTTP_UPDATE_CODE_UPDATE_RUNNING) {
        result = ESP_ERR_INVALID_STATE;
    } else {
        memcpy(s_code.code, code, sizeof(s_code.code));
        s_code.failed_attempts = 0;
        s_code.expires_us = esp_timer_get_time() +
            (int64_t)CONFIG_IRIS_FACTORY_HTTP_TRIGGER_CODE_TTL_MS * 1000;
        s_code.state = FACTORY_HTTP_UPDATE_CODE_AVAILABLE;
    }
    taskEXIT_CRITICAL(&s_code_lock);
    secure_clear(code, sizeof(code));
    return result;
}

void factory_http_update_code_cancel(void)
{
    taskENTER_CRITICAL(&s_code_lock);
    if (s_code.state != FACTORY_HTTP_UPDATE_CODE_CONSUMED &&
        s_code.state != FACTORY_HTTP_UPDATE_CODE_UPDATE_RUNNING) {
        secure_clear(s_code.code, sizeof(s_code.code));
        s_code.state = FACTORY_HTTP_UPDATE_CODE_DISABLED;
        s_code.expires_us = 0;
        s_code.failed_attempts = 0;
    }
    taskEXIT_CRITICAL(&s_code_lock);
}

factory_http_update_code_result_t factory_http_update_code_consume(
    const char *supplied_code)
{
    char candidate[FACTORY_HTTP_UPDATE_CODE_DIGITS] = {0};
    const size_t supplied_size = supplied_code != NULL
        ? strnlen(supplied_code, FACTORY_HTTP_UPDATE_CODE_BYTES) : 0;
    bool canonical = supplied_size == FACTORY_HTTP_UPDATE_CODE_DIGITS;
    for (size_t i = 0; i < FACTORY_HTTP_UPDATE_CODE_DIGITS; ++i) {
        const char character = i < supplied_size ? supplied_code[i] : '\0';
        candidate[i] = character;
        canonical = canonical && character >= '0' && character <= '9';
    }

    factory_http_update_code_result_t result =
        FACTORY_HTTP_UPDATE_CODE_UNAVAILABLE;
    taskENTER_CRITICAL(&s_code_lock);
    expire_locked(esp_timer_get_time());
    if (s_code.state == FACTORY_HTTP_UPDATE_CODE_AVAILABLE) {
        uint8_t difference = canonical ? 0U : 1U;
        for (size_t i = 0; i < FACTORY_HTTP_UPDATE_CODE_DIGITS; ++i) {
            difference |= (uint8_t)(candidate[i] ^ s_code.code[i]);
        }
        if (difference == 0) {
            secure_clear(s_code.code, sizeof(s_code.code));
            s_code.expires_us = 0;
            s_code.state = FACTORY_HTTP_UPDATE_CODE_CONSUMED;
            result = FACTORY_HTTP_UPDATE_CODE_ACCEPTED;
        } else {
            ++s_code.failed_attempts;
            if (s_code.failed_attempts >=
                CONFIG_IRIS_FACTORY_HTTP_TRIGGER_MAX_ATTEMPTS) {
                secure_clear(s_code.code, sizeof(s_code.code));
                s_code.expires_us = 0;
                s_code.state = FACTORY_HTTP_UPDATE_CODE_LOCKED;
            }
            result = FACTORY_HTTP_UPDATE_CODE_REJECTED;
        }
    }
    taskEXIT_CRITICAL(&s_code_lock);
    secure_clear(candidate, sizeof(candidate));
    return result;
}

void factory_http_update_code_mark_running(void)
{
    taskENTER_CRITICAL(&s_code_lock);
    if (s_code.state == FACTORY_HTTP_UPDATE_CODE_CONSUMED) {
        s_code.state = FACTORY_HTTP_UPDATE_CODE_UPDATE_RUNNING;
    }
    taskEXIT_CRITICAL(&s_code_lock);
}

void factory_http_update_code_mark_start_failed(void)
{
    taskENTER_CRITICAL(&s_code_lock);
    if (s_code.state == FACTORY_HTTP_UPDATE_CODE_CONSUMED ||
        s_code.state == FACTORY_HTTP_UPDATE_CODE_UPDATE_RUNNING) {
        s_code.state = FACTORY_HTTP_UPDATE_CODE_START_FAILED;
    }
    taskEXIT_CRITICAL(&s_code_lock);
}

void factory_http_update_code_mark_update_failed(void)
{
    taskENTER_CRITICAL(&s_code_lock);
    if (s_code.state == FACTORY_HTTP_UPDATE_CODE_UPDATE_RUNNING) {
        s_code.state = FACTORY_HTTP_UPDATE_CODE_START_FAILED;
    }
    taskEXIT_CRITICAL(&s_code_lock);
}

esp_err_t factory_http_update_code_get_snapshot(
    factory_http_update_code_snapshot_t *snapshot)
{
    if (snapshot == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    const int64_t now = esp_timer_get_time();
    taskENTER_CRITICAL(&s_code_lock);
    expire_locked(now);
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->state = s_code.state;
    if (s_code.state == FACTORY_HTTP_UPDATE_CODE_AVAILABLE) {
        memcpy(snapshot->code, s_code.code, sizeof(snapshot->code));
        const int64_t remaining_us = s_code.expires_us - now;
        snapshot->remaining_ms = remaining_us > 0
            ? (uint32_t)((remaining_us + 999) / 1000) : 0;
        snapshot->remaining_attempts =
            CONFIG_IRIS_FACTORY_HTTP_TRIGGER_MAX_ATTEMPTS -
            s_code.failed_attempts;
    }
    taskEXIT_CRITICAL(&s_code_lock);
    return ESP_OK;
}

#else

esp_err_t factory_http_update_code_generate(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

void factory_http_update_code_cancel(void)
{
}

factory_http_update_code_result_t factory_http_update_code_consume(
    const char *supplied_code)
{
    (void)supplied_code;
    return FACTORY_HTTP_UPDATE_CODE_UNAVAILABLE;
}

void factory_http_update_code_mark_running(void)
{
}

void factory_http_update_code_mark_start_failed(void)
{
}

void factory_http_update_code_mark_update_failed(void)
{
}

esp_err_t factory_http_update_code_get_snapshot(
    factory_http_update_code_snapshot_t *snapshot)
{
    if (snapshot == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->state = FACTORY_HTTP_UPDATE_CODE_DISABLED;
    return ESP_OK;
}

#endif
