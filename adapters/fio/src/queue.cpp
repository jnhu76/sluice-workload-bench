// queue / commit (ADAPTER-ARCHITECTURE §4.3-§4.5).
//
// queue() return domain is {FIO_Q_QUEUED, FIO_Q_COMPLETED}:
// - QUEUED: Sluice accepted the request; the completion is delivered exactly
//   once through getevents()/event().
// - COMPLETED (with io_u->error set): the request was NOT accepted (invalid
//   ddir, unregistered fd, submit rejection). Submitting nothing is honest
//   here; INV-02 makes rejection side-effect free.
// FIO_Q_BUSY is unreachable: capacity == td->o.iodepth is an identity, so a
// free slot exists whenever fio can call queue (fio caps in-flight io_us at
// iodepth via its freelist); non-transient submit errors (admission closed,
// health, invalid_state) must not map to BUSY because retrying can never
// succeed while getevents(min>0) would block on completions that cannot
// appear -- that would be a retry deadlock, so they fail closed instead.
#include "errno_map.hpp"
#include "state.hpp"

#include "callbacks.hpp"

namespace fio_sluice {

int sluice_commit(struct thread_data* td) {
    // Mark only what this engine accepted since the last commit, then clear:
    // the core's td->io_u_queued is unusable here (ghost/double counting;
    // see state.hpp). A commit with nothing pending is a no-op, like the
    // sync engine's guard (engines/sync.c:380-381).
    auto* state = state_of(td);
    if (state != nullptr && state->pending_submits != 0) {
        io_u_mark_submit(td, state->pending_submits);
        state->pending_submits = 0;
    }
    return 0;
}

enum fio_q_status sluice_queue(struct thread_data* td, struct io_u* io_u) {
    fio_ro_check(td, io_u);

    auto* state = state_of(td);
    if (state == nullptr) {
        log_err("fio-sluice: queue before engine init\n");
        io_u->error = EIO;
        return FIO_Q_COMPLETED;
    }
    if (state->failed) {
        io_u->error = EIO;
        return FIO_Q_COMPLETED;
    }

    const enum fio_ddir ddir = io_u->ddir;
    if (ddir != DDIR_READ && ddir != DDIR_WRITE && ddir != DDIR_SYNC && ddir != DDIR_DATASYNC) {
        log_err("fio-sluice: ddir %d is not supported\n", static_cast<int>(ddir));
        io_u->error = EINVAL;
        return FIO_Q_COMPLETED;
    }

    const auto registered = state->files.find(io_u->file->fd);
    if (registered == state->files.end()) {
        log_err("fio-sluice: fd %d was never registered by open_file\n", io_u->file->fd);
        io_u->error = EBADF;
        return FIO_Q_COMPLETED;
    }
    const sluice::async::NativeFileRef file{io_u->file->fd, registered->second};

    if (state->free_slots.empty()) {
        // Unreachable while capacity == iodepth holds; a slot-accounting bug
        // must not surface as undefined behavior or as a silent drop.
        log_err("fio-sluice: slot accounting bug: no free slot (capacity=%zu"
                ", fio cur_depth=%u)\n",
                state->capacity, td->cur_depth);
        std::terminate();
    }
    const std::size_t index = state->free_slots.back();
    state->free_slots.pop_back();
    auto& slot = state->slots[index];

    auto reject = [&](const sluice::IoError& error) {
        state->free_slots.push_back(index);
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
        ++state->research.submit_rejected_calls;
#endif
        const auto mapped = map_errno(error);
        if (mapped.needs_diag) {
            log_err("fio-sluice: submit rejected ddir=%d: code=%s os_errno=%d -> errno=%d\n",
                    static_cast<int>(ddir), sluice::to_string(error.code).data(),
                    error.os_errno, mapped.value);
        }
        io_u->error = mapped.value;
        return FIO_Q_COMPLETED;
    };

    try {
        if (ddir == DDIR_READ || ddir == DDIR_WRITE) {
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
            if (ddir == DDIR_READ) {
                ++state->research.submit_read_calls;
            } else {
                ++state->research.submit_write_calls;
            }
#endif
            auto ticket = ddir == DDIR_READ
                ? state->scope->submit_read({file,
                                             static_cast<std::byte*>(io_u->xfer_buf),
                                             static_cast<std::size_t>(io_u->xfer_buflen),
                                             io_u->offset})
                : state->scope->submit_write({file,
                                              static_cast<const std::byte*>(io_u->xfer_buf),
                                              static_cast<std::size_t>(io_u->xfer_buflen),
                                              io_u->offset});
            if (!ticket.has_value()) {
                return reject(ticket.error());
            }
            slot.u = io_u;
            slot.kind = ddir == DDIR_READ ? EngineState::Slot::Kind::read
                                          : EngineState::Slot::Kind::write;
            slot.data = ticket.value();
            slot.sync = {};
            ++state->pending_submits;
            return FIO_Q_QUEUED;
        }
#if defined(SLUICE_FIO_RESEARCH_COUNTERS)
        ++state->research.submit_sync_calls;
#endif
        auto ticket = ddir == DDIR_SYNC ? state->scope->submit_sync_all({file})
                                        : state->scope->submit_sync_data({file});
        if (!ticket.has_value()) {
            return reject(ticket.error());
        }
        slot.u = io_u;
        slot.kind = ddir == DDIR_SYNC ? EngineState::Slot::Kind::sync_all
                                      : EngineState::Slot::Kind::sync_data;
        slot.sync = ticket.value();
        slot.data = {};
        ++state->pending_submits;
        return FIO_Q_QUEUED;
    } catch (const std::exception& error) {
        state->free_slots.push_back(index);
        log_err("fio-sluice: submit threw: %s\n", error.what());
        io_u->error = EIO;
        return FIO_Q_COMPLETED;
    } catch (...) {
        state->free_slots.push_back(index);
        log_err("fio-sluice: submit threw an unknown exception\n");
        io_u->error = EIO;
        return FIO_Q_COMPLETED;
    }
}

} // namespace fio_sluice
