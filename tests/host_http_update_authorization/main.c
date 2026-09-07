#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "factory_http_update_authorization.h"

static int64_t s_now_us;
static uint32_t s_random_values[32];
static size_t s_random_count;
static size_t s_random_index;

int64_t esp_timer_get_time(void)
{
    return s_now_us;
}

void esp_fill_random(void *buffer, size_t length)
{
    assert(length == sizeof(uint32_t));
    assert(s_random_index < s_random_count);
    memcpy(buffer, &s_random_values[s_random_index++], sizeof(uint32_t));
}

static void random_values(const uint32_t *values, size_t count)
{
    assert(count <= sizeof(s_random_values) / sizeof(s_random_values[0]));
    memcpy(s_random_values, values, count * sizeof(values[0]));
    s_random_count = count;
    s_random_index = 0;
}

static factory_http_update_code_snapshot_t snapshot(void)
{
    factory_http_update_code_snapshot_t value;
    assert(factory_http_update_code_get_snapshot(&value) == ESP_OK);
    return value;
}

typedef struct {
    const char *code;
    factory_http_update_code_result_t result;
} consume_thread_t;

static void *consume_thread(void *argument)
{
    consume_thread_t *context = argument;
    context->result = factory_http_update_code_consume(context->code);
    return NULL;
}

int main(void)
{
    factory_http_update_code_snapshot_t value = snapshot();
    assert(value.state == FACTORY_HTTP_UPDATE_CODE_DISABLED);

    const uint32_t first[] = {UINT32_MAX, 38271U};
    random_values(first, 2);
    assert(factory_http_update_code_generate() == ESP_OK);
    assert(s_random_index == 2);
    value = snapshot();
    assert(value.state == FACTORY_HTTP_UPDATE_CODE_AVAILABLE);
    assert(strcmp(value.code, "038271") == 0);
    assert(value.remaining_attempts == 3);
    assert(value.remaining_ms == 60000);

    assert(factory_http_update_code_consume(NULL) ==
           FACTORY_HTTP_UPDATE_CODE_REJECTED);
    assert(factory_http_update_code_consume("38271") ==
           FACTORY_HTTP_UPDATE_CODE_REJECTED);
    value = snapshot();
    assert(value.remaining_attempts == 1);
    assert(factory_http_update_code_consume("abcdef") ==
           FACTORY_HTTP_UPDATE_CODE_REJECTED);
    value = snapshot();
    assert(value.state == FACTORY_HTTP_UPDATE_CODE_LOCKED);
    assert(value.code[0] == '\0');
    assert(factory_http_update_code_consume("038271") ==
           FACTORY_HTTP_UPDATE_CODE_UNAVAILABLE);

    const uint32_t second[] = {7U};
    random_values(second, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    value = snapshot();
    assert(strcmp(value.code, "000007") == 0);
    assert(factory_http_update_code_consume("000007") ==
           FACTORY_HTTP_UPDATE_CODE_ACCEPTED);
    assert(factory_http_update_code_consume("000007") ==
           FACTORY_HTTP_UPDATE_CODE_UNAVAILABLE);
    factory_http_update_code_mark_running();
    assert(snapshot().state == FACTORY_HTTP_UPDATE_CODE_UPDATE_RUNNING);
    random_values(second, 1);
    assert(factory_http_update_code_generate() == ESP_ERR_INVALID_STATE);
    factory_http_update_code_mark_update_failed();
    assert(snapshot().state == FACTORY_HTTP_UPDATE_CODE_START_FAILED);

    const uint32_t third[] = {999999U};
    random_values(third, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    s_now_us += 60000000;
    value = snapshot();
    assert(value.state == FACTORY_HTTP_UPDATE_CODE_EXPIRED);
    assert(value.code[0] == '\0');

    const uint32_t fourth[] = {123456U};
    random_values(fourth, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    factory_http_update_code_cancel();
    assert(snapshot().state == FACTORY_HTTP_UPDATE_CODE_DISABLED);

    const uint32_t fifth[] = {654321U};
    random_values(fifth, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    consume_thread_t left = {.code = "654321"};
    consume_thread_t right = {.code = "654321"};
    pthread_t left_thread;
    pthread_t right_thread;
    assert(pthread_create(&left_thread, NULL, consume_thread, &left) == 0);
    assert(pthread_create(&right_thread, NULL, consume_thread, &right) == 0);
    assert(pthread_join(left_thread, NULL) == 0);
    assert(pthread_join(right_thread, NULL) == 0);
    assert((left.result == FACTORY_HTTP_UPDATE_CODE_ACCEPTED) !=
           (right.result == FACTORY_HTTP_UPDATE_CODE_ACCEPTED));
    assert((left.result == FACTORY_HTTP_UPDATE_CODE_UNAVAILABLE) !=
           (right.result == FACTORY_HTTP_UPDATE_CODE_UNAVAILABLE));

    puts("host HTTP Update authorization tests passed");
    return 0;
}
