#include "workload.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <iostream>
#include <map>
#include <random>
#include <stdexcept>
#include <string>
#include <utility>

namespace bench {
namespace {

using namespace std::string_view_literals;

template <typename T>
T number(std::string_view value, std::string_view option, T minimum, T maximum) {
    T parsed{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), parsed);
    if (error != std::errc{} || end != value.data() + value.size() ||
        parsed < minimum || parsed > maximum) {
        throw std::invalid_argument(std::string(option) + " expects an integer in [" +
                                    std::to_string(minimum) + ", " +
                                    std::to_string(maximum) + "]");
    }
    return parsed;
}

template <typename T, std::size_t N>
T choice(std::string_view value, std::string_view option,
         const std::array<std::pair<std::string_view, T>, N>& choices) {
    for (const auto& [label, result] : choices) {
        if (label == value) return result;
    }
    throw std::invalid_argument("unsupported value for " + std::string(option) +
                                ": " + std::string(value));
}

constexpr std::array backends{
    std::pair{"posix"sv, Backend::posix}, std::pair{"direct"sv, Backend::direct},
    std::pair{"threadpool"sv, Backend::threadpool}, std::pair{"uring"sv, Backend::uring}
};

constexpr std::array access_patterns{
    std::pair{"sequential"sv, Access::sequential}, std::pair{"random"sv, Access::random},
    std::pair{"hotspot"sv, Access::hotspot}
};

Options preset(std::string_view scenario) {
    Options options;
    options.scenario = scenario;
    if (scenario == "seq-read") {
        options.access = Access::sequential;
        options.read_sizes = {{65536, 100}};
    } else if (scenario == "random-read") {
        return options;
    } else if (scenario == "mixed" || scenario == "mixed-size") {
        options.read_percent = 90;
        if (scenario == "mixed-size") {
            options.read_sizes = options.write_sizes = {{4096, 80}, {65536, 20}};
        }
    } else if (scenario == "background-write") {
        options.write_sizes = {{65536, 100}};
    } else {
        throw std::invalid_argument("unknown scenario: " + std::string(scenario));
    }
    return options;
}

std::vector<SizeWeight> size_weights(std::string_view value, std::string_view option) {
    std::vector<SizeWeight> sizes;
    unsigned total = 0;
    for (;;) {
        const auto comma = value.find(',');
        const auto field = value.substr(0, comma);
        const auto colon = field.find(':');
        if (colon == std::string_view::npos) {
            throw std::invalid_argument(std::string(option) + " expects bytes:weight pairs");
        }
        const auto size = number(field.substr(0, colon), option, alignment, mib);
        if (size % alignment != 0) {
            throw std::invalid_argument(std::string(option) + " requires 4096-byte multiples");
        }
        if (std::any_of(sizes.begin(), sizes.end(),
                        [size](const SizeWeight& field) { return field.bytes == size; })) {
            throw std::invalid_argument(std::string(option) + " contains a duplicate size");
        }
        const auto weight = number(field.substr(colon + 1), option, 1u, 100u);
        total += weight;
        if (total > 100) break;
        sizes.push_back({size, weight});
        if (comma == std::string_view::npos) break;
        value.remove_prefix(comma + 1);
    }
    if (total != 100) {
        throw std::invalid_argument(std::string(option) + " weights must sum to 100");
    }
    return sizes;
}

class Random {
public:
    explicit Random(std::uint64_t seed) : engine_(seed) {}

    std::uint64_t below(std::uint64_t count) {
        const auto threshold = -count % count;
        auto sample = engine_();
        while (sample < threshold) sample = engine_();
        return sample % count;
    }

