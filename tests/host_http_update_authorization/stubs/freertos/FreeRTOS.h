#pragma once

#include <pthread.h>

typedef pthread_mutex_t portMUX_TYPE;

#define portMUX_INITIALIZER_UNLOCKED PTHREAD_MUTEX_INITIALIZER
#define taskENTER_CRITICAL(lock) pthread_mutex_lock(lock)
#define taskEXIT_CRITICAL(lock) pthread_mutex_unlock(lock)
