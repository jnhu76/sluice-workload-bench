#include "runner.hpp"

#include <sluice/async/request_scope.hpp>
#include <sluice/async/threadpool_backend.hpp>
#include <sluice/async/uring_backend.hpp>
#include <sluice/blocking/file.hpp>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <cstdlib>
#include <span>
#include <string>
#include <system_error>
#include <thread>

#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <unistd.h>

namespace bench {
namespace {

using Clock = std::chrono::steady_clock;
using Time = Clock::time_point;
using Buffer = std::vector<std::byte>;
using namespace std::chrono_literals;
using namespace sluice::async;

[[noreturn]] void errno_error(const char* action) {
    throw std::system_error(errno, std::generic_category(), action);
}

template <typename T>
void check(const sluice::Result<T>& result, std::string_view action) {
    if (!result.has_value()) {
        const auto error = result.error();
        throw std::runtime_error(std::string(action) + ": " +
                                 std::string(sluice::to_string(error.code)) +
                                 " (errno " + std::to_string(error.os_errno) + ")");
    }
}

void check_length(std::size_t actual, const Request& request, std::size_t index) {
    if (actual != request.size) {
        throw std::runtime_error("short " + std::string(name(request.operation)) +
                                 " at request " + std::to_string(index));
    }
}

std::uint64_t mix(std::uint64_t value) {
    value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31);
}

void store_word(std::byte* output, std::uint64_t value) {
    for (unsigned index = 0; index < 8; ++index) {
        output[index] = std::byte((value >> (8 * index)) & 0xff);
    }
}

void pattern(std::span<std::byte> output, std::uint64_t offset, std::uint64_t seed) {
    for (std::size_t i = 0; i < output.size(); i += 8) {
        store_word(output.data() + i, mix(seed + (offset + i) / 8));
    }
}

void independent_check(std::span<const std::byte> actual, std::span<const std::byte> expected,
                       std::uint64_t offset) {
    const auto difference = std::mismatch(actual.begin(), actual.end(), expected.begin());
    if (difference.first != actual.end()) {
        throw std::runtime_error("byte mismatch at file offset " +
                                 std::to_string(offset + (difference.first - actual.begin())));
    }
}

void setup_io(int fd, std::span<std::byte> bytes, std::uint64_t offset, bool write) {
    std::size_t done = 0;
    while (done < bytes.size()) {
        auto* data = bytes.data() + done;
        const auto size = bytes.size() - done;
        const auto position = static_cast<off_t>(offset + done);
        const auto count = write ? ::pwrite(fd, data, size, position)
                                 : ::pread(fd, data, size, position);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) errno_error(write ? "fixture pwrite" : "oracle pread");
        if (count == 0) throw std::runtime_error("unexpected EOF while accessing fixture");
        done += static_cast<std::size_t>(count);
    }
}

class Fixture {
public:
    explicit Fixture(const std::filesystem::path& directory) {
        path_ = (directory / "sluice-bench.XXXXXX").string();
        fd_ = ::mkstemp(path_.data());
        if (fd_ < 0) errno_error("create scratch file");
    }

    Fixture(const Fixture&) = delete;
    Fixture& operator=(const Fixture&) = delete;

    ~Fixture() {
        ::close(fd_);
        ::unlink(path_.c_str());
    }

    void prepare(const Workload& workload, std::uint64_t seed) const {
        Buffer chunk(mib);
        for (std::uint64_t offset = 0; offset < workload.file_bytes; offset += chunk.size()) {
            const auto count = std::min<std::uint64_t>(chunk.size(), workload.file_bytes - offset);
            auto bytes = std::span(chunk).first(count);
            pattern(bytes, offset, seed);
            setup_io(fd_, bytes, offset, true);
        }
        if (::fdatasync(fd_) != 0) errno_error("flush fixture setup");
    }

