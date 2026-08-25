add_executable(bridge_common_tests test_common.c)
target_link_libraries(bridge_common_tests PRIVATE bridge_common)

if(MSVC)
    target_compile_options(bridge_common_tests PRIVATE /W4 /WX /experimental:c11atomics)
else()
    target_compile_options(bridge_common_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

add_test(NAME bridge_common_tests COMMAND bridge_common_tests)
