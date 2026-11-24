cmake_minimum_required(VERSION 3.20)
project(s31_audio_convert_tests LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_executable(s31_audio_convert_test
    test_audio_convert.c
    ../main/audio_convert.c
)
target_include_directories(s31_audio_convert_test PRIVATE ../main)
if(MSVC)
    target_compile_options(s31_audio_convert_test PRIVATE /W4 /WX)
else()
    target_compile_options(s31_audio_convert_test PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

enable_testing()
add_test(NAME s31_audio_convert COMMAND s31_audio_convert_test)

add_executable(s31_reconnect_timer_tests test_reconnect_timer.c)
target_include_directories(s31_reconnect_timer_tests PRIVATE ../main)
if(MSVC)
    target_compile_options(s31_reconnect_timer_tests PRIVATE /W4 /WX)
else()
    target_compile_options(s31_reconnect_timer_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()
add_test(NAME s31_reconnect_timer_tests COMMAND s31_reconnect_timer_tests)
