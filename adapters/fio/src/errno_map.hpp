// IoError -> errno translation for io_u->error (ADAPTER-ARCHITECTURE §6.3).
// Kept free of fio headers so the mapping is unit-testable standalone.
#pragma once

#include <sluice/error.hpp>

namespace fio_sluice {

struct MappedErrno {
    int value = 0;
    // Codes with no lossless errno mapping keep EIO and must be reported
    // through fio's log_err by the caller (fail closed, never success).
    bool needs_diag = false;
};

// ERR-01: preserve the native errno when present.
// Otherwise reduce the canonical code; invalid_state/backend_error have no
// lossless mapping and fall back to EIO with needs_diag set.
MappedErrno map_errno(const sluice::IoError& error) noexcept;

// eof is not an error for fio: the caller reports it as a residual
// (short transfer) the way a confirmed-byte short count is reported.
inline bool is_eof(const sluice::IoError& error) noexcept {
    return error.code == sluice::IoError::Code::eof && error.os_errno == 0;
}

} // namespace fio_sluice