    void check_final_bytes(const Options& options, const Workload& workload) const {
        struct stat info{};
        if (::fstat(fd_, &info) != 0) errno_error("stat fixture");
        if (static_cast<std::uint64_t>(info.st_size) != workload.file_bytes) {
            throw std::runtime_error("unexpected fixture length");
        }
        Buffer expected(mib);
        Buffer actual(mib);
        for (std::uint64_t offset = 0; workload.read_count != 0 &&
             offset < options.working_set; offset += expected.size()) {
            const auto count = std::min<std::uint64_t>(expected.size(), options.working_set - offset);
            auto bytes = std::span(actual).first(count);
            auto reference = std::span(expected).first(count);
            pattern(reference, offset, options.seed);
            setup_io(fd_, bytes, offset, false);
            independent_check(bytes, reference, offset);
        }
        pattern(expected, 0, ~options.seed);
        for (std::size_t index = 0; index < workload.requests.size(); ++index) {
            const auto& request = workload.requests[index];
            if (request.operation == Operation::read) continue;
            auto bytes = std::span(actual).first(request.size);
            setup_io(fd_, bytes, request.offset, false);
            store_word(expected.data(), index + 1);
            independent_check(bytes, std::span(expected).first(request.size), request.offset);
        }
    }

    std::optional<double> resident_fraction(std::size_t bytes) const {
        const long page_size = ::sysconf(_SC_PAGESIZE);
        if (page_size <= 0) return std::nullopt;
        const auto page = static_cast<std::size_t>(page_size);
        std::array<unsigned char, 1024> present{};
        auto* mapping = ::mmap(nullptr, bytes, PROT_NONE, MAP_SHARED, fd_, 0);
        if (mapping == MAP_FAILED) return std::nullopt;
        std::size_t resident = 0;
        bool available = true;
        for (std::size_t offset = 0; offset < bytes; offset += page * present.size()) {
            const auto count = std::min(bytes - offset, page * present.size());
            auto* address = static_cast<char*>(mapping) + offset;
            if (::mincore(address, count, present.data()) != 0) {
                available = false;
                break;
            }
            for (std::size_t i = 0; i < (count + page - 1) / page; ++i) {
                resident += (present[i] & 1) != 0;
            }
        }
        ::munmap(mapping, bytes);
        if (!available) return std::nullopt;
        return static_cast<double>(resident) / ((bytes + page - 1) / page);
    }

    long filesystem_type() const {
        struct statfs identity{};
        if (::fstatfs(fd_, &identity) != 0) errno_error("identify filesystem");
        return identity.f_type;
    }

    int fd() const { return fd_; }
    const std::string& path() const { return path_; }

private:
    std::string path_;
    int fd_;
};

double seconds(timeval time) {
    return time.tv_sec + time.tv_usec / 1'000'000.0;
}

rusage process_usage() {
    rusage usage{};
    if (::getrusage(RUSAGE_SELF, &usage) != 0) errno_error("getrusage");
    return usage;
}

class Observation {
public:
    Observation(Run& run, const Options& options, const Workload& workload, bool timed)
        : run_(run), options_(options), workload_(workload), timed_(timed) {
        if (timed_) run_.samples.resize(workload.requests.size());
        else expected_.resize(workload.max_read_size);
    }

    void start() {
        if (timed_) before_ = process_usage();
        start_ = Clock::now();
    }

    void stop() {
        if (!timed_) return;
        run_.elapsed = since(Clock::now(), start_);
        const auto after = process_usage();
        run_.user_seconds = seconds(after.ru_utime) - seconds(before_.ru_utime);
        run_.system_seconds = seconds(after.ru_stime) - seconds(before_.ru_stime);
    }

    bool scheduled() const { return timed_ && workload_.scheduled; }

    Time due(const Request& request) const { return start_ + request.arrival; }

    void completed(std::size_t index, std::span<const std::byte> buffer, Time submitted) {
        const auto& request = workload_.requests[index];
        if (timed_) {
            const auto end = Clock::now();
            const auto offered = scheduled() ? due(request) : submitted;
            run_.samples[index] = {since(end, offered), since(submitted, offered)};
        } else if (request.operation == Operation::read) {
            auto reference = std::span(expected_).first(request.size);
            pattern(reference, request.offset, options_.seed);
            independent_check(buffer, reference, request.offset);
        }
    }

private:
    static std::chrono::nanoseconds since(Time after, Time before) {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(after - before);
    }

    Run& run_;
    const Options& options_;
    const Workload& workload_;
    bool timed_;
    Buffer expected_;
    rusage before_{};
    Time start_;
};

