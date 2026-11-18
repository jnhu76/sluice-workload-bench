// Per-job engine state (ADAPTER-ARCHITECTURE §2).
#pragma once

#include <sluice/async/async_io_context.hpp>
#include <sluice/async/request_scope.hpp>
#include <sluice/async/threadpool_backend.hpp>
#include <sluice/async/uring_backend.hpp>

#include <cstddef>
#include <cstdint>
#include <optional>
#include <unordered_map>
#include <vector>

#include "fio_cxx.hpp"

namespace fio_sluice {

enum class Backend : std::uint8_t { threadpool, uring };

struct EngineState {
    // One completion harvested but not yet delivered through event().
    struct Completion {
        io_u* u;
    };

    struct Slot {
        enum class Kind : std::uint8_t { none, read, write, sync_data, sync_all };

        io_u* u = nullptr; // borrowed while IO_U_F_FLIGHT is set
        Kind kind = Kind::none;
        sluice::async::ScopeTicket<std::size_t> data;
        sluice::async::ScopeTicket<void> sync;
    };

    Backend backend = Backend::threadpool;
    std::size_t capacity = 0; // == td->o.iodepth, no user knob

    // Declaration order is teardown order: the scope (progress owner)
    // releases before the context destructs (SHUT-04).
    std::optional<sluice::async::AsyncIoContext> context;
    std::optional<sluice::async::RequestScope> scope;

    std::vector<Slot> slots;          // exactly capacity entries
    std::vector<std::size_t> free_slots;

    std::vector<Completion> staged;   // bounded by capacity
    std::size_t staged_cursor = 0;

    // fd -> declared access, registered by open_file (LIFE-02: fd stays fio's)
    std::unordered_map<int, sluice::FileAccess> files;

    bool failed = false;

    bool outstanding() const noexcept {
        return free_slots.size() != slots.size();
    }
};

inline EngineState* state_of(struct thread_data* td) noexcept {
    return static_cast<EngineState*>(td->io_ops_data);
}

} // namespace fio_sluice
