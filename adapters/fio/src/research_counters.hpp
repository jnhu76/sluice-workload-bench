// Benchmark-local research counters for the fio adapter path.
//
// Production builds never define SLUICE_FIO_RESEARCH_COUNTERS: this header
// then declares nothing and no call site exists. The research build emits one
// JSON line prefixed with RESEARCH_COUNTERS on stderr at cleanup and, when
// SLUICE_FIO_RESEARCH_COUNTERS_FILE is set, appends the same line to that
// file.
#pragma once

#if defined(SLUICE_FIO_RESEARCH_COUNTERS)

#include <sluice/async/detail/research_counters.hpp>

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace fio_sluice {

struct ResearchCounters {
    // Scan-length histogram extension (session-002): number of harvest()
    // calls whose slot scan examined exactly k slots, k = 0..64. Slot
    // counts above 64 saturate into the last bucket (the adapter capacity
    // is fio iodepth, and the measured cells use iodepth <= 64).
    static constexpr std::size_t kScanHistBuckets = 65;

    // Adapter counters (one EngineState per fio thread/job).
    std::uint64_t getevents_calls = 0;
    std::uint64_t harvest_calls = 0;
    std::uint64_t harvest_slots_examined = 0;
    std::uint64_t scan_hist[kScanHistBuckets] = {};
    std::uint64_t take_calls = 0;
    std::uint64_t take_pending = 0;
    std::uint64_t take_ready = 0;
    std::uint64_t take_empty = 0;
    std::uint64_t staged_completions = 0;
    std::uint64_t max_staged = 0;
    std::uint64_t submit_read_calls = 0;
    std::uint64_t submit_write_calls = 0;
    std::uint64_t submit_sync_calls = 0;
    std::uint64_t submit_rejected_calls = 0;

    // Core counters are process-global; this state records the value at the
    // end of init so cleanup reports this job's window.
    sluice::async::detail::research::Snapshot core_before{};

    void note_staged(std::uint64_t staged_size) noexcept {
        ++staged_completions;
        if (staged_size > max_staged) {
            max_staged = staged_size;
        }
    }

    void note_scan_length(std::uint64_t examined) noexcept {
        const auto bucket = examined < kScanHistBuckets
                                ? examined
                                : kScanHistBuckets - 1;
        ++scan_hist[bucket];
    }

    void emit(const char* backend, unsigned int iodepth, unsigned int workers) const noexcept {
        const auto core = sluice::async::detail::research::delta(
            core_before, sluice::async::detail::research::snapshot());
        char hist[2048];
        int off = 0;
        for (std::size_t i = 0; i < kScanHistBuckets && off >= 0
                 && static_cast<std::size_t>(off) < sizeof(hist);
             ++i) {
            off += std::snprintf(
                hist + off, sizeof(hist) - static_cast<std::size_t>(off),
                "%s%llu", i == 0 ? "" : ",",
                static_cast<unsigned long long>(scan_hist[i]));
        }
        char line[4096];
        const int n = std::snprintf(
            line, sizeof(line),
            "RESEARCH_COUNTERS {\"schema\":\"sluice-fio-research/2\","
            "\"backend\":\"%s\",\"iodepth\":%u,\"workers\":%u,"
            "\"getevents_calls\":%llu,\"harvest_calls\":%llu,"
            "\"harvest_slots_examined\":%llu,\"take_calls\":%llu,"
            "\"take_pending\":%llu,\"take_ready\":%llu,\"take_empty\":%llu,"
            "\"staged_completions\":%llu,\"max_staged\":%llu,"
            "\"scan_hist\":[%s],"
            "\"submit_read_calls\":%llu,\"submit_write_calls\":%llu,"
            "\"submit_sync_calls\":%llu,\"submit_rejected_calls\":%llu,"
            "\"core\":{\"scope_resolve_calls\":%llu,"
            "\"scope_resolve_slot_iterations\":%llu,"
            "\"scope_take_calls\":%llu,\"bridge_consume_calls\":%llu,"
            "\"core_mutex_lock_calls\":%llu}}\n",
            backend, iodepth, workers,
            static_cast<unsigned long long>(getevents_calls),
            static_cast<unsigned long long>(harvest_calls),
            static_cast<unsigned long long>(harvest_slots_examined),
            static_cast<unsigned long long>(take_calls),
            static_cast<unsigned long long>(take_pending),
            static_cast<unsigned long long>(take_ready),
            static_cast<unsigned long long>(take_empty),
            static_cast<unsigned long long>(staged_completions),
            static_cast<unsigned long long>(max_staged),
            hist,
            static_cast<unsigned long long>(submit_read_calls),
            static_cast<unsigned long long>(submit_write_calls),
            static_cast<unsigned long long>(submit_sync_calls),
            static_cast<unsigned long long>(submit_rejected_calls),
            static_cast<unsigned long long>(core.scope_resolve_calls),
            static_cast<unsigned long long>(core.scope_resolve_slot_iterations),
            static_cast<unsigned long long>(core.scope_take_calls),
            static_cast<unsigned long long>(core.bridge_consume_calls),
            static_cast<unsigned long long>(core.core_mutex_lock_calls));
        if (n <= 0) {
            return;
        }
        std::fputs(line, stderr);
        std::fflush(stderr);
        const char* path = std::getenv("SLUICE_FIO_RESEARCH_COUNTERS_FILE");
        if (path != nullptr && path[0] != '\0') {
            if (std::FILE* out = std::fopen(path, "a")) {
                std::fputs(line, out);
                std::fclose(out);
            }
        }
    }
};

} // namespace fio_sluice

#endif // SLUICE_FIO_RESEARCH_COUNTERS
