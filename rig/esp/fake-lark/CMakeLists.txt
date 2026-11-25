cmake_minimum_required(VERSION 3.13)

include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(fake_lark C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

add_executable(fake_lark
    src/main.c
    src/usb_descriptors.c
    src/uac1_driver.c
    src/audio_gen.c
    src/uart_ctl.c
)

target_include_directories(fake_lark PRIVATE ${CMAKE_CURRENT_LIST_DIR}/src)
target_compile_options(fake_lark PRIVATE -Wall -Wextra -Wno-unused-parameter)

target_link_libraries(fake_lark
    pico_stdlib
    hardware_uart
    tinyusb_device
    tinyusb_board
)

pico_enable_stdio_uart(fake_lark 0)
pico_enable_stdio_usb(fake_lark 0)

