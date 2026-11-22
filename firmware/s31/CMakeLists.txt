cmake_minimum_required(VERSION 3.20)

set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../common")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(larkbridge_s31_gateway)

# ESP-IDF v6.1-rc1's preview S31 Bluedroid sources trigger GCC 15's
# stringop-truncation diagnostic. Keep -Werror for this project and relax only
# that known upstream warning on the SDK Bluetooth component.
idf_component_get_property(bt_component bt COMPONENT_LIB)
target_compile_options(${bt_component} PRIVATE -Wno-error=stringop-truncation)
