#include "errno_map.hpp"

#include <cerrno>

namespace fio_sluice {

MappedErrno map_errno(const sluice::IoError& error) noexcept {
    if (error.os_errno != 0) {
        return {.value = error.os_errno, .needs_diag = false};
    }
    switch (error.code) {
    case sluice::IoError::Code::eof:
        // Callers convert eof to a residual before reaching this table.
        return {.value = 0, .needs_diag = false};
    case sluice::IoError::Code::canceled:
        return {.value = ECANCELED, .needs_diag = false};
    case sluice::IoError::Code::interrupted:
        return {.value = EINTR, .needs_diag = false};
    case sluice::IoError::Code::would_block:
        return {.value = EAGAIN, .needs_diag = false};
    case sluice::IoError::Code::no_space:
        return {.value = ENOSPC, .needs_diag = false};
    case sluice::IoError::Code::permission_denied:
        return {.value = EACCES, .needs_diag = false};
    case sluice::IoError::Code::not_found:
        return {.value = ENOENT, .needs_diag = false};
    case sluice::IoError::Code::invalid_argument:
        return {.value = EINVAL, .needs_diag = false};
    case sluice::IoError::Code::not_supported:
        return {.value = ENOTSUP, .needs_diag = true};
    case sluice::IoError::Code::invalid_state:
    case sluice::IoError::Code::backend_error:
        return {.value = EIO, .needs_diag = true};
    }
    return {.value = EIO, .needs_diag = true};
}

} // namespace fio_sluice
