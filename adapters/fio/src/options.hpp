// Engine-private fio options (ADAPTER-ARCHITECTURE §3.8).
#pragma once

#include <cstddef>

#include "fio_cxx.hpp"

namespace fio_sluice {

struct EngineOptions {
    // fio core stores td in the first pointer field (init.c:1276); off1 of
    // any real option must never be 0.
    void* pad;
    char* backend;        // --sluice_backend: threadpool (default) | uring
    unsigned int workers; // --sluice_workers: threadpool worker count
};

extern struct fio_option engine_options[];

} // namespace fio_sluice
