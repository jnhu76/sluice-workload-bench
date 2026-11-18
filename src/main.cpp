#include "runner.hpp"
#include "workload.hpp"

#include <algorithm>
#include <iomanip>
#include <iostream>
#include <string_view>

#include <sys/utsname.h>

namespace {

using namespace bench;

void json_string(std::ostream& out, std::string_view text) {
    constexpr std::string_view hex = "0123456789abcdef";
    out << '"';
    for (const unsigned char c : text) {
        if (c == '"' || c == '\\') {
            out << '\\' << c;
        } else if (c < 0x20) {
            out << "\\u00" << hex[c >> 4] << hex[c & 0xf];
        } else {
            out << c;
        }
    }
    out << '"';
}

void size_distribution(std::ostream& out, const std::vector<SizeWeight>& distribution) {
    out << '[';
    for (std::size_t i = 0; i < distribution.size(); ++i) {
        if (i != 0) out << ',';
        out << "{\"bytes\":" << distribution[i].bytes
            << ",\"weight\":" << distribution[i].weight << '}';
    }
    out << ']';
}

void percentiles(std::ostream& out, std::vector<std::chrono::nanoseconds>& values) {
    std::sort(values.begin(), values.end());
    const auto rank = [&](std::size_t percent) {
        return values[(values.size() * percent + 99) / 100 - 1].count();
    };
    out << "{\"p50\":" << rank(50) << ",\"p95\":" << rank(95) << ",\"p99\":" << rank(99) << '}';
}

void print_result(const Options& options, const Workload& workload, const Run& run) {
    auto& out = std::cout;
    const auto requests = workload.requests.size();
    const double elapsed = std::chrono::duration<double>(run.elapsed).count();
    if (elapsed <= 0) throw std::runtime_error("clock resolution too coarse for this run");

    out << std::setprecision(9)
        << "{\"schema\":1,\"status\":\"ok\",\"byte_oracle\":\"passed\""
        << ",\"scenario\":";
    json_string(out, options.scenario);
    out << ",\"backend\":";
    json_string(out, name(options.backend));
    out << ",\"sdk_revision\":\"" SLUICE_REVISION "\",\"sdk_profile\":\"" BENCH_BUILD_PROFILE "\""
        << ",\"compiler\":";
    json_string(out, __VERSION__);
    struct utsname host{};
    if (::uname(&host) == 0) {
        out << ",\"kernel\":";
        json_string(out, host.release);
        out << ",\"architecture\":";
        json_string(out, host.machine);
    }
    out << ",\"filesystem_type\":\"0x" << std::hex << run.filesystem_type << std::dec << '"';
    out << ",\"fixture_resident_fraction_before_timing\":";
    if (run.resident_fraction.has_value()) out << *run.resident_fraction;
    else out << "null";

    out << ",\"workload\":{\"trace_id\":\"" << std::hex << std::setw(16) << std::setfill('0')
        << workload.fingerprint << std::dec << '"'
        << ",\"seed\":\"" << options.seed << "\",\"access\":";
    json_string(out, name(options.access));
    out << ",\"arrival\":\"" << (workload.scheduled ? "scheduled" : "closed-loop") << '"'
        << ",\"requests\":" << requests
        << ",\"read_requests\":" << workload.read_count
        << ",\"write_requests\":" << requests - workload.read_count
        << ",\"read_bytes\":" << workload.read_bytes
        << ",\"write_bytes\":" << workload.write_bytes
        << ",\"working_set_bytes\":" << (workload.read_count != 0 ? options.working_set : 0)
        << ",\"fixture_bytes\":" << workload.file_bytes;
    if (options.scenario == "background-write") {
        out << ",\"read_rate_iops\":" << options.read_rate
            << ",\"write_rate_iops\":" << options.write_rate;
    } else {
        out << ",\"rate_iops\":" << options.rate << ",\"read_percent\":" << options.read_percent;
    }
    if (options.access == Access::hotspot) {
        out << ",\"hotspot_percent\":" << options.hotspot_percent
            << ",\"hotspot_range_percent\":" << options.hotspot_range_percent;
    }
    out << ",\"read_sizes\":";
    size_distribution(out, options.read_sizes);
    out << ",\"write_sizes\":";
    size_distribution(out, options.write_sizes);
    out << "},\"execution\":{\"depth\":" << options.depth
        << ",\"workers\":" << (options.backend == Backend::threadpool ? options.workers : 0)
        << ",\"byte_limit\":" << options.byte_limit
        << ",\"peak_inflight_requests\":" << run.peak_requests
        << ",\"peak_inflight_bytes\":" << run.peak_bytes
        << ",\"allocated_payload_bytes\":" << run.buffer_bytes
        << ",\"blocked_admission_checks\":" << run.blocked_admissions
        << ",\"progress_waits\":" << run.progress_waits
        << "},\"timing\":{\"wall_ns\":" << run.elapsed.count()
        << ",\"user_cpu_seconds\":" << run.user_seconds
        << ",\"system_cpu_seconds\":" << run.system_seconds
        << ",\"cpu_ns_per_request\":" << (run.user_seconds + run.system_seconds) * 1e9 / requests
        << ",\"iops\":" << requests / elapsed
        << ",\"mib_per_second\":" << (workload.read_bytes + workload.write_bytes) / elapsed / mib
        << "},\"classes\":[";

    std::vector<std::vector<std::chrono::nanoseconds>> latencies(workload.classes.size());
    std::vector<std::vector<std::chrono::nanoseconds>> queues(workload.classes.size());
    for (std::size_t i = 0; i < workload.classes.size(); ++i) {
        latencies[i].reserve(workload.classes[i].requests);
        queues[i].reserve(workload.classes[i].requests);
    }
    for (std::size_t i = 0; i < run.samples.size(); ++i) {
        const auto group = workload.requests[i].latency_class;
        latencies[group].push_back(run.samples[i].latency);
        queues[group].push_back(run.samples[i].queue_delay);
    }
    for (std::size_t i = 0; i < workload.classes.size(); ++i) {
        const auto& group = workload.classes[i];
        if (i != 0) out << ',';
        out << "{\"operation\":\"" << name(group.operation)
            << "\",\"size_bytes\":" << group.size
            << ",\"samples\":" << group.requests << ",\"latency_ns\":";
        percentiles(out, latencies[i]);
        out << ",\"pre_submit_delay_ns\":";
        if (workload.scheduled) percentiles(out, queues[i]);
        else out << "null";
        out << '}';
    }
    out << "]}\n";
}

} // namespace

int main(int argc, char* argv[]) {
    if (argc == 2 && std::string_view(argv[1]) == "--help") {
        bench::print_help();
        return 0;
    }
    try {
        const auto options = bench::parse_options(argc, argv);
        const auto workload = bench::make_workload(options);
        const auto run = bench::execute(options, workload);
        print_result(options, workload, run);
        return 0;
    } catch (const bench::BackendUnavailable& error) {
        std::cout << "{\"schema\":1,\"status\":\"unavailable\",\"backend\":\"uring\",\"reason\":";
        json_string(std::cout, error.what());
        std::cout << "}\n";
        return 77;
    } catch (const std::exception& error) {
        std::cerr << "sluice-workload-bench: " << error.what() << '\n';
        return 1;
    }
}
