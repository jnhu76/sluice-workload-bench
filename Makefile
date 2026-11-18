PROFILE ?= noring
CXXFLAGS ?= -O3 -DNDEBUG
JOBS ?= 4
SLUICE_REVISION := $(shell cat SLUICE_REVISION)
BUILD := build/$(PROFILE)
SDK := $(BUILD)/sdk
BIN := $(BUILD)/sluice-workload-bench
SOURCES := $(wildcard src/*.cpp)
HEADERS := $(wildcard src/*.hpp)

ifeq ($(PROFILE),ring)
URING_LIB := -luring
else ifneq ($(PROFILE),noring)
$(error PROFILE must be ring or noring)
endif

.PHONY: all check
all: $(BIN)

$(SDK)/.ready: SLUICE_REVISION tools/build_sdk.sh
	bash tools/build_sdk.sh "$(PROFILE)" "$(JOBS)"

$(BIN): Makefile $(SOURCES) $(HEADERS) $(SDK)/.ready
	$(CXX) $(CPPFLAGS) -isystem "$(SDK)/include" $(CXXFLAGS) -std=c++20 -Wall -Wextra -Wpedantic \
		-DSLUICE_REVISION='"$(SLUICE_REVISION)"' -DBENCH_BUILD_PROFILE='"$(PROFILE)"' \
		$(SOURCES) $(LDFLAGS) "$(SDK)/lib/libsluice_async.a" "$(SDK)/lib/libsluice_core.a" \
		$(URING_LIB) -pthread $(LDLIBS) -o "$@"

check: $(BIN)
	python3 tools/check.py "$(BIN)" $(if $(filter 1,$(REQUIRE_URING)),--require-uring)
