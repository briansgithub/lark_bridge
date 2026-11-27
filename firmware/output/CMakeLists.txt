cmake_minimum_required(VERSION 3.16)

set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../common")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(larkbridge_output)

# Car UX feature 3C: AVRCP 1.3 TG metadata. Patch a build-directory copy of
# Bluedroid's btc_avrc.c (anchors asserted by the script) and compile it in
# place of the original inside __idf_bt. The IDF tree is never modified.
set(LB_BTC_AVRC "$ENV{IDF_PATH}/components/bt/host/bluedroid/btc/profile/std/avrc/btc_avrc.c")
set(LB_BTC_AVRC_PATCHED "${CMAKE_BINARY_DIR}/patched_btc_avrc.c")
find_package(Python3 REQUIRED COMPONENTS Interpreter)
execute_process(
    COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_LIST_DIR}/tools/patch_btc_avrc.py"
            "${LB_BTC_AVRC}" "${LB_BTC_AVRC_PATCHED}"
    RESULT_VARIABLE LB_PATCH_RESULT
    OUTPUT_VARIABLE LB_PATCH_OUTPUT
    ERROR_VARIABLE LB_PATCH_ERROR)
if(NOT LB_PATCH_RESULT EQUAL 0)
    message(FATAL_ERROR "patch_btc_avrc.py failed: ${LB_PATCH_OUTPUT}${LB_PATCH_ERROR}")
endif()
message(STATUS "${LB_PATCH_OUTPUT}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
             "${CMAKE_CURRENT_LIST_DIR}/tools/patch_btc_avrc.py" "${LB_BTC_AVRC}")
get_target_property(LB_BT_SOURCES __idf_bt SOURCES)
set(LB_BTC_AVRC_INDEX -1)
set(LB_INDEX 0)
foreach(LB_SRC IN LISTS LB_BT_SOURCES)
    if(LB_SRC MATCHES "btc/profile/std/avrc/btc_avrc\.c$")
        set(LB_BTC_AVRC_INDEX ${LB_INDEX})
    endif()
    math(EXPR LB_INDEX "${LB_INDEX} + 1")
endforeach()
if(LB_BTC_AVRC_INDEX EQUAL -1)
    message(FATAL_ERROR "btc_avrc.c not found in __idf_bt sources")
endif()
list(GET LB_BT_SOURCES ${LB_BTC_AVRC_INDEX} LB_BTC_AVRC_ORIGINAL)
message(STATUS "LarkBridge: replacing ${LB_BTC_AVRC_ORIGINAL} with ${LB_BTC_AVRC_PATCHED}")
list(REMOVE_AT LB_BT_SOURCES ${LB_BTC_AVRC_INDEX})
list(APPEND LB_BT_SOURCES "${LB_BTC_AVRC_PATCHED}")
set_property(TARGET __idf_bt PROPERTY SOURCES ${LB_BT_SOURCES})