struct Buffers {
    Buffer read;
    Buffer write;

    Buffers(const Workload& workload, std::uint64_t seed)
        : read(workload.max_read_size), write(workload.max_write_size) {
        pattern(write, 0, ~seed);
    }

    std::span<std::byte> for_request(const Request& request, std::size_t index) {
        if (request.operation == Operation::read) return std::span(read).first(request.size);
        store_word(write.data(), index + 1);
        return std::span(write).first(request.size);
    }
};

std::size_t blocking_io(const Fixture& fixture, const std::optional<sluice::File>& file,
                        const Request& request, std::span<std::byte> bytes) {
    const bool read = request.operation == Operation::read;
    if (file.has_value()) {
        const auto result = read ? sluice::blocking::read_at(*file, request.offset, bytes)
                                 : sluice::blocking::write_at(*file, request.offset, bytes);
        check(result, name(request.operation));
        return result.value();
    }
    const auto offset = static_cast<off_t>(request.offset);
    const auto result = read ? ::pread(fixture.fd(), bytes.data(), bytes.size(), offset)
                             : ::pwrite(fixture.fd(), bytes.data(), bytes.size(), offset);
    if (result < 0) errno_error(read ? "pread" : "pwrite");
    return static_cast<std::size_t>(result);
}

void run_blocking(const Options& options, const Workload& workload, const Fixture& fixture,
                  const std::optional<sluice::File>& file, Observation& observation, Run& run) {
    Buffers buffers(workload, options.seed);
    run.buffer_bytes = buffers.read.size() + buffers.write.size();
    run.peak_requests = 1;
    run.peak_bytes = std::max(workload.max_read_size, workload.max_write_size);
    observation.start();
    for (std::size_t index = 0; index < workload.requests.size(); ++index) {
        const auto& request = workload.requests[index];
        if (observation.scheduled()) std::this_thread::sleep_until(observation.due(request));
        const auto submitted = Clock::now();
        auto bytes = buffers.for_request(request, index);
        check_length(blocking_io(fixture, file, request, bytes), request, index);
        observation.completed(index, bytes, submitted);
    }
    observation.stop();
}

AsyncIoContext make_context(const Options& options) {
    if (options.backend == Backend::threadpool) {
        return make_threadpool_context({.request_capacity = options.depth, .worker_count = options.workers});
    }
    try {
        auto context = make_uring_context({.request_capacity = options.depth,
                                           .queue_depth = static_cast<unsigned>(options.depth)});
        if (!context.has_value() && context.error().code == sluice::IoError::Code::not_supported) {
            throw BackendUnavailable("Sluice SDK was built without io_uring");
        }
        check(context, "construct io_uring context");
        return std::move(context).value();
    } catch (const std::runtime_error& error) {
        const std::string_view message = error.what();
        if (message.starts_with("sluice::async::UringAsyncBackend: io_uring ")) {
            throw BackendUnavailable(error.what());
        }
        throw;
    }
}

struct Flight {
    std::size_t index;
    ScopeTicket<std::size_t> ticket;
    Time submitted;
};

struct Slot {
    Buffers buffers;
    std::optional<Flight> flight;
};

