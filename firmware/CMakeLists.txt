cmake_minimum_required(VERSION 3.13)

# Register local board headers (including waveshare_rp2350b_plus_w) before SDK init.
list(APPEND PICO_BOARD_HEADER_DIRS "${CMAKE_CURRENT_LIST_DIR}/boards")

include($ENV{PICO_SDK_PATH}/pico_sdk_init.cmake)
project(pwmvid C CXX ASM)
pico_sdk_init()

# Board family: enable at most one (see firmware/board.h).
#   RP2350A  = Official Pico 2 / Pico 2 W   (ADC on GP26-GP29 only)
#   RP2350B  = Waveshare RP2350B-Plus-W     (ADC on GP40-GP47, GP42=ADC2) <- default
option(BOARD_RP2350A "Build for RP2350A (Official Pico 2 / Pico 2 W)" OFF)
option(BOARD_RP2350B "Build for RP2350B (Waveshare RP2350B-Plus-W)" OFF)

# DEBUG: budget oscilloscope build — 400 MHz overclock + 1.50 V VREG + NVVDD ADC trace.
# WARNING: 1.50 V DVDD EXCEEDS the RP2350 datasheet absolute maximum of 1.21 V!
# Normal bench runs MUST use the stock 150 MHz build (-DDEBUG=OFF).
option(DEBUG "Debug build: 400 MHz OC + 1.50V VREG + NVVDD ADC trace (UNSAFE OVERVOLTAGE)" OFF)
option(ALLOW_UNSAFE_OVERCLOCK_1V5 "Explicitly acknowledge 1.50V DVDD > 1.21V RP2350 abs max for DEBUG builds" OFF)
option(ALLOW_PICO2W_BOARD_HEADER "Allow mismatched pico2/pico2_w SDK board header with BOARD_RP2350B (stock build only)" OFF)

if(BOARD_RP2350A AND BOARD_RP2350B)
    message(FATAL_ERROR "enable only one of BOARD_RP2350A / BOARD_RP2350B")
endif()

if(BOARD_RP2350A)
    set(BOARD_DEF BOARD_RP2350A)
elseif(BOARD_RP2350B)
    set(BOARD_DEF BOARD_RP2350B)
else()
    set(BOARD_DEF BOARD_RP2350B)
    message(STATUS "pico-ga100vfi: no family given, defaulting to RP2350B")
endif()
message(STATUS "pico-ga100vfi: board family = ${BOARD_DEF} (PICO_BOARD=${PICO_BOARD})")

# Guard against RP2350A/B SDK header mismatch (pico2/pico2_w declares PICO_RP2350A=1 & 4 MiB flash).
if(BOARD_DEF STREQUAL "BOARD_RP2350B" AND (PICO_BOARD STREQUAL "pico2" OR PICO_BOARD STREQUAL "pico2_w"))
    if(DEBUG)
        message(FATAL_ERROR
            "BOARD_RP2350B with DEBUG=ON cannot use PICO_BOARD=${PICO_BOARD} "
            "(declares PICO_RP2350A=1, breaking GP42 ADC assertions). "
            "Use -DPICO_BOARD=waveshare_rp2350b_plus_w.")
    elseif(NOT ALLOW_PICO2W_BOARD_HEADER)
        message(FATAL_ERROR
            "BOARD_RP2350B mismatches PICO_BOARD=${PICO_BOARD} (RP2350A, 4 MiB Flash). "
            "Use -DPICO_BOARD=waveshare_rp2350b_plus_w (included in firmware/boards/) "
            "for Waveshare RP2350B-Plus-W (RP2350B QFN-80, 16 MiB Flash), "
            "or pass -DALLOW_PICO2W_BOARD_HEADER=ON only if you explicitly want the legacy header.")
    endif()
endif()

set(BOARD_DEFS ${BOARD_DEF})
if(DEBUG)
    if(NOT ALLOW_UNSAFE_OVERCLOCK_1V5)
        message(FATAL_ERROR
            "REFUSED -DDEBUG=ON: firmware/pwmvid.c disables the RP2350 VREG limit and requests "
            "1.50 V DVDD, exceeding the RP2350 datasheet absolute maximum of 1.21 V even with the "
            "GPU disconnected. Use the stock 150 MHz build (-DDEBUG=OFF) for bench runs, or pass "
            "-DALLOW_UNSAFE_OVERCLOCK_1V5=ON only if you explicitly accept silicon overvoltage risk.")
    endif()
    list(APPEND BOARD_DEFS DEBUG)
    message(WARNING "pico-ga100vfi: DEBUG build ENABLED with 1.50 V DVDD (> 1.21 V RP2350 abs max)!")
else()
    message(STATUS "pico-ga100vfi: normal build (stock 150 MHz clocks, stock VREG, no ADC)")
endif()

add_executable(pwmvid pwmvid.c)
target_compile_definitions(pwmvid PRIVATE ${BOARD_DEFS})
target_link_libraries(pwmvid
    pico_stdlib
    hardware_adc
    hardware_dma
    hardware_clocks
    hardware_vreg
    hardware_i2c
    pico_multicore
    hardware_watchdog
)
pico_enable_stdio_usb(pwmvid 1)
pico_enable_stdio_uart(pwmvid 0)
pico_add_extra_outputs(pwmvid)

# Standalone I2C detection tool (stock clocks; see detect.c).
add_executable(detect detect.c)
target_compile_definitions(detect PRIVATE ${BOARD_DEF})
target_link_libraries(detect
    pico_stdlib
    hardware_i2c
)
pico_enable_stdio_usb(detect 1)
pico_enable_stdio_uart(detect 0)
pico_add_extra_outputs(detect)
