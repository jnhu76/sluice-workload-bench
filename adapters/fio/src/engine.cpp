// ioengine_ops table, engine options, init/cleanup (ADAPTER-ARCHITECTURE §3/§4.1/§4.8).
#include "state.hpp"

#include <utility>

#include "callbacks.hpp"
#include "options.hpp"

#ifndef SLUICE_REVISION
#define SLUICE_REVISION "unknown"
#endif
#ifndef SLUICE_BUILD_PROFILE
#define SLUICE_BUILD_PROFILE "unknown"
#endif
#ifndef FIO_SOURCE_TAG
#define FIO_SOURCE_TAG "unknown"
#endif

#ifdef SLUICE_FIO_INIT_SEAM
// Test-only: the lifecycle test defines this and throws at a chosen
// init step to prove allocation failures cannot escape the C callback.
// Compiled out of both shipped .so profiles.
extern "C" void fio_sluice_init_seam(int step);
#define FIO_SLUICE_INIT_SEAM(step) fio_sluice_init_seam(step)
#else
#define FIO_SLUICE_INIT_SEAM(step) ((void)0)
#endif

namespace fio_sluice {
namespace {

constexpr char kBackendThreadpool[] = "threadpool";
constexpr char kBackendUring[] = "uring";
constexpr unsigned int kDefaultWorkers = 4;

int reject_init(struct thread_data* td, const char* reason) {
    log_err("fio-sluice: init rejected: %s\n", reason);
    return 1;
}

} // namespace

struct fio_option engine_options[] = {
    {
        .name = "sluice_backend",
        .lname = "Sluice backend selection",
        .type = FIO_OPT_STR_STORE,
        .off1 = offsetof(struct EngineOptions, backend),
        .help = "Sluice async backend: threadpool or uring",
        .def = "threadpool",
        .category = FIO_OPT_C_ENGINE,
        .group = FIO_OPT_G_INVALID,
    },
    {
        .name = "sluice_workers",
        .lname = "Sluice threadpool worker count",
        .type = FIO_OPT_INT,
        .off1 = offsetof(struct EngineOptions, workers),
        .minval = 1,
        .help = "threadpool worker count (threadpool backend only)",
        .def = "4",
        .category = FIO_OPT_C_ENGINE,
        .group = FIO_OPT_G_INVALID,
    },
    {
        .name = NULL,
    },
};

int sluice_init(struct thread_data* td) {
    if (td->o.verify_async) {
        return reject_init(td, "verify_async queues from a non-owner thread; not supported");
    }
    auto* eo = static_cast<EngineOptions*>(td->eo);

    Backend backend;
    const char* wanted = eo->backend != nullptr ? eo->backend : kBackendThreadpool;
    if (std::strcmp(wanted, kBackendThreadpool) == 0) {
        backend = Backend::threadpool;
    } else if (std::strcmp(wanted, kBackendUring) == 0) {
        backend = Backend::uring;
    } else {
        log_err("fio-sluice: invalid sluice_backend \"%s\" (expected threadpool or uring)\n",
                wanted);
        return 1;
    }

    const std::size_t capacity = td->o.iodepth;
    if (capacity == 0) {
        return reject_init(td, "iodepth must be nonzero");
    }
    if (backend == Backend::threadpool && eo->workers == 0) {
        return reject_init(td, "sluice_workers must be >= 1 for the threadpool backend");
    }
    if (backend == Backend::uring && eo->workers != kDefaultWorkers) {
        return reject_init(td, "sluice_workers applies to the threadpool backend only");
    }

    // One try covers every throwing init step: an allocation failure must
    // never escape the C callback into fio. The unique_ptr owns the state
    // until success; EngineState's declaration order tears the scope down
    // before the context on rollback (SHUT-04).
    std::unique_ptr<EngineState> state;
    try {
        FIO_SLUICE_INIT_SEAM(0);
        state = std::make_unique<EngineState>();
        if (backend == Backend::threadpool) {
            state->context.emplace(sluice::async::make_threadpool_context(
                {.request_capacity = capacity, .worker_count = eo->workers}));
        } else {
            auto made = sluice::async::make_uring_context(
                {.request_capacity = capacity, .queue_depth = static_cast<unsigned>(capacity)});
            if (!made.has_value()) {
                const auto& error = made.error();
                if (error.code == sluice::IoError::Code::not_supported) {
                    log_err("fio-sluice: io_uring unavailable: engine built from the %s"
                            " SDK profile; rebuild with the ring profile\n",
                            SLUICE_BUILD_PROFILE);
                } else {
                    log_err("fio-sluice: io_uring context rejected: code=%s os_errno=%d\n",
                            sluice::to_string(error.code).data(), error.os_errno);
                }
                return 1;
            }
            state->context.emplace(std::move(made).value());
        }
        state->scope.emplace(*state->context, capacity,
                             sluice::async::ScopeCleanupPolicy::drain);
        state->backend = backend;
        state->capacity = capacity;
        FIO_SLUICE_INIT_SEAM(1);
        state->slots.resize(capacity);
        state->free_slots.reserve(capacity);
        for (std::size_t i = 0; i < capacity; ++i) {
            state->free_slots.push_back(capacity - 1 - i);
        }
        FIO_SLUICE_INIT_SEAM(2);
        state->staged.reserve(capacity);
    } catch (const std::exception& error) {
        log_err("fio-sluice: init failed: %s\n", error.what());
        return 1;
    } catch (...) {
        log_err("fio-sluice: init failed: unknown exception\n");
        return 1;
    }

    log_err("fio-sluice: loaded revision=%s sdk_profile=%s fio_source=%s backend=%s"
            " iodepth=%zu workers=%u\n",
            SLUICE_REVISION, SLUICE_BUILD_PROFILE, FIO_SOURCE_TAG, wanted, capacity,
            eo->workers);
    td->io_ops_data = state.release();
    return 0;
}

void sluice_cleanup(struct thread_data* td) {
    auto* state = state_of(td);
    if (state == nullptr) {
        return;
    }
    if (state->staged_cursor != state->staged.size()) {
        log_err("fio-sluice: %zu staged completions were never consumed by fio\n",
                state->staged.size() - state->staged_cursor);
    }
    if (!state->failed && state->scope.has_value()) {
        const auto settled = state->scope->finish();
        if (!settled.has_value()) {
            log_err("fio-sluice: scope settlement error: code=%s os_errno=%d\n",
                    sluice::to_string(settled.error().code).data(), settled.error().os_errno);
        }
    }
    if (state->outstanding()) {
        log_err("fio-sluice: cleanup reached with %zu request(s) still outstanding\n",
                state->slots.size() - state->free_slots.size());
    }
    delete state;
    td->io_ops_data = nullptr;
}

} // namespace fio_sluice

// Exported for dlsym resolution from the host fio process. The variable is
// not mangled, so no extern "C" is needed (ABIQ §4.5); the visibility
// attribute keeps it exported under -fvisibility=hidden release defaults.
__attribute__((visibility("default"))) struct ioengine_ops ioengine = {
    .name = "sluice",
    .version = FIO_IOOPS_VERSION,
    .flags = FIO_NO_OFFLOAD,
    .init = fio_sluice::sluice_init,
    .queue = fio_sluice::sluice_queue,
    .commit = fio_sluice::sluice_commit,
    .getevents = fio_sluice::sluice_getevents,
    .event = fio_sluice::sluice_event,
    .cleanup = fio_sluice::sluice_cleanup,
    .open_file = fio_sluice::sluice_open_file,
    .close_file = fio_sluice::sluice_close_file,
    .get_file_size = fio_sluice::sluice_get_file_size,
    .option_struct_size = sizeof(struct fio_sluice::EngineOptions),
    .options = fio_sluice::engine_options,
};
