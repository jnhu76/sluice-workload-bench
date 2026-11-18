#pragma once

#include "workload.hpp"

#include <optional>
#include <stdexcept>
#include <vector>

namespace bench {

struct BackendUnavailable : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct Sample {
    std::chrono::nanoseconds latency;
    std::chrono::nanoseconds queue_delay;
};

struct Run {
    std::vector<Sample> samples;
    std::chrono::nanoseconds elapsed{};
    double user_seconds = 0;
    double system_seconds = 0;
    std::size_t peak_requests = 0;
    std::size_t peak_bytes = 0;
    std::size_t blocked_admissions = 0;
    std::size_t progress_waits = 0;
    std::size_t buffer_bytes = 0;
    std::optional<double> resident_fraction;
    long filesystem_type = 0;
};

Run execute(const Options& options, const Workload& workload);

} // namespace bench
