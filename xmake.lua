-- xmake build for the Sluice workload bench and the fio external engine
-- (docs/fio-integration/ADAPTER-ARCHITECTURE §7; replaces the Makefile).
--
-- Targets:
--   sluice-workload-bench        bench binary, noring non-PIC SDK
--   sluice-workload-bench-ring   bench binary, ring non-PIC SDK + -luring
--   fio-sluice-noring            fio external engine .so, noring PIC SDK
--   fio-sluice-ring              fio external engine .so, ring PIC SDK
--   fio-sluice-errno-map-test    IoError->errno mapping unit test
--   fio-sluice-lifecycle-test    engine lifecycle contract test (links the
--                                real engine TUs against stub fio-core
--                                callbacks; SLUICE_FIO_INIT_SEAM is defined
--                                for this target only)
--
-- SDK readiness reuses tools/build_sdk.sh unchanged for the non-PIC
-- prefixes (the script honors SLUICE_SOURCE). The PIC prefixes replicate
-- the same flow with --cxxflags=-fPIC in a separate source tree and install
-- prefix, because a static SDK built without -fPIC cannot link into a
-- shared object and tools/build_sdk.sh has no pic variant. Each profile
-- keeps its own prefix; liburing profiles are never mixed into noring ones.
--
-- `xmake clean -a` removes build/ entirely, including the frozen fio tree
-- and the installed SDKs; both are recreated on demand by the hooks below.
--
-- Scope note: xmake evaluates this file in a restricted description scope
-- (no io, no os.execv, no try), while hook bodies run with a fuller
-- environment. Functions that need those globals therefore take an explicit
-- `api` table captured in the hook body; only base Lua is used lexically.
set_project("sluice-workload-bench")
set_languages("c++20")
set_warnings("all", "extra")

add_rules("mode.debug", "mode.release")

local root = os.projectdir()
local build_root = path.join(root, "build")
local targetdir = path.join(build_root, "linux", "x86_64", "release")

local fio_tag = "fio-3.40"
local fio_commit = "ff930c4653ae3952d6b09ab3ec89671aeabf2cbe"
local fio_dir = path.join(build_root, "fio", "upstream")

local jobs = tonumber(os.getenv("JOBS")) or 4

local function trim(value)
    return (value or ""):gsub("%s+", "")
end

-- api = { io = io, os = os, try = try } captured in a hook body.
local function api_shell_ok(api, program, argv)
    local os, try = api.os, api.try
    local ok = false
    try {
        function()
            os.execv(program, argv)
            ok = true
        end,
        catch = function() end,
    }
    return ok
end

local function api_shell_output(api, cmd)
    local os, try = api.os, api.try
    local out = nil
    try {
        function()
            out = os.iorun(cmd)
        end,
        catch = function() end,
    }
    return trim(out)
end

local function api_sluice_remote(api)
    local os = api.os
    local env_source = os.getenv("SLUICE_SOURCE")
    if env_source and env_source ~= "" then
        return path.absolute(env_source)
    end
    return "https://github.com/jnhu76/Sluice.git"
end

local function sdk_prefix(profile, pic)
    return path.join(build_root, pic and (profile .. "-pic") or profile)
end

local function api_build_pic_sdk(api, profile, revision)
    local io, os = api.io, api.os
    local prefix = sdk_prefix(profile, true)
    local source = path.join(prefix, "sluice")
    local sdk = path.join(prefix, "sdk")
    local liburing = profile == "ring" and "y" or "n"
    os.mkdir(source)
    os.mkdir(sdk)
    if not os.isdir(path.join(source, ".git")) then
        os.execv("git", { "-C", source, "init", "-q" })
    end
    if not api_shell_ok(api, "git", { "-C", source, "cat-file", "-e",
                                      revision .. "^{commit}" }) then
        os.execv("git", { "-C", source, "fetch", "--depth", "1", "--no-tags",
                          api_sluice_remote(api), revision })
    end
    os.execv("git", { "-C", source, "checkout", "-q", "--detach", revision })
    local dirty = api_shell_output(api, "git -C " .. source ..
                                       " status --porcelain --untracked-files=no")
    if dirty ~= "" then
        error("Sluice SDK source checkout " .. source .. " contains tracked edits")
    end
    -- -P pins every call to this checkout (after the subcommand: this xmake
    -- version otherwise resolves nested invocations to the outermost
    -- ancestor xmake.lua, which is this repo). Run with the checkout as the
    -- working directory: the nested xmake takes the working directory's
    -- project lock, and taking this repo's lock here deadlocks the parent
    -- build that already holds it.
    local olddir = os.cd(source)
    os.execv("xmake", { "f", "-P", source, "-m", "release",
                        "--liburing=" .. liburing, "--cxxflags=-fPIC", "-y" })
    os.execv("xmake", { "build", "-P", source, "-j", tostring(jobs), "-y",
                        "sluice_async" })
    os.execv("xmake", { "install", "-P", source, "-o", sdk, "sluice_core" })
    os.execv("xmake", { "install", "-P", source, "-o", sdk, "sluice_async" })
    os.cd(olddir)
    io.writefile(path.join(sdk, ".ready"), revision .. "\n")
