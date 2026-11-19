// Exhaustive IoError -> errno mapping checks (ADAPTER-ARCHITECTURE §6.3).
// Pure C++: no fio headers, links only errno_map.cpp.
#include <cerrno>
#include <cstdio>

#include "errno_map.hpp"

using fio_sluice::map_errno;
using fio_sluice::is_eof;

namespace {

int failures = 0;

void expect_errno(const char* what, const sluice::IoError& error, int value) {
    const auto mapped = map_errno(error);
    if (mapped.value != value || mapped.needs_diag) {
        std::printf("FAIL %s: got errno=%d needs_diag=%d, want errno=%d\n", what,
                    mapped.value, mapped.needs_diag ? 1 : 0, value);
        ++failures;
    }
}

void expect_diag(const char* what, const sluice::IoError& error) {
    const auto mapped = map_errno(error);
    if (mapped.value != EIO || !mapped.needs_diag) {
        std::printf("FAIL %s: got errno=%d needs_diag=%d, want errno=%d needs_diag=1\n",
                    what, mapped.value, mapped.needs_diag ? 1 : 0, EIO);
        ++failures;
    }
}

} // namespace

int main() {
    // Rule 1: native errno wins whenever present, even for codes that
    // otherwise reduce (ERR-01).
    expect_errno("os_errno passthrough", sluice::IoError{sluice::IoError::Code::invalid_state, 28},
                 28);
    expect_errno("os_errno EIO", sluice::IoError{sluice::IoError::Code::backend_error, EIO}, EIO);
    expect_errno("os_errno ENOSPC", sluice::IoError{sluice::IoError::Code::would_block, ENOSPC},
                 ENOSPC);

    // Rule 2: canonical reductions with os_errno == 0.
    expect_errno("canceled", sluice::IoError{sluice::IoError::Code::canceled}, ECANCELED);
    expect_errno("interrupted", sluice::IoError{sluice::IoError::Code::interrupted}, EINTR);
    expect_errno("would_block", sluice::IoError{sluice::IoError::Code::would_block}, EAGAIN);
    expect_errno("no_space", sluice::IoError{sluice::IoError::Code::no_space}, ENOSPC);
    expect_errno("permission_denied", sluice::IoError{sluice::IoError::Code::permission_denied},
                 EACCES);
    expect_errno("not_found", sluice::IoError{sluice::IoError::Code::not_found}, ENOENT);
    expect_errno("invalid_argument", sluice::IoError{sluice::IoError::Code::invalid_argument},
                 EINVAL);

    // not_supported has an errno but must be surfaced to the log.
    {
        const auto mapped = map_errno(sluice::IoError{sluice::IoError::Code::not_supported});
        if (mapped.value != ENOTSUP || !mapped.needs_diag) {
            std::printf("FAIL not_supported: errno=%d needs_diag=%d\n", mapped.value,
                        mapped.needs_diag ? 1 : 0);
            ++failures;
        }
    }

    // No lossless mapping exists: conservative EIO plus a diagnostic line.
    expect_diag("invalid_state", sluice::IoError{sluice::IoError::Code::invalid_state});
    expect_diag("backend_error", sluice::IoError{sluice::IoError::Code::backend_error});

    // eof is not an error: callers turn it into a residual.
    {
        const sluice::IoError eof{sluice::IoError::Code::eof};
        if (!is_eof(eof)) {
            std::printf("FAIL eof classification\n");
            ++failures;
        }
        const auto mapped = map_errno(eof);
        if (mapped.value != 0 || mapped.needs_diag) {
            std::printf("FAIL eof mapping: errno=%d\n", mapped.value);
            ++failures;
        }
    }

    if (failures != 0) {
        std::printf("errno_map_test: %d failure(s)\n", failures);
        return 1;
    }
    std::printf("errno_map_test: PASS\n");
    return 0;
}
