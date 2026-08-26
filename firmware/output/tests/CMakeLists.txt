cmake_minimum_required(VERSION 3.20)
project(larkbridge_output_policy_tests LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

add_executable(output_policy_tests
    test_output_policy.c
    ../main/output_policy.c
)
target_include_directories(output_policy_tests PRIVATE
    ../main
    ../../common/include
)

if(MSVC)
    target_compile_options(output_policy_tests PRIVATE /W4 /WX)
else()
    target_compile_options(output_policy_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

enable_testing()
add_test(NAME output_policy_tests COMMAND output_policy_tests)
