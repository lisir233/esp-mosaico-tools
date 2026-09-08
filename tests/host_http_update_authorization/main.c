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

typedef struct {
    factory_http_update_code_snapshot_t snapshot;
} snapshot_thread_t;

static void *consume_thread(void *argument)
{
    consume_thread_t *context = argument;
    context->result = factory_http_update_code_consume(context->code);
    return NULL;
}

static void *snapshot_thread(void *argument)
{
    snapshot_thread_t *context = argument;
    assert(factory_http_update_code_get_snapshot(&context->snapshot) == ESP_OK);
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
    assert(value.remaining_ms == 180000);

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
    assert(value.remaining_ms == 180000);
    assert(factory_http_update_code_consume("038271") ==
           FACTORY_HTTP_UPDATE_CODE_UNAVAILABLE);

    /* A locked interval automatically rotates at its original deadline. */
    const uint32_t second[] = {7U};
    random_values(second, 1);
    s_now_us += 180000000;
    value = snapshot();
    assert(s_random_index == 1);
    assert(value.state == FACTORY_HTTP_UPDATE_CODE_AVAILABLE);
    assert(strcmp(value.code, "000007") == 0);
    assert(value.remaining_ms == 180000);
    assert(value.remaining_attempts == 3);
    assert(factory_http_update_code_consume("038271") ==
           FACTORY_HTTP_UPDATE_CODE_REJECTED);
    value = snapshot();
    assert(value.remaining_attempts == 2);
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

    /* An available code rotates exactly at the configured interval. */
    const uint32_t third[] = {999999U};
    random_values(third, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    s_now_us += 179999999;
    value = snapshot();
    assert(strcmp(value.code, "999999") == 0);
    assert(value.remaining_ms == 1);

    const uint32_t fourth[] = {123456U};
    random_values(fourth, 1);
    ++s_now_us;
    assert(factory_http_update_code_consume("999999") ==
           FACTORY_HTTP_UPDATE_CODE_REJECTED);
    value = snapshot();
    assert(value.state == FACTORY_HTTP_UPDATE_CODE_AVAILABLE);
    assert(strcmp(value.code, "123456") == 0);
    assert(value.remaining_ms == 180000);
    assert(value.remaining_attempts == 2);

    /* Manual generation starts a fresh interval and invalidates the old code. */
    s_now_us += 10000000;
    const uint32_t fifth[] = {654321U};
    random_values(fifth, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    value = snapshot();
    assert(strcmp(value.code, "654321") == 0);
    assert(value.remaining_ms == 180000);
    assert(factory_http_update_code_consume("123456") ==
           FACTORY_HTTP_UPDATE_CODE_REJECTED);
    assert(factory_http_update_code_consume("654321") ==
           FACTORY_HTTP_UPDATE_CODE_ACCEPTED);
    factory_http_update_code_mark_start_failed();

    /* Cancelling the page stops all future rotations. */
    const uint32_t sixth[] = {222222U};
    random_values(sixth, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    factory_http_update_code_cancel();
    assert(snapshot().state == FACTORY_HTTP_UPDATE_CODE_DISABLED);
    s_now_us += 180000000;
    assert(snapshot().state == FACTORY_HTTP_UPDATE_CODE_DISABLED);
    assert(s_random_index == 1);

    /* Exactly one concurrent consumer can admit an update. */
    const uint32_t seventh[] = {444444U};
    random_values(seventh, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    consume_thread_t left = {.code = "444444"};
    consume_thread_t right = {.code = "444444"};
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

    /* Consumed/running/failed authorization never rotates itself back open. */
    factory_http_update_code_mark_running();
    factory_http_update_code_mark_update_failed();
    s_now_us += 180000000;
    assert(snapshot().state == FACTORY_HTTP_UPDATE_CODE_START_FAILED);

    /* Concurrent observers claim only one rotation and publish one new code. */
    const uint32_t eighth[] = {555555U};
    random_values(eighth, 1);
    assert(factory_http_update_code_generate() == ESP_OK);
    s_now_us += 180000000;
    const uint32_t ninth[] = {666666U};
    random_values(ninth, 1);
    snapshot_thread_t first_snapshot = {0};
    snapshot_thread_t second_snapshot = {0};
    pthread_t first_thread;
    pthread_t second_thread;
    assert(pthread_create(&first_thread, NULL, snapshot_thread,
                          &first_snapshot) == 0);
    assert(pthread_create(&second_thread, NULL, snapshot_thread,
                          &second_snapshot) == 0);
    assert(pthread_join(first_thread, NULL) == 0);
    assert(pthread_join(second_thread, NULL) == 0);
    assert(s_random_index == 1);
    value = snapshot();
    assert(value.state == FACTORY_HTTP_UPDATE_CODE_AVAILABLE);
    assert(strcmp(value.code, "666666") == 0);
    assert(value.remaining_attempts == 3);

    puts("host HTTP Update authorization tests passed");
    return 0;
}
