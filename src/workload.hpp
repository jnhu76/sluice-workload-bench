#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string_view>
#include <vector>

namespace bench {

inline constexpr std::size_t alignment = 4096;
inline constexpr std::size_t mib = 1024 * 1024;

enum class Backend { posix, direct, threadpool, uring };
enum class Access { sequential, random, hotspot };
enum class Operation { read, write };

struct SizeWeight {
    std::size_t bytes;
    unsigned weight;
};

struct Options {
    std::string_view scenario = "random-read";
    Backend backend = Backend::posix;
    Access access = Access::random;
    std::vector<SizeWeight> read_sizes{{4096, 100}};
    std::vector<SizeWeight> write_sizes{{4096, 100}};
    unsigned read_percent = 100;
    std::size_t requests = 10000;
    std::size_t working_set = 32 * mib;
    unsigned hotspot_percent = 80;
    unsigned hotspot_range_percent = 20;
    std::uint64_t seed = 42;
    unsigned rate = 0;
    unsigned read_rate = 8000;
    unsigned write_rate = 1000;
    std::size_t depth = 1;
    std::size_t workers = 1;
    std::size_t byte_limit = 0;
    std::filesystem::path scratch_dir = ".";
};

struct Request {
    Operation operation;
    std::uint64_t offset;
    std::size_t size;
    std::chrono::nanoseconds arrival;
    std::size_t latency_class;
};

struct LatencyClass {
    Operation operation;
    std::size_t size;
    std::size_t requests = 0;
};

struct Workload {
    std::vector<Request> requests;
    std::vector<LatencyClass> classes;
    std::size_t read_count = 0;
    std::uint64_t read_bytes = 0;
    std::uint64_t write_bytes = 0;
    std::uint64_t file_bytes = 0;
    std::uint64_t fingerprint = 0;
    std::size_t max_read_size = 0;
    std::size_t max_write_size = 0;
    bool scheduled = false;
};

Options parse_options(int argc, char* argv[]);
Workload make_workload(const Options& options);
std::string_view name(Backend backend);
std::string_view name(Operation operation);
std::string_view name(Access access);
void print_help();

} // namespace bench
