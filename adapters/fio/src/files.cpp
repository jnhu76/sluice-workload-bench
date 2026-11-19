// open_file / close_file / get_file_size (ADAPTER-ARCHITECTURE §4.7).
// fd ownership stays with fio: generic_open_file/generic_close_file handle
// O_DIRECT, O_CREAT and hashing; the engine only records the borrow.
#include "state.hpp"

#include "callbacks.hpp"

namespace fio_sluice {

int sluice_open_file(struct thread_data* td, struct fio_file* f) {
    const int r = generic_open_file(td, f);
    if (r != 0) {
        return r;
    }
    auto* state = state_of(td);
    if (state == nullptr) {
        log_err("fio-sluice: open_file before engine init\n");
        generic_close_file(td, f);
        return -1;
    }
    const int flags = fcntl(f->fd, F_GETFL);
    if (flags < 0) {
        log_err("fio-sluice: F_GETFL failed on fd %d: %s\n", f->fd, strerror(errno));
        generic_close_file(td, f);
        return -1;
    }
    sluice::FileAccess access;
    switch (flags & O_ACCMODE) {
    case O_RDONLY:
        access = sluice::FileAccess::read_only;
        break;
    case O_WRONLY:
        access = sluice::FileAccess::write_only;
        break;
    default:
        access = sluice::FileAccess::read_write;
        break;
    }
    try {
        state->files[f->fd] = access;
    } catch (const std::exception& error) {
        log_err("fio-sluice: file registration failed: %s\n", error.what());
        generic_close_file(td, f);
        return -1;
    }
    return 0;
}

int sluice_close_file(struct thread_data* td, struct fio_file* f) {
    auto* state = state_of(td);
    if (state != nullptr) {
        for (const auto& slot : state->slots) {
            if (slot.u != nullptr && slot.u->file == f) {
                // fio gates the real close on f->references == 0
                // (filesetup.c:1898/1912-1913) and every io_u holds a
                // reference until it is reaped (io_u.c:1488/855-856), so
                // a live slot at close_file means an engine bookkeeping
                // bug, not a legal fio sequence. Closing anyway could
                // misdirect still-borrowed I/O to a reused fd number;
                // returning an error without closing violates put_file's
                // f->fd == -1 post-condition (filesetup.c:1921). Fail
                // closed like every other invariant breach.
                log_err("fio-sluice: close_file invariant violated: fd %d still has"
                        " in-flight request(s); failing closed\n",
                        f->fd);
                std::terminate();
            }
        }
        state->files.erase(f->fd);
    }
    return generic_close_file(td, f);
}

int sluice_get_file_size(struct thread_data* td, struct fio_file* f) {
    return generic_get_file_size(td, f);
}

} // namespace fio_sluice
