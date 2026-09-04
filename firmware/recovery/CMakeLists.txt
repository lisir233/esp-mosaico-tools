cmake_minimum_required(VERSION 3.16)

# `mosaico.py recover` passes this standard ESP-IDF variable with `-D`.
# Accepting the same name from the environment also keeps low-noise and
# standalone build runners independent of workspace-specific variable names.
if(NOT DEFINED EXTRA_COMPONENT_DIRS AND DEFINED ENV{EXTRA_COMPONENT_DIRS})
    set(EXTRA_COMPONENT_DIRS "$ENV{EXTRA_COMPONENT_DIRS}")
endif()

set(RECOVERY_PREBUILT_DIR
    "${CMAKE_CURRENT_LIST_DIR}/prebuilt/recovery" CACHE PATH
    "Directory containing the checked-in recovery image and manifest")

if(NOT SDKCONFIG)
    set(SDKCONFIG "${CMAKE_BINARY_DIR}/sdkconfig")
endif()

if(NOT SDKCONFIG_DEFAULTS)
    set(SDKCONFIG_DEFAULTS
        "sdkconfig.defaults;sdkconfig.recovery.defaults")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(factory)

include(cmake/recovery_image.cmake)
