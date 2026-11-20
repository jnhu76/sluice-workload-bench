// getevents / event (ADAPTER-ARCHITECTURE §5/§6).
//
// getevents forms the core calls with (io_u.c io_u_queued_complete /
// ioengines.c td_io_getevents):
//   min > 0, t == NULL      -- block until min completions are reaped
//   min == 0, t == {0,0}    -- poll: return immediately with whatever is ready
// A non-NULL non-zero t is treated as a relative budget from entry.
#include "errno_map.hpp"
#include <sluice/async/request_scope.hpp>

// Evaluated before any fio header (state.hpp's fio_cxx.hpp include defines
// the minmax.h function-like macros): a nanoseconds::max() call would not
// survive them.
#include <chrono>
#include <limits>

namespace fio_sluice {
namespace {
constexpr std::chrono::nanoseconds kUnboundedWait =
    std::chrono::nanoseconds::max();
}
} // namespace fio_sluice

#include "state.hpp"

#include "callbacks.hpp"

namespace fio_sluice {
namespace {

using Clock = std::chrono::steady_clock;
using Kind = sluice::async::AsyncIoContext::ProgressWaitOutcome::Kind;

// Accounting invariants must hold in release builds too: -DNDEBUG compiles
// assert() away, so invariant violations log and terminate instead of
// turning into undefined behavior (ADAPTER-ARCHITECTURE §5.1/§6.1).
[[noreturn]] void fail_closed(const char* where, const char* what) {
    log_err("fio-sluice: %s: %s; failing closed\n", where, what);
    std::terminate();
}

void complete_data_observation(io_u* u, sluice::Result<std::size_t>&& result) {
    if (result.has_value()) {
        const unsigned long long confirmed = result.value();
        u->error = 0;
        u->resid = confirmed < u->xfer_buflen ? u->xfer_buflen - confirmed : 0;
        return;
    }
    const sluice::IoError& error = result.error();
    if (is_eof(error)) {
        u->error = 0;
        u->resid = u->xfer_buflen;
        return;
    }
    const auto mapped = map_errno(error);
    if (mapped.needs_diag) {
        log_err("fio-sluice: request failed: code=%s os_errno=%d -> errno=%d\n",
                sluice::to_string(error.code).data(), error.os_errno, mapped.value);
    }
    u->error = static_cast<unsigned int>(mapped.value);
    u->resid = 0;
}

void complete_void_observation(io_u* u, const sluice::Result<void>& result) {
    u->resid = 0;
    if (result.has_value()) {
        u->error = 0;
        return;
    }
    const sluice::IoError& error = result.error();
    const auto mapped = map_errno(error);
    if (mapped.needs_diag) {
        log_err("fio-sluice: request failed: code=%s os_errno=%d -> errno=%d\n",
                sluice::to_string(error.code).data(), error.os_errno, mapped.value);
    }
    u->error = static_cast<unsigned int>(mapped.value);
}

void release_slot(EngineState* state, std::size_t index) {
    auto& slot = state->slots[index];
    slot.u = nullptr;
    slot.kind = EngineState::Slot::Kind::none;
    slot.data = {};
    slot.sync = {};
    state->free_slots.push_back(index);
}

// Non-destructive take scan: pending tickets stay put, ready tickets are
// consumed (which reclaims the scope slot), the io_u error/resid fields are
// filled before the completion is staged.
void harvest(EngineState* state, unsigned int max) {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
    ++state->research.harvest_calls;
    std::uint64_t examined = 0;
#endif
    for (std::size_t i = 0; i < state->slots.size() && state->staged.size() < max; ++i) {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
        ++examined;
#endif
        auto& slot = state->slots[i];
        if (slot.u == nullptr) {
            continue;
        }
        switch (slot.kind) {
        case EngineState::Slot::Kind::read:
        case EngineState::Slot::Kind::write: {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
            ++state->research.take_calls;
#endif
            auto observed = state->scope->take(slot.data);
            if (observed.readiness == sluice::async::RequestReadiness::pending) {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
                ++state->research.take_pending;
#endif
                break;
            }
            if (observed.readiness == sluice::async::RequestReadiness::empty) {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
                ++state->research.take_empty;
#endif
                fail_closed("getevents", "lost accepted request (ticket resolves to no slot)");
            }
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
            ++state->research.take_ready;
#endif
            complete_data_observation(slot.u, std::move(observed.result));
            state->staged.push_back(EngineState::Completion{slot.u});
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
            state->research.note_staged(state->staged.size());
#endif
            release_slot(state, i);
            break;
        }
        case EngineState::Slot::Kind::sync_data:
        case EngineState::Slot::Kind::sync_all: {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
            ++state->research.take_calls;
#endif
            auto observed = state->scope->take(slot.sync);
            if (observed.readiness == sluice::async::RequestReadiness::pending) {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
                ++state->research.take_pending;
#endif
                break;
            }
            if (observed.readiness == sluice::async::RequestReadiness::empty) {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
                ++state->research.take_empty;
#endif
                fail_closed("getevents", "lost accepted request (ticket resolves to no slot)");
            }
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
            ++state->research.take_ready;
#endif
            complete_void_observation(slot.u, observed.result);
            state->staged.push_back(EngineState::Completion{slot.u});
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
            state->research.note_staged(state->staged.size());
#endif
            release_slot(state, i);
            break;
        }
        case EngineState::Slot::Kind::none:
            break;
        }
    }
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
    state->research.harvest_slots_examined += examined;
    state->research.note_scan_length(examined);
#endif
}

// Health-failure settlement (ADAPTER-ARCHITECTURE §5.5). Tier 1: this thread
// is the context's fixed progress owner, so it drives the blocking drain
// shutdown; after a completed settlement every request keeps its real
// terminal result and is delivered like an ordinary completion. Tier 2: when
// even shutdown cannot safely retire the borrows there is no honest way to
// hand buffers back to fio or to keep them -- terminate with diagnostics.
int settle_health_failure(EngineState* state, unsigned int max, const char* where) {
    auto outcome = state->context->shutdown(sluice::async::ShutdownPolicy::drain);
    if (!outcome.has_value()) {
        fail_closed(where, "drain shutdown failed; requests cannot be safely retired");
    }
    switch (outcome.value()) {
    case sluice::async::ShutdownOutcome::completed:
        break;
    case sluice::async::ShutdownOutcome::health_unresolved:
    case sluice::async::ShutdownOutcome::health_failed:
        fail_closed(where,
                    "drain shutdown settled without a safe outcome; outstanding borrows"
                    " must not be released");
    }
    state->failed = true;
    harvest(state, max);
    if (state->staged.size() < max && state->outstanding()) {
        fail_closed(where, "request stayed non-terminal after a completed drain shutdown");
    }
    return static_cast<int>(state->staged.size());
}

} // namespace

int sluice_getevents(struct thread_data* td, unsigned int min, unsigned int max,
                     const struct timespec* t) {
    auto* state = state_of(td);
    if (state == nullptr) {
        fail_closed("getevents", "engine state missing (init failed or skipped)");
    }
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
    ++state->research.getevents_calls;
#endif
    if (state->staged_cursor != state->staged.size()) {
        fail_closed("getevents", "previous completion batch was not fully consumed by event()");
    }
    state->staged.clear();
    state->staged_cursor = 0;
    if (max == 0) {
        return 0;
    }
    if (min > max) {
        min = max;
    }

    const bool unbounded = t == nullptr;
    std::chrono::nanoseconds budget{0};
    if (!unbounded) {
        budget = std::chrono::seconds(t->tv_sec) + std::chrono::nanoseconds(t->tv_nsec);
    }
    const bool zero_poll = !unbounded && budget.count() == 0;
    std::optional<Clock::time_point> deadline;
    if (!unbounded) {
        deadline = Clock::now() + std::chrono::duration_cast<Clock::duration>(budget);
    }
    const auto remaining = [&]() -> std::chrono::nanoseconds {
        if (!deadline.has_value()) {
            return kUnboundedWait;
        }
        return std::chrono::duration_cast<std::chrono::nanoseconds>(*deadline - Clock::now());
    };

    try {
        // The zero-budget poll form performs at most one bounded progress
        // pass so it still reaps completions an idle owner owes, and never
        // parks (ADAPTER-ARCHITECTURE §5.1).
        bool drove_zero_poll = false;
        for (;;) {
            harvest(state, max);
            if (state->staged.size() >= min || state->staged.size() == max) {
                return static_cast<int>(state->staged.size());
            }
            if (state->failed) {
                // Everything outstanding was delivered at settlement; a
                // shut-down context must not be driven again.
                return static_cast<int>(state->staged.size());
            }
            const bool expired = !unbounded && remaining().count() <= 0;
            if (expired && !(zero_poll && !drove_zero_poll)) {
                return static_cast<int>(state->staged.size());
            }
            drove_zero_poll = true;

            auto pass = state->context->poll_progress();
            if (!pass.has_value() || pass.value().health_failed) {
                return settle_health_failure(state, max, "getevents poll_progress");
            }
            if (pass.value().completed > 0 || pass.value().immediate_work_remains ||
                pass.value().dispatch_retry_remains) {
                // The owner still owes bounded driver work; parking here
                // would strand it (PROG-01).
                continue;
            }
            if (expired) {
                return static_cast<int>(state->staged.size());
            }

            const auto wait_budget = remaining();
            auto woke = wait_budget == kUnboundedWait
                ? state->context->wait_one()
                : state->context->wait_one(wait_budget);
            if (!woke.has_value() || woke.value().kind == Kind::health_failure) {
                return settle_health_failure(state, max, "getevents wait_one");
            }
            if (woke.value().kind == Kind::control_interrupted) {
                // Sticky owner control; retire before waiting again (PROG-04).
                state->context->acknowledge_progress_control();
            }
            // progress / deadline_expired: loop; the harvest-and-gate order
            // re-checks completions before any deadline exit, so an expiry
            // never strands a completion (PROG-04: expiry cancels nothing).
        }
    } catch (const std::exception& error) {
        fail_closed("getevents", error.what());
    } catch (...) {
        fail_closed("getevents", "unknown exception");
    }
}

struct io_u* sluice_event(struct thread_data* td, int i) {
    auto* state = state_of(td);
    if (state == nullptr) {
        fail_closed("event", "engine state missing (init failed or skipped)");
    }
    if (i < 0 || static_cast<std::size_t>(i) >= state->staged.size()) {
        fail_closed("event", "index beyond the staged completion batch");
    }
    ++state->staged_cursor;
    // Never nullptr: the core dereferences the return value without a null
    // check (io_u.c io_completed).
    return state->staged[static_cast<std::size_t>(i)].u;
}

} // namespace fio_sluice