void run_async(const Options& options, const Workload& workload, const sluice::File& file,
               AsyncIoContext& context, Observation& observation, Run& run) {
    std::vector<Slot> slots;
    slots.reserve(options.depth);
    std::vector<std::size_t> free_slots;
    free_slots.reserve(options.depth);
    for (std::size_t i = 0; i < options.depth; ++i) {
        slots.push_back({Buffers(workload, options.seed), std::nullopt});
        free_slots.push_back(options.depth - 1 - i);
    }
    run.buffer_bytes = options.depth * (workload.max_read_size + workload.max_write_size);
    RequestScope scope(context, options.depth, ScopeCleanupPolicy::drain);
    std::size_t next = 0;
    std::size_t completed = 0;
    std::size_t in_flight_bytes = 0;
    auto last_progress = Clock::now();

    const auto room_for = [&](const Request& request) {
        return !free_slots.empty() &&
               (options.byte_limit == 0 || request.size <= options.byte_limit - in_flight_bytes);
    };

    observation.start();
    while (completed < workload.requests.size()) {
        bool admission_blocked = false;
        while (next < workload.requests.size()) {
            const auto& request = workload.requests[next];
            if (observation.scheduled() && Clock::now() < observation.due(request)) break;
            if (!room_for(request)) {
                admission_blocked = true;
                break;
            }
            if (free_slots.size() == options.depth) last_progress = Clock::now();
            auto& slot = slots[free_slots.back()];
            free_slots.pop_back();
            const auto submitted = Clock::now();
            const auto bytes = slot.buffers.for_request(request, next);
            const auto ticket = request.operation == Operation::read
                ? scope.submit_read({NativeFileRef(file), bytes.data(), request.size, request.offset})
                : scope.submit_write({NativeFileRef(file), bytes.data(), request.size, request.offset});
            if (!ticket.has_value()) {
                check(ticket, "submit " + std::string(name(request.operation)) +
                              " #" + std::to_string(next));
            }
            slot.flight.emplace(Flight{next, ticket.value(), submitted});
            in_flight_bytes += request.size;
            run.peak_requests = std::max(run.peak_requests, options.depth - free_slots.size());
            run.peak_bytes = std::max(run.peak_bytes, in_flight_bytes);
            ++next;
        }
        run.blocked_admissions += admission_blocked;

        const auto before = completed;
        for (std::size_t i = 0; i < slots.size(); ++i) {
            auto& slot = slots[i];
            if (!slot.flight) continue;
            auto observed = scope.take(slot.flight->ticket);
            if (observed.readiness == RequestReadiness::pending) continue;
            if (observed.readiness != RequestReadiness::ready) {
                throw std::runtime_error("lost accepted request");
            }

            const auto index = slot.flight->index;
            const auto& request = workload.requests[index];
            check(observed.result, name(request.operation));
            check_length(observed.result.value(), request, index);
            const auto& buffer = request.operation == Operation::read ? slot.buffers.read : slot.buffers.write;
            observation.completed(index, std::span(buffer).first(request.size), slot.flight->submitted);
            in_flight_bytes -= request.size;
            slot.flight.reset();
            free_slots.push_back(i);
            ++completed;
        }
        if (completed != before) {
            last_progress = Clock::now();
            continue;
        }
        if (free_slots.size() == options.depth) {
            std::this_thread::sleep_until(observation.due(workload.requests[next]));
            continue;
        }

        auto until = last_progress + 20s;
        if (observation.scheduled() && next < workload.requests.size() &&
            room_for(workload.requests[next])) {
            until = std::min(until, observation.due(workload.requests[next]));
        }
        const auto now = Clock::now();
        if (now >= last_progress + 20s) throw std::runtime_error("async progress timed out");
        if (now >= until) continue;
        const auto progress = context.wait_one(until - now);
        ++run.progress_waits;
        check(progress, "drive async progress");
        using Kind = AsyncIoContext::ProgressWaitOutcome::Kind;
        if (progress.value().kind != Kind::progress &&
            progress.value().kind != Kind::deadline_expired) {
            throw std::runtime_error("unexpected async progress control or health failure");
        }
    }
    observation.stop();
    check(scope.finish(), "finish request scope");
}

} // namespace

Run execute(const Options& options, const Workload& workload) {
    std::optional<AsyncIoContext> context;
    if (options.backend == Backend::threadpool || options.backend == Backend::uring) {
        context.emplace(make_context(options));
    }
    Fixture fixture(options.scratch_dir);
    std::optional<sluice::File> file;
    if (options.backend != Backend::posix) {
        auto opened = sluice::File::open(fixture.path(), {.access = sluice::FileAccess::read_write});
        check(opened, "open fixture");
        file.emplace(std::move(opened).value());
    }
    Run measured;
    measured.filesystem_type = fixture.filesystem_type();
    for (const bool timed : {false, true}) {
        fixture.prepare(workload, options.seed);
        Run verified;
        auto& run = timed ? measured : verified;
        if (timed) measured.resident_fraction = fixture.resident_fraction(workload.file_bytes);
        Observation observation(run, options, workload, timed);
        if (context) run_async(options, workload, *file, *context, observation, run);
        else run_blocking(options, workload, fixture, file, observation, run);
        fixture.check_final_bytes(options, workload);
    }
    return measured;
}

} // namespace bench