    template <typename T>
    void shuffle(std::vector<T>& values) {
        for (auto count = values.size(); count > 1; --count) {
            std::swap(values[count - 1], values[below(count)]);
        }
    }

private:
    std::mt19937_64 engine_;
};

std::size_t pick_size(Random& random, const std::vector<SizeWeight>& sizes) {
    auto percentile = random.below(100);
    for (const auto [size, weight] : sizes) {
        if (percentile < weight) return size;
        percentile -= weight;
    }
    throw std::logic_error("invalid size distribution");
}

std::chrono::nanoseconds arrival(std::size_t index, unsigned rate) {
    return std::chrono::nanoseconds(rate == 0 ? 0 : index * 1'000'000'000ULL / rate);
}

std::uint64_t random_offset(Random& random, const Options& options, std::size_t size) {
    const auto blocks = (options.working_set - size) / alignment + 1;
    if (options.access == Access::random) return random.below(blocks) * alignment;
    const auto hot_bytes = options.working_set * options.hotspot_range_percent / 100;
    const auto hot_blocks = hot_bytes >= size ? (hot_bytes - size) / alignment + 1 : 0;
    if (hot_blocks == 0 || hot_blocks == blocks) {
        throw std::invalid_argument("hotspot cannot fit this request size and a cold region");
    }
    const auto cold_start = (hot_bytes + alignment - 1) / alignment;
    if (cold_start >= blocks) {
        throw std::invalid_argument("cold region cannot fit this request size");
    }
    if (random.below(100) < options.hotspot_percent) {
        return random.below(hot_blocks) * alignment;
    }
    return (cold_start + random.below(blocks - cold_start)) * alignment;
}

void hash_value(std::uint64_t& hash, std::uint64_t value) {
    for (unsigned shift = 0; shift < 64; shift += 8) {
        hash ^= (value >> shift) & 0xff;
        hash *= 1099511628211ULL;
    }
}

} // namespace

std::string_view name(Backend backend) {
    for (const auto& [label, entry] : backends) {
        if (backend == entry) return label;
    }
    throw std::logic_error("unknown backend");
}

std::string_view name(Access access) {
    for (const auto& [label, entry] : access_patterns) {
        if (access == entry) return label;
    }
    throw std::logic_error("unknown access pattern");
}

std::string_view name(Operation operation) {
    return operation == Operation::read ? "read" : "write";
}

Options parse_options(int argc, char* argv[]) {
    if (argc % 2 == 0) throw std::invalid_argument("options are --name value pairs");
    std::map<std::string_view, std::string_view> arguments;
    for (int i = 1; i < argc; i += 2) {
        if (!arguments.emplace(argv[i], argv[i + 1]).second) {
            throw std::invalid_argument("duplicate option: " + std::string(argv[i]));
        }
    }
    const auto found = arguments.find("--scenario");
    auto options = preset(found == arguments.end() ? "random-read" : found->second);
    const bool background = options.scenario == "background-write";
    for (const auto& [key, value] : arguments) {
        if (key == "--scenario") continue;
        if (key == "--backend") options.backend = choice(value, key, backends);
        else if (key == "--access") options.access = choice(value, key, access_patterns);
        else if (key == "--read-sizes") options.read_sizes = size_weights(value, key);
        else if (key == "--write-sizes") options.write_sizes = size_weights(value, key);
        else if (key == "--read-percent" && !background) {
            options.read_percent = number(value, key, 0u, 100u);
        } else if (key == "--requests") {
            options.requests = number<std::size_t>(value, key, 1, 1'000'000);
        } else if (key == "--working-set-mib") {
            options.working_set = number<std::size_t>(value, key, 1, 1'048'576) * mib;
        } else if (key == "--hotspot-percent") {
            options.hotspot_percent = number(value, key, 1u, 99u);
        } else if (key == "--hotspot-range-percent") {
            options.hotspot_range_percent = number(value, key, 1u, 99u);
        } else if (key == "--seed") {
            options.seed = number<std::uint64_t>(value, key, 0, UINT64_MAX);
        } else if (key == "--rate" && !background) {
            options.rate = number(value, key, 0u, 1'000'000'000u);
        } else if (key == "--read-rate" && background) {
            options.read_rate = number(value, key, 1u, 1'000'000'000u);
        } else if (key == "--write-rate" && background) {
            options.write_rate = number(value, key, 1u, 1'000'000'000u);
        } else if (key == "--depth") {
            options.depth = number<std::size_t>(value, key, 1, 1024);
        } else if (key == "--workers") {
            options.workers = number<std::size_t>(value, key, 1, 256);
        } else if (key == "--max-inflight-bytes") {
            options.byte_limit = number<std::size_t>(value, key, 0, 1024 * mib);
        } else if (key == "--scratch-dir") {
            options.scratch_dir = value;
        } else {
            throw std::invalid_argument("option is unknown or unavailable for this scenario: " +
                                        std::string(key));
        }
    }
    if ((options.backend == Backend::posix || options.backend == Backend::direct) &&
        options.depth != 1) {
        throw std::invalid_argument("the single-caller POSIX and Direct arms require depth 1");
    }
    if (options.backend != Backend::threadpool && arguments.contains("--workers")) {
        throw std::invalid_argument("--workers applies only to the ThreadPool arm");
    }
    if (background && options.requests < 2) {
        throw std::invalid_argument("background-write requires at least two requests");
    }
    return options;
}

Workload make_workload(const Options& options) {
    Workload workload;
    workload.requests.reserve(options.requests);
    const bool background = options.scenario == "background-write";
    workload.scheduled = background || options.rate != 0;
    Random random(options.seed);

    std::vector<Operation> operations(options.requests, Operation::read);
    if (!background) {
        const auto writes = options.requests * (100 - options.read_percent) / 100;
        std::fill_n(operations.begin(), writes, Operation::write);
        random.shuffle(operations);
    }

    std::size_t reads = 0;
    std::size_t writes = 0;
    std::uint64_t next_read_offset = 0;
    std::vector<std::size_t> write_indices;
    std::map<std::pair<Operation, std::size_t>, std::size_t> classes;
    for (std::size_t index = 0; index < options.requests; ++index) {
        auto operation = operations[index];
        auto scheduled = arrival(index, options.rate);
        if (background) {
            const auto read_at = arrival(reads, options.read_rate);
            const auto write_at = arrival(writes, options.write_rate);
            operation = (read_at < write_at || (read_at == write_at && reads == 0))
                            ? Operation::read : Operation::write;
            scheduled = std::min(read_at, write_at);
        }
        const bool is_read = operation == Operation::read;
        const auto size = pick_size(random, is_read ? options.read_sizes : options.write_sizes);
        const auto [class_index, new_class] = classes.try_emplace({operation, size}, classes.size());
        if (new_class) workload.classes.push_back({operation, size});
        ++workload.classes[class_index->second].requests;
        Request request{operation, 0, size, scheduled, class_index->second};
        if (is_read) {
            if (size > options.working_set) {
                throw std::invalid_argument("read size exceeds the working set");
            }
            if (options.access == Access::sequential) {
                if (next_read_offset + size > options.working_set) next_read_offset = 0;
                request.offset = next_read_offset;
                next_read_offset += size;
            } else {
                request.offset = random_offset(random, options, size);
            }
            ++reads;
            workload.read_bytes += size;
            workload.max_read_size = std::max(workload.max_read_size, size);
        } else {
            write_indices.push_back(index);
            ++writes;
            workload.write_bytes += size;
            workload.max_write_size = std::max(workload.max_write_size, size);
        }
        workload.requests.push_back(request);
    }
    if (options.byte_limit != 0 &&
        options.byte_limit < std::max(workload.max_read_size, workload.max_write_size)) {
        throw std::invalid_argument("byte budget cannot admit the largest generated request");
    }

    if (options.access != Access::sequential) random.shuffle(write_indices);
    std::uint64_t write_offset = reads == 0 ? 0 : options.working_set;
    for (const auto index : write_indices) {
        workload.requests[index].offset = write_offset;
        write_offset += workload.requests[index].size;
    }
    workload.read_count = reads;
    workload.file_bytes = write_offset;
    workload.fingerprint = 14695981039346656037ULL;
    hash_value(workload.fingerprint, 1);
    hash_value(workload.fingerprint, options.seed);
    hash_value(workload.fingerprint, workload.file_bytes);
    hash_value(workload.fingerprint, workload.scheduled);
    for (const auto& request : workload.requests) {
        hash_value(workload.fingerprint, static_cast<std::uint64_t>(request.operation));
        hash_value(workload.fingerprint, request.offset);
        hash_value(workload.fingerprint, request.size);
        hash_value(workload.fingerprint, request.arrival.count());
    }
    return workload;
}

void print_help() {
    std::cout << "Usage: sluice-workload-bench [--option value ...]\n"
                 "  --scenario seq-read|random-read|mixed|mixed-size|background-write\n"
                 "  --backend posix|direct|threadpool|uring\n"
                 "  --access sequential|random|hotspot\n"
                 "  --read-percent 0..100   --requests COUNT   --working-set-mib MIB\n"
                 "  --read-sizes 4096:80,65536:20   --write-sizes 4096:100\n"
                 "  --hotspot-percent 80   --hotspot-range-percent 20   --seed 42\n"
                 "  --depth COUNT   --workers COUNT (ThreadPool)   --max-inflight-bytes BYTES\n"
                 "  --rate IOPS (0 = closed loop) or --read-rate IOPS --write-rate IOPS (background-write)\n"
                 "  --scratch-dir EXISTING_DIRECTORY\n";
}

} // namespace bench