end

-- tools/build_sdk.sh stays untouched and stays the builder for the non-PIC
-- prefixes. Its internal plain `xmake` calls run inside
-- build/<profile>/sluice, where xmake 3.1.2 would resolve to this repo (the
-- outermost ancestor xmake.lua) and reject --liburing. The shim directory
-- prepended to PATH for the script's process tree injects `-P .` so each
-- call builds the checkout the script cd'd into, which is exactly the
-- script's intent.
local function api_run_build_sdk(api, profile)
    local io, os = api.io, api.os
    local real_xmake = api_shell_output(api, "command -v xmake")
    if real_xmake == "" then
        error("xmake not found in PATH")
    end
    local shim_dir = path.join(build_root, "xmake-local-shim")
    os.mkdir(shim_dir)
    io.writefile(path.join(shim_dir, "xmake"),
        "#!/bin/sh\n" ..
        "# compatibility for xmake 3.1.2 nested-project resolution\n" ..
        "if [ $# -eq 0 ]; then\n" ..
        "    exec " .. real_xmake .. " -P .\n" ..
        "fi\n" ..
        "command=\"$1\"\n" ..
        "shift\n" ..
        "exec " .. real_xmake .. " \"$command\" -P . \"$@\"\n")
    api_shell_ok(api, "chmod", { "+x", path.join(shim_dir, "xmake") })
    os.execv("bash", { path.join(root, "tools", "build_sdk.sh"), profile,
                       tostring(jobs) },
             { envs = { PATH = shim_dir .. ":" .. os.getenv("PATH") } })
end

local function api_ensure_sdk(api, profile, pic)
    local io = api.io
    local revision = trim(io.readfile(path.join(root, "SLUICE_REVISION")))
    if revision == "" then
        error("SLUICE_REVISION is empty or missing")
    end
    local ready = path.join(sdk_prefix(profile, pic), "sdk", ".ready")
    if os.isfile(ready) and trim(io.readfile(ready)) == revision then
        return
    end
    if pic then
        api_build_pic_sdk(api, profile, revision)
    else
        api_run_build_sdk(api, profile)
    end
    if not (os.isfile(ready) and trim(io.readfile(ready)) == revision) then
        error("SDK readiness stamp missing or stale after build: " .. ready)
    end
end

local function api_ensure_fio_source(api)
    local os = api.os
    if not os.isdir(path.join(fio_dir, ".git")) then
        os.mkdir(fio_dir)
        os.execv("git", { "clone", "--depth", "1", "--branch", fio_tag,
                          "https://github.com/axboe/fio.git", fio_dir })
    end
    local tag = api_shell_output(api, "git -C " .. fio_dir ..
                                     " describe --tags --exact-match")
    local head = api_shell_output(api, "git -C " .. fio_dir .. " rev-parse HEAD")
    if tag ~= fio_tag or head ~= fio_commit then
        error("frozen fio tree " .. fio_dir .. " is " .. tostring(tag) .. "@" ..
              tostring(head) .. ", expected " .. fio_tag .. "@" .. fio_commit)
    end
    if not os.isfile(path.join(fio_dir, "config-host.h")) then
        local olddir = os.cd(fio_dir)
        os.execv(path.join(fio_dir, "configure"), {})
        os.cd(olddir)
    end
end

local function link_setup(target_sdk, profile)
    -- Link the SDK archives by name: -L/-l ordering survives both the
    -- binary and the shared-engine link steps (file-style ldflags are
    -- dropped from shared-target link lines).
    add_linkdirs(path.join(target_sdk, "lib"))
    add_links("sluice_async", "sluice_core")
    if profile == "ring" then
        add_links("uring")
    end
    add_ldflags("-pthread", { force = true })
