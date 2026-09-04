cmake_minimum_required(VERSION 3.16)

foreach(component_path_var MOSAICO_BSP_PATH MOSAICO_ESP_IRIS_PATH)
    if(NOT DEFINED ${component_path_var} AND DEFINED ENV{${component_path_var}})
        set(${component_path_var} "$ENV{${component_path_var}}")
    endif()
    if(NOT DEFINED ${component_path_var})
        message(FATAL_ERROR
            "${component_path_var} must be provided by mosaico.py recover")
    endif()
endforeach()

set(mosaico_bsp_component
    "${MOSAICO_BSP_PATH}/components/esp-mosaico-bsp")
set(mosaico_esp_iris_component
    "${MOSAICO_ESP_IRIS_PATH}/components/esp_iris")
foreach(component_path ${mosaico_bsp_component} ${mosaico_esp_iris_component})
    if(NOT EXISTS "${component_path}/CMakeLists.txt")
        message(FATAL_ERROR "Required Recovery component is unavailable: ${component_path}")
    endif()
endforeach()
list(APPEND EXTRA_COMPONENT_DIRS
    "${mosaico_bsp_component}"
    "${mosaico_esp_iris_component}")

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
