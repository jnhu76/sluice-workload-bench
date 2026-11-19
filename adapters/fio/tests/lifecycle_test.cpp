// Engine lifecycle contract tests: close_file fd-borrow invariant and init
// allocation-failure containment (ADAPTER-ARCHITECTURE §4.1/§4.7).
// Links the real engine TUs against stub fio-core callbacks, so the code
// under test is exactly what ships in the .so profiles (plus the
// SLUICE_FIO_INIT_SEAM hook compiled in only for this binary).
//
// Standard/system headers come first; state.hpp pulls the Sluice headers and
// then fio_cxx.hpp with fio.h last (minmax.h macros, see events.cpp).
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <fcntl.h>
#include <new>
#include <string>

#include "state.hpp"

#include "callbacks.hpp"
#include "options.hpp"

using fio_sluice::EngineOptions;
using fio_sluice::EngineState;

namespace {

int failures = 0;
int close_calls = 0; // incremented only by the generic_close_file stub
int marker_fd = -1;  // violation child writes 'X' here if the stub ever runs

void check(bool ok, const std::string& what) {
    if (ok) {
        std::printf("ok: %s\n", what.c_str());
    } else {
        std::printf("FAIL %s\n", what.c_str());
        ++failures;
    }
}

bool ensure_dir(const std::string& path) {
    if (mkdir(path.c_str(), 0755) == 0 || errno == EEXIST) {
        return true;
    }
    if (errno == ENOENT) {
        const auto slash = path.find_last_of('/');
        if (slash != std::string::npos && slash > 0 &&
            ensure_dir(path.substr(0, slash))) {
            return mkdir(path.c_str(), 0755) == 0 || errno == EEXIST;
        }
    }
    return false;
}

// The seam throws from inside sluice_init (built with SLUICE_FIO_INIT_SEAM).
int seam_fail_step = -1;
bool seam_throw_custom = false;

// Fresh thread_data + bare EngineState per scenario: open_file/close_file
// need only io_ops_data plus slots/files, no context or scope.
struct Scenario {
    thread_data* td;
    EngineState* state;

    explicit Scenario(std::size_t slot_count)
        : td(new thread_data{}), state(new EngineState{}) {
        state->slots.resize(slot_count);
        td->io_ops_data = state;
    }
    ~Scenario() {
        delete td;
        delete state;
    }
    Scenario(const Scenario&) = delete;
    Scenario& operator=(const Scenario&) = delete;
};

} // namespace

// The five fio-core symbols fio_cxx.hpp declares, resolved for the test
// binary. The close stub is the ONLY closer of test fds, which makes
// "stub not called while borrowed" equivalent to "fd not closed".
extern "C" ssize_t log_err(const char* format, ...) {
    va_list args;
    va_start(args, format);
    std::vprintf(format, args);
    va_end(args);
    return 0;
}

extern "C" int generic_open_file(struct thread_data*, struct fio_file*) {
    return 0; // the test pre-sets f->fd on a real open()ed fd
}

extern "C" int generic_close_file(struct thread_data*, struct fio_file* f) {
    if (marker_fd >= 0) {
        const char byte = 'X';
        (void)write(marker_fd, &byte, 1);
    }
    const int r = close(f->fd);
    f->fd = -1;
    ++close_calls;
    return r == 0 ? 0 : errno;
}

extern "C" int generic_get_file_size(struct thread_data*, struct fio_file*) {
    return 0;
}

extern "C" void io_u_mark_submit(struct thread_data*, unsigned int) {}

// Thrown by the seam on request; deliberately NOT derived from
// std::exception so the catch(...) arm is exercised.
struct SeamedBoom {};

extern "C" void fio_sluice_init_seam(int step) {
    if (step != seam_fail_step) {
        return;
    }
    if (seam_throw_custom) {
        throw SeamedBoom{};
    }
    throw std::bad_alloc{};
}

