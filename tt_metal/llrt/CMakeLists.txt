set(LLRT_SRC
    ${CMAKE_CURRENT_SOURCE_DIR}/llrt.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/rtoptions.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/tlb_config.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/tt_cluster.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/tt_elffile.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/tt_memory.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/hal.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/grayskull/gs_hal.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/wormhole/wh_hal.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/wormhole/wh_hal_tensix.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/wormhole/wh_hal_active_eth.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/wormhole/wh_hal_idle_eth.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/blackhole/bh_hal.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/blackhole/bh_hal_tensix.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/blackhole/bh_hal_active_eth.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/blackhole/bh_hal_idle_eth.cpp
)

add_library(llrt OBJECT ${LLRT_SRC})
target_link_libraries(
    llrt
    PUBLIC
        common
        umd::device
)
target_compile_options(llrt PRIVATE -Wno-int-to-pointer-cast)
