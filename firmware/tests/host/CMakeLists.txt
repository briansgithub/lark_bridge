add_executable(bridge_common_tests test_common.c)
target_link_libraries(bridge_common_tests PRIVATE bridge_common)

if(MSVC)
    target_compile_options(bridge_common_tests PRIVATE /W4 /WX /experimental:c11atomics)
else()
    target_compile_options(bridge_common_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

add_test(NAME bridge_common_tests COMMAND bridge_common_tests)

add_executable(bridge_link_campaign test_link_campaign.c)
target_link_libraries(bridge_link_campaign PRIVATE bridge_common)

if(MSVC)
    target_compile_options(bridge_link_campaign PRIVATE /W4 /WX /experimental:c11atomics)
else()
    target_compile_options(bridge_link_campaign PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

add_test(NAME bridge_link_campaign COMMAND bridge_link_campaign)

add_executable(bridge_console_tests test_console.c)
target_link_libraries(bridge_console_tests PRIVATE bridge_common)

if(MSVC)
    target_compile_options(bridge_console_tests PRIVATE /W4 /WX /experimental:c11atomics)
else()
    target_compile_options(bridge_console_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

add_test(NAME bridge_console_tests COMMAND bridge_console_tests)

add_executable(bridge_fault_frame_tests test_fault_frames.c)
target_link_libraries(bridge_fault_frame_tests PRIVATE bridge_common)

if(MSVC)
    target_compile_options(bridge_fault_frame_tests PRIVATE /W4 /WX /experimental:c11atomics)
else()
    target_compile_options(bridge_fault_frame_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

add_test(NAME bridge_fault_frame_tests COMMAND bridge_fault_frame_tests)

add_executable(bridge_tap_tests test_tap.c)
target_link_libraries(bridge_tap_tests PRIVATE bridge_common)
if(MSVC)
    target_compile_options(bridge_tap_tests PRIVATE /W4 /WX /experimental:c11atomics)
else()
    target_compile_options(bridge_tap_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()
add_test(NAME bridge_tap_tests COMMAND bridge_tap_tests)

add_executable(bridge_meta_tests test_meta.c)
target_link_libraries(bridge_meta_tests PRIVATE bridge_common)
if(MSVC)
    target_compile_options(bridge_meta_tests PRIVATE /W4 /WX /experimental:c11atomics)
else()
    target_compile_options(bridge_meta_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()
add_test(NAME bridge_meta_tests COMMAND bridge_meta_tests)