namespace {

int open_scratch(const std::string& path) {
    return open(path.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0600);
}

// T1.1: no in-flight borrows -> close_file is a plain pass-through close.
void t1_normal_close(const std::string& dir) {
    const std::string path = dir + "/t1-normal.bin";
    Scenario s(1);
    struct fio_file f{};
    f.fd = open_scratch(path);
    check(f.fd >= 0, "t1 normal: scratch file opened");
    check(fio_sluice::sluice_open_file(s.td, &f) == 0,
          "t1 normal: open_file succeeded");
    check(s.state->files.count(f.fd) == 1, "t1 normal: fd registered");

    const int before = close_calls;
    check(fio_sluice::sluice_close_file(s.td, &f) == 0,
          "t1 normal: close_file returned 0");
    check(close_calls == before + 1, "t1 normal: stub closer ran once");
    check(fcntl(f.fd, F_GETFD) == -1 && errno == EBADF,
          "t1 normal: fd really closed (stub validated)");
    unlink(path.c_str());
}

// T1.2: a live borrow on a DIFFERENT file must not block this close; the
// scan predicate matches slot.u->file == f exactly.
void t1_borrow_on_other_file(const std::string& dir) {
    const std::string path1 = dir + "/t1-other-a.bin";
    const std::string path2 = dir + "/t1-other-b.bin";
    Scenario s(1);
    struct fio_file f1{};
    struct fio_file f2{};
    static struct io_u borrowed{};
    f1.fd = open_scratch(path1);
    f2.fd = open_scratch(path2);
    check(f1.fd >= 0 && f2.fd >= 0, "t1 other-borrow: scratch files opened");
    check(fio_sluice::sluice_open_file(s.td, &f1) == 0 &&
              fio_sluice::sluice_open_file(s.td, &f2) == 0,
          "t1 other-borrow: both fds registered");
    borrowed.file = &f2; // kind stays none: the close scan reads only ->file
    s.state->slots[0].u = &borrowed;

    const int before = close_calls;
    check(fio_sluice::sluice_close_file(s.td, &f1) == 0,
          "t1 other-borrow: close of the un-borrowed file proceeded");
    check(close_calls == before + 1 && fcntl(f1.fd, F_GETFD) == -1,
          "t1 other-borrow: f1 fd closed");
    check(fcntl(f2.fd, F_GETFD) >= 0, "t1 other-borrow: f2 fd still open");

    s.state->slots[0].u = nullptr;
    check(fio_sluice::sluice_close_file(s.td, &f2) == 0 &&
              close_calls == before + 2,
          "t1 other-borrow: f2 closes normally once its slot clears");
    unlink(path1.c_str());
    unlink(path2.c_str());
}

// T1.3: a staged completion still referencing f is not a live borrow (the
// io_u was already reaped); close must proceed. Pins the predicate to
// live slots only.
void t1_staged_completion(const std::string& dir) {
    const std::string path = dir + "/t1-staged.bin";
    Scenario s(0);
    struct fio_file f{};
    static struct io_u done{};
    f.fd = open_scratch(path);
    check(f.fd >= 0, "t1 staged: scratch file opened");
    check(fio_sluice::sluice_open_file(s.td, &f) == 0,
          "t1 staged: fd registered");
    done.file = &f;
    s.state->staged.push_back(EngineState::Completion{&done});

    const int before = close_calls;
    check(fio_sluice::sluice_close_file(s.td, &f) == 0 &&
              close_calls == before + 1,
          "t1 staged: staged completion does not block close");
    unlink(path.c_str());
}

// T1.4: contract guard for close-with-in-flight, invoked synthetically. The
// audited fio 3.40 refcount gate (close_file only fires at f->references==0
// and every io_u holds a reference until reaped) makes this state
// unreachable through real fio, so the callback is called directly. The
// engine must fail closed (terminate) without closing the fd; the pipe EOF
// (stub never wrote) plus SIGABRT are the receipt, no sleeps needed.
void t1_violation_fails_closed(const std::string& dir) {
    const std::string path = dir + "/t1-violation.bin";
    int pipefd[2];
    if (pipe(pipefd) != 0) {
        check(false, "t1 violation: pipe created");
        return;
    }
    const pid_t pid = fork();
    if (pid == 0) {
        // Child: any failure below uses distinct exit codes so a RED run is
        // diagnosable from the wait status alone.
        close(pipefd[0]);
        marker_fd = pipefd[1];
        struct thread_data* td = new thread_data{};
        EngineState* state = new EngineState{};
        state->slots.resize(1);
        td->io_ops_data = state;
        struct fio_file f{};
        struct io_u borrowed{};
        f.fd = open_scratch(path);
        if (f.fd < 0 || fio_sluice::sluice_open_file(td, &f) != 0) {
            _exit(9);
        }
        borrowed.file = &f;
        state->slots[0].u = &borrowed;
        fio_sluice::sluice_close_file(td, &f);
        // Reached only when the invariant check let the close through (the
        // stub has already written 'X' by then): the observable RED signal.
        _exit(42);
    }
    close(pipefd[1]);
    char byte = 0;
    const ssize_t n = read(pipefd[0], &byte, 1); // EOF == stub never ran
    int status = 0;
    const pid_t waited = waitpid(pid, &status, 0);
    check(waited == pid, "t1 violation: child reaped");
    check(n == 0, "t1 violation: stub closer never ran while borrowed");
    check(WIFSIGNALED(status) && WTERMSIG(status) == SIGABRT,
          "t1 violation: engine failed closed (SIGABRT)");
    close(pipefd[0]);
    unlink(path.c_str());
}

// T2: every throwing init step must be contained to a return-1 callback
// (nothing escapes into fio, io_ops_data stays null, RAII rolls the state
// back), and a subsequent init on the same td must succeed -- proof that a
// failed init leaves no leaked progress owner behind.
void t2_init_failure_contained() {
    static thread_data td{};
    static EngineOptions eo{};
    td.o.iodepth = 4;
    eo.pad = nullptr;
    eo.backend = const_cast<char*>("threadpool");
    eo.workers = 2;
    td.eo = &eo;

    const struct Variant {
        int step;
        bool custom;
        const char* label;
    } variants[] = {
        {0, false, "t2 step0 bad_alloc"},
        {1, false, "t2 step1 bad_alloc"},
        {1, true, "t2 step1 custom"},
        {2, false, "t2 step2 bad_alloc"},
    };
    for (const auto& variant : variants) {
        seam_fail_step = variant.step;
        seam_throw_custom = variant.custom;
        const int r = fio_sluice::sluice_init(&td);
        check(r == 1, std::string(variant.label) + ": failed init returned 1");
        check(td.io_ops_data == nullptr,
              std::string(variant.label) + ": no state published");

        seam_fail_step = -1;
        seam_throw_custom = false;
        check(fio_sluice::sluice_init(&td) == 0,
              std::string(variant.label) + ": re-init succeeded");
        fio_sluice::sluice_cleanup(&td);
        check(td.io_ops_data == nullptr,
              std::string(variant.label) + ": cleanup unpublished state");
    }
}

} // namespace

int main(int argc, char** argv) {
    // Unbuffered: the RED run aborts mid-test and buffered output would be
    // lost from the receipt log.
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    const std::string dir = argc > 1 ? argv[1] : "build/scratch/lifecycle";
    if (!ensure_dir(dir)) {
        std::printf("FAIL cannot create scratch dir %s\n", dir.c_str());
        return 1;
    }

    t1_normal_close(dir);
    t1_borrow_on_other_file(dir);
    t1_staged_completion(dir);
    t1_violation_fails_closed(dir);
    t2_init_failure_contained();

    if (failures != 0) {
        std::printf("lifecycle_test: %d failure(s)\n", failures);
        return 1;
    }
    // All scenario files are unlinked above; drop the scratch dir itself so
    // callers with their own cleanliness checks (smoke.sh) see no leftovers.
    rmdir(dir.c_str());
    std::printf("lifecycle_test: PASS\n");
    return 0;
}
