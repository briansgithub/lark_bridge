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
