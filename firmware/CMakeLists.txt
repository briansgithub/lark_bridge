cmake_minimum_required(VERSION 3.20)
project(larkbridge_firmware_core LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

add_library(bridge_common STATIC
    common/src/bridge_ring.c
    common/src/bridge_pcm.c
    common/src/bridge_link.c
    common/src/bridge_protocol.c
    common/src/bridge_rate.c
    common/src/bridge_session.c
)
target_include_directories(bridge_common PUBLIC common/include)

if(MSVC)
    target_compile_options(bridge_common PRIVATE /W4 /WX /experimental:c11atomics)
else()
    target_compile_options(bridge_common PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

enable_testing()
add_subdirectory(tests/host)
