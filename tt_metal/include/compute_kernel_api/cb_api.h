#pragma once

#define SYNC SyncHalf

#include "compute_kernel_api/llk_includes.h"

namespace ckernel {

// documented in dataflow_api.h
ALWI void cb_wait_front(uint32_t cbid, uint32_t ntiles) {
    UNPACK(( llk_wait_tiles(cbid, ntiles)  ));
}

// documented in dataflow_api.h
ALWI void cb_pop_front(uint32_t cbid, uint32_t ntiles) {
    UNPACK(( llk_pop_tiles(cbid, ntiles)  ));
}


// documented in dataflow_api.h
ALWI void cb_reserve_back(uint32_t cbid, uint32_t ntiles)
{
    PACK(( llk_wait_for_free_tiles<false,false,false>(cbid,ntiles)  ));
}


// documented in dataflow_api.h
ALWI void cb_push_back(uint32_t cbid, uint32_t ntiles)
{
    PACK(( llk_push_tiles<false,false>(cbid, ntiles)  ));
}


} // namespace ckernel