end

local function bench_target(name, profile)
    local sdk = path.join(sdk_prefix(profile, false), "sdk")
    target(name)
        set_kind("binary")
        add_files("src/*.cpp")
        add_cxxflags("-isystem " .. path.join(sdk, "include"), "-Wpedantic", { force = true })
        link_setup(sdk, profile)
        on_load(function(target)
            if io ~= nil and io.readfile ~= nil then
                local revision = trim(io.readfile(path.join(root, "SLUICE_REVISION")))
                if revision ~= "" then
                    target:add("defines", 'SLUICE_REVISION="' .. revision .. '"',
                               'BENCH_BUILD_PROFILE="' .. profile .. '"')
                end
            end
        end)
        before_build(function()
            api_ensure_sdk({ io = io, os = os, try = try }, profile, false)
        end)
end

local function engine_target(name, profile)
    local sdk = path.join(sdk_prefix(profile, true), "sdk")
    target(name)
        set_kind("shared")
        set_filename(name .. ".so")
        add_files("adapters/fio/src/*.cpp")
        add_includedirs(fio_dir)
        add_cxxflags("-isystem " .. path.join(sdk, "include"), { force = true })
        link_setup(sdk, profile)
        on_load(function(target)
            if io ~= nil and io.readfile ~= nil then
                local revision = trim(io.readfile(path.join(root, "SLUICE_REVISION")))
                if revision ~= "" then
                    target:add("defines", 'SLUICE_REVISION="' .. revision .. '"',
                               'SLUICE_BUILD_PROFILE="' .. profile .. '"',
                               'FIO_SOURCE_TAG="' .. fio_tag .. '"')
                end
            end
        end)
        before_build(function()
            local api = { io = io, os = os, try = try }
            api_ensure_sdk(api, profile, true)
            api_ensure_fio_source(api)
        end)
end

bench_target("sluice-workload-bench", "noring")
bench_target("sluice-workload-bench-ring", "ring")
engine_target("fio-sluice-noring", "noring")
engine_target("fio-sluice-ring", "ring")

target("fio-sluice-errno-map-test")
    set_kind("binary")
    add_files("adapters/fio/src/errno_map.cpp", "adapters/fio/tests/errno_map_test.cpp")
    add_includedirs("adapters/fio/src")
    add_cxxflags("-isystem " .. path.join(sdk_prefix("noring", false), "sdk", "include"),
                 { force = true })
    before_build(function()
        api_ensure_sdk({ io = io, os = os, try = try }, "noring", false)
    end)

target("fio-sluice-lifecycle-test")
    set_kind("binary")
    add_files("adapters/fio/src/engine.cpp", "adapters/fio/src/queue.cpp",
              "adapters/fio/src/events.cpp", "adapters/fio/src/files.cpp",
              "adapters/fio/src/errno_map.cpp",
              "adapters/fio/tests/lifecycle_test.cpp")
    add_includedirs("adapters/fio/src")
    add_includedirs(fio_dir)
    add_defines("SLUICE_FIO_INIT_SEAM")
    add_cxxflags("-isystem " .. path.join(sdk_prefix("noring", false), "sdk", "include"),
                 { force = true })
    link_setup(path.join(sdk_prefix("noring", false), "sdk"), "noring")
    before_build(function()
        api_ensure_sdk({ io = io, os = os, try = try }, "noring", false)
        api_ensure_fio_source({ io = io, os = os, try = try })
    end)

-- check.py runs small functional workloads: only ever invoked explicitly.
task("check")
    set_menu {
        description = "run tools/check.py on the noring bench binary (build it first)",
        usage = "xmake check",
        options = {},
    }
    on_run(function()
        local bin = path.join(targetdir, "sluice-workload-bench")
        if not os.isfile(bin) then
            error("build first: xmake build -y sluice-workload-bench")
        end
        os.execv("python3", { path.join(root, "tools", "check.py"), bin })
    end)
task_end()

task("check-ring")
    set_menu {
        description = "run tools/check.py --require-uring on the ring bench binary",
        usage = "xmake check-ring",
        options = {},
    }
    on_run(function()
        local bin = path.join(targetdir, "sluice-workload-bench-ring")
        if not os.isfile(bin) then
            error("build first: xmake build -y sluice-workload-bench-ring")
        end
        os.execv("python3", { path.join(root, "tools", "check.py"), bin, "--require-uring" })
    end)
task_end()
