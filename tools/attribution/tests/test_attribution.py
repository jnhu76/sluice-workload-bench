"""Automated tests for the attribution tooling (campaign directive G9).

The 12 mandatory cases map to test methods named test_req<N>_<name>.
Run: python3 -m unittest discover -s tools/attribution/tests -v
(plus the module docstring numbering kept stable for review).
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))

from attribution import (capability, parse_bpftrace, parse_perf,  # noqa: E402
                        parse_trace, report)
import render_cost_ledger  # noqa: E402


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_run(session: pathlib.Path, run_id: str, *, fio_json: str | None,
             perf_raw: str | None = None, strace_raw: str | None = None,
             engine_load: list | None = None, discarded: bool = False,
             extra_meta: dict | None = None,
             job_text: str | None = None,
             wall_s: float | None = None) -> pathlib.Path:
    d = session / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "status": "ok", "engine_load": engine_load,
            "discarded": discarded}
    if wall_s is not None:
        meta["wall_s"] = wall_s
    if extra_meta:
        meta.update(extra_meta)
    write(d / "meta.json", json.dumps(meta))
    if job_text is not None:
        write(d / "fio-job.fio", job_text)
    if fio_json is not None:
        write(d / "fio-result.json", fio_json)
    if perf_raw is not None:
        write(d / "perf-stat.raw.txt", perf_raw)
    if strace_raw is not None:
        write(d / "strace-summary.txt", strace_raw)
    return d


def fio_doc(io_bytes: int, bs: str = "4k", iops: float = 100.0,
            clat_p99_ns: int = 1000, with_lat: bool = True) -> str:
    read = {"io_bytes": io_bytes, "iops": iops, "bw_bytes": iops * 4096,
            "clat_ns": {"percentile": {"50.000000": clat_p99_ns // 2,
                                       "95.000000": clat_p99_ns,
                                       "99.000000": clat_p99_ns}}}
    if with_lat:
        read["lat_ns"] = {"percentile": {"99.000000": clat_p99_ns * 3}}
    doc = {"jobs": [{"jobname": "job", "usr_cpu": 80.0, "sys_cpu": 10.0,
                     "iodepth_level": {"1": 100.0},
                     "job options": {"bs": bs},
                     "read": read}]}
    return json.dumps(doc)


MAIN_PERF_OK = (
    "1488000,,instructions:u,944441,100.00,,\n"
    "2408000,,cycles:u,944441,100.00,,\n"
    "940.5,msec,task-clock:u,944441,100.00,,\n"
)
MAIN_PERF_MULTIPLEXED = (
    "1488000,,instructions:u,944441,100.00,,\n"
    "1204000,,cycles:u,944441,47.00,,\n"
    "940.5,msec,task-clock:u,944441,100.00,,\n"
)
MAIN_PERF_KERNEL = (
    "1488000,,instructions:u,944441,100.00,,\n"
    "2408000,,cycles:u,944441,100.00,,\n"
    "940.5,msec,task-clock:u,944441,100.00,,\n"
    "<not counted>,,cycles:k,0,0.00,,\n"
)


class TestParsePerf(unittest.TestCase):
    def test_req2_kernel_not_fabricated(self):
        events = parse_perf.parse_perf_stat_csv(MAIN_PERF_KERNEL)
        self.assertEqual(events["cycles:k"]["kind"], "not_counted")
        self.assertIsNone(events["cycles:k"]["value"])
        row = report.per_op_row(
            {"metrics": {"lifecycle_ios": 1000},
             "perf": events, "run_id": "A1__io_uring__pmu__rep1",
             "job_text": "ramp_time=0\n"},
            "process_lifecycle")
        # kernel cycles must NOT appear as a numeric per-op value anywhere
        for label in row["values"]:
            self.assertNotIn("cycles:k", label)
            self.assertNotIn("KERNEL", label)

    def test_req3_multiplex_downgrades(self):
        events = parse_perf.parse_perf_stat_csv(MAIN_PERF_MULTIPLEXED)
        self.assertEqual(events["cycles:u"]["kind"], "scaled")
        row = report.per_op_row(
            {"metrics": {"lifecycle_ios": 1000},
             "perf": events, "run_id": "A1__io_uring__pmu__rep1",
             "job_text": "ramp_time=0\n"},
            "process_lifecycle")
        cyc = row["values"]["PROCESS_LIFECYCLE_CYCLES_per_op"]
        self.assertEqual(cyc["class"], "SCALED")
        self.assertIsNone(cyc["value"])
        self.assertEqual(row["status"], "ok")  # instructions still exact

    def test_not_counted_is_not_zero(self):
        events = parse_perf.parse_perf_stat_csv(
            "5,,LLC-loads:u,100,100.00,,\n<not counted>,,branches:u,0,0.00,,\n")
        self.assertEqual(events["LLC-loads:u"]["value"], 5)
        self.assertIsNone(events["branches:u"]["value"])
        self.assertEqual(events["branches:u"]["kind"], "not_counted")


class TestDenominator(unittest.TestCase):
    def test_req4_denominator_mismatch_refused(self):
        # io_bytes=0 -> no denominator -> per-op computation refused
        run = {"metrics": {"lifecycle_ios": None}, "perf":
               parse_perf.parse_perf_stat_csv(MAIN_PERF_OK),
               "run_id": "A1__io_uring__pmu__rep1",
               "job_text": "ramp_time=0\n"}
        row = report.per_op_row(run, "process_lifecycle")
        self.assertEqual(row["status"], "REFUSED")
        self.assertIsNone(row["denominator"])
        for label, value in row["values"].items():
            self.assertNotEqual(value.get("class"), "counted",
                                "refused row must not emit counted per-ops")

    def test_lifecycle_ios_uniform_bs(self):
        job = {"read": {"io_bytes": 4096 * 100}}
        self.assertEqual(report.lifecycle_ios(job, 4096), 100)
        job_bad = {"read": {"io_bytes": 5000}}
        self.assertIsNone(report.lifecycle_ios(job_bad, 4096))

    def test_req9_clat_not_total_latency(self):
        m = report.fio_metrics(fio_doc(4096 * 10, clat_p99_ns=1000))
        read = m["directions"]["read"]
        self.assertIn("clat_p99_us", read)
        self.assertIn("lat_p99_us", read)
        self.assertNotEqual(read["clat_p99_us"], read["lat_p99_us"])


class TestCapability(unittest.TestCase):
    def test_req1_missing_perf_unavailable(self):
        # PATH stripped of perf: every perf-derived capability must be
        # UNAVAILABLE with evidence, never zero/fabricated.
        env = {"PATH": "/nonexistent"}
        matrix = capability.probe_all(env=env)
        for row in ("user_instructions", "user_cycles", "kernel_cycles",
                    "task_clock"):
            self.assertEqual(matrix[row]["status"], "UNAVAILABLE", row)
        self.assertIn(matrix["cpu_accounting_verdict"],
                      ("PARTIAL_CPU_ACCOUNTING", "ENVIRONMENT_BLOCKED"))

    def test_probe_perf_event_missing_binary(self):
        row = capability.probe_perf_event("instructions:u", env={"PATH": ""})
        self.assertEqual(row["status"], "UNAVAILABLE")
        self.assertIn("not found", row["reason"])


class TestTraceAndJoins(unittest.TestCase):
    def test_req5_lost_events_reject_join(self):
        integrity = parse_trace.JoinIntegrity()
        integrity.observe_lost_events(
            parse_trace.detect_lost_events("Lost 927693 events"))
        verdict = integrity.verdict()
        self.assertEqual(verdict["join"], "INVALID")
        self.assertIn("lost events", verdict["reason"])

    def test_req6_duplicate_identity_ambiguous(self):
        integrity = parse_trace.JoinIntegrity()
        integrity.observe_keys([(7, 1), (7, 1), (8, 2)])
        verdict = integrity.verdict()
        self.assertEqual(verdict["join"], "AMBIGUOUS")
        self.assertIn("duplicate", verdict["reason"])

    def test_strace_summary_parsing(self):
        raw = ("% time     seconds  usecs/call     calls    errors syscall\n"
               " 15.62    1.219954          19     61631           io_uring_enter\n"
               "  2.73    0.213425        4446        48         3 futex\n")
        syscalls = parse_trace.parse_strace_summary(raw)
        self.assertEqual(syscalls["io_uring_enter"]["calls"], 61631)
        self.assertEqual(syscalls["io_uring_enter"]["errors"], 0)
        self.assertEqual(syscalls["futex"]["calls"], 48)
        self.assertEqual(syscalls["futex"]["errors"], 3)

    def test_enter_shape_parsing(self):
        raw = ("io_uring_enter(6, 1, 0, 0, NULL, 0) = 1\n"
               "io_uring_enter(6, 1, 0, 0, NULL, 0) = 1\n"
               "io_uring_enter(6, 16, 1, IORING_ENTER_GETEVENTS, NULL, 8) = 16\n")
        shape = parse_trace.parse_io_uring_enter_lines(raw)
        self.assertEqual(shape["enters"], 3)
        self.assertEqual(shape["to_submit_min_complete_flags"]
                         ["to_submit=1,min_complete=0,flags=0"], 2)
        self.assertEqual(shape["to_submit_min_complete_flags"]
                         ["to_submit=16,min_complete=1,"
                          "flags=IORING_ENTER_GETEVENTS"], 1)


def build_session(tmp: pathlib.Path, variant: str = "ok") -> pathlib.Path:
    session = tmp / ("session-" + variant)
    session.mkdir()
    rev = "5d94f233967c3aef993eb19ba9e3252c532b0951"
    write(session / "binary-manifest.json", json.dumps(
        {"sluice_revision": rev,
         "engines": {"sluice-uring": {"text_section_match": True}},
         "fio_diag": {"sha256": "fixture"}}))
    load = [rev, "ring", "fio-3.40", "uring", "16", "4"]
    ios = 4096 * 1000
    # clean: two engines x 2 reps + a psync row
    make_run(session, "A1__io_uring__clean__rep1",
             fio_json=fio_doc(ios, iops=600000.0), engine_load=None)
    make_run(session, "A1__io_uring__clean__rep2",
             fio_json=fio_doc(ios, iops=590000.0), engine_load=None)
    make_run(session, "A1__sluice-uring__clean__rep1",
             fio_json=fio_doc(ios, iops=420000.0), engine_load=load)
    make_run(session, "A1__sluice-uring__clean__rep2",
             fio_json=fio_doc(ios, iops=410000.0), engine_load=load)
    make_run(session, "F3__io_uring__clean__rep1",
             fio_json=fio_doc(ios, iops=5000.0), engine_load=None)
    # pmu with exact counts (1000 ios denominator)
    pmu_ios = 4096 * 1000
    make_run(session, "A1__io_uring__pmu__rep1",
             fio_json=fio_doc(pmu_ios, iops=600000.0),
             perf_raw=MAIN_PERF_OK, engine_load=None,
             job_text="[job]\nramp_time=0\n")
    make_run(session, "A1__sluice-uring__pmu__rep1",
             fio_json=fio_doc(pmu_ios, iops=420000.0),
             perf_raw="7440000,,instructions:u,1,100.00,,\n"
                      "12040000,,cycles:u,1,100.00,,\n"
                      "1881.0,msec,task-clock:u,1,100.00,,\n",
             engine_load=load, job_text="[job]\nramp_time=0\n")
    # crosscheck C2
    make_run(session, "C2__io_uring__pmu__rep1",
             fio_json=fio_doc(pmu_ios, iops=600000.0),
             perf_raw="1490000,,instructions:u,1,100.00,,\n"
                      "2410000,,cycles:u,1,100.00,,\n"
                      "941.0,msec,task-clock:u,1,100.00,,\n",
             engine_load=None, job_text="[job]\nramp_time=0\n")
    # record run (SAMPLED)
    d = make_run(session, "A1__sluice-uring__record__rep1",
                 fio_json=fio_doc(ios), engine_load=load)
    write(d / "perf-report-flat.txt",
          "# Samples: 5000 of event 'cycles:u'\n"
          "# Overhead  Command  Symbol\n"
          "     12.34%  fio  sluice::async::detail::RequestCore::"
          "resolve_internal_\n"
          "      5.00%  fio  fio_sluice::harvest\n")
    # trace runs
    make_run(session, "A1__sluice-uring__trace-bare__rep1",
             fio_json=fio_doc(ios, iops=420000.0), engine_load=load)
    d = make_run(session, "A1__sluice-uring__trace-straced__rep1",
                 fio_json=fio_doc(4096 * 300, iops=90000.0),
                 strace_raw=" 0.1    0.1    300    300   0 io_uring_enter\n"
                            " 0.2    0.1    900    890   2 futex\n",
                 engine_load=load)
    write(d / "event-loss.json", json.dumps(
        {"mode": "strace-count", "bpftrace_lost_events": None}))
    return session


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_req11_recompute_from_fixture(self):
        session = build_session(self.tmp)
        analysis = report.analyze_session(session)
        norm = analysis["normalized"]
        row = norm["A1/sluice-uring"]["pmu"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["denominator"], 1000)
        # 7,440,000 instructions / 1000 ios = 7440.0 per op -- recomputed
        # from raw fixture text, independently of the runner.
        self.assertEqual(row["values"]
                         ["PROCESS_LIFECYCLE_INSTRUCTIONS_per_op"]["value"],
                         7440.0)
        self.assertEqual(row["values"]
                         ["PROCESS_LIFECYCLE_CYCLES_per_op"]["value"], 12040.0)
        self.assertEqual(row["values"]
                         ["PROCESS_LIFECYCLE_CPU_TIME_us_per_op"]["value"],
                         1881.0)
        window = json.loads((session / "analysis" / "window-contract.json")
                            .read_text())
        # this fixture has no A2/C2 pair, so no per-engine delta is due
        self.assertIn("verdict", window)
        self.assertEqual(window.get("per_engine"), {})

    def test_req8_f3_not_ranked(self):
        session = build_session(self.tmp)
        analysis = report.analyze_session(session)
        self.assertTrue(analysis["invariants"]["f3_excluded_from_ranking"])
        for entry in analysis["ranking"]:
            self.assertFalse(entry["cell"].startswith("F3"))

    def test_req10_clean_profile_not_merged(self):
        session = build_session(self.tmp)
        analysis = report.analyze_session(session)
        norm = analysis["normalized"]["A1/sluice-uring"]
        self.assertIn("clean", norm)
        self.assertIn("pmu", norm)
        # each row keeps its own provenance: pmu rows come from pmu runs,
        # clean rows from clean runs; nothing is merged across modes
        self.assertIn("pmu", norm["pmu"][0]["run_id"])
        self.assertEqual(len(norm["clean"]["runs"]), 2)
        for run in norm["clean"]["runs"]:
            self.assertIn("__clean__", run["run_id"])

    def test_req12_sampled_never_exact(self):
        session = build_session(self.tmp)
        analysis = report.analyze_session(session)
        rec = analysis["normalized"]["A1/sluice-uring"]["record"][0]
        self.assertEqual(rec["class"], "SAMPLED")
        self.assertNotIn("values", rec)
        self.assertNotIn("per_op", json.dumps(rec["perf_report"]))

    def test_req7_sha_mismatch_flagged(self):
        session = build_session(self.tmp)
        load_other = ["1111111111111111111111111111111111111111",
                      "ring", "fio-3.40", "uring", "16", "4"]
        make_run(session, "A2__sluice-uring__clean__rep1",
                 fio_json=fio_doc(4096 * 1000), engine_load=load_other)
        analysis = report.analyze_session(session)
        self.assertFalse(analysis["sha_mismatch_free"])
        self.assertEqual(len(analysis["sha_mismatch"]), 1)
        self.assertEqual(analysis["sha_mismatch"][0]["run_id"],
                         "A2__sluice-uring__clean__rep1")

    def test_discarded_runs_excluded(self):
        session = build_session(self.tmp)
        make_run(session, "A1__io_uring__clean__rep3",
                 fio_json=fio_doc(4096 * 1000, iops=1.0),
                 engine_load=None, discarded=True,
                 extra_meta={"discard_reason": ["tripwire diagnostic: x"]})
        analysis = report.analyze_session(session)
        runs = analysis["normalized"]["A1/io_uring"]["clean"]["runs"]
        self.assertEqual(len(runs), 2)  # rep3 excluded, not merged

    def test_trace_rows(self):
        session = build_session(self.tmp)
        analysis = report.analyze_session(session)
        traced = analysis["normalized"]["A1/sluice-uring"]["trace-straced"][0]
        self.assertEqual(traced["label"], "STRACE_DIAGNOSTIC")
        self.assertEqual(traced["lifecycle_ios"], 300)
        self.assertEqual(traced["syscalls"]["io_uring_enter"]
                         ["calls_per_op"], 1.0)


class TestRootSession(unittest.TestCase):
    def test_bpftrace_maps_and_summary(self):
        raw = ("@enter[fio]: 1000\n"
               "@submit[fio]: 1000\n"
               "@punt[fio]: 998\n"
               "@cqe[fio]: 400\n@cqe[iou-wrk-7]: 600\n"
               "@futex[fio]: 12\n"
               "@dwell[fio]: 5000000000\n"
               "@dwell[iou-wrk-7]: 3000000000\n")
        maps = parse_bpftrace.parse_maps(raw)
        self.assertEqual(maps["enter"][("fio",)], 1000)
        self.assertEqual(maps["dwell"][("iou-wrk-7",)], 3000000000)
        summary = parse_bpftrace.summarize(maps, 1000)
        self.assertEqual(summary["per_op"]["enters"], 1.0)
        self.assertEqual(summary["per_op"]["punts"], 0.998)
        # cqe is comm-filtered: the worker's 600 completions are not this
        # run's completions (host-aggregate evidence, not per-process).
        self.assertEqual(summary["counts"]["cqe_events"], 400)
        self.assertEqual(summary["per_op"]["cqe_events"], 0.4)
        self.assertEqual(
            summary["counts"]["cqe_events_non_fio_comms_system_wide"], 600)
        self.assertEqual(summary["scope"], "HOST_ISOLATED_AGGREGATE")
        self.assertEqual(summary["fio_dwell_us_per_op"], 5000.0)
        self.assertEqual(summary["dwell_ms"]["fio"], 5000.0)

    def test_bpftrace_per_cpu_lines_aggregate(self):
        raw = ("@dwell[fio]: 100\n@dwell[fio]: 250\n"
               "@enter[fio]: 7\n@enter[fio]: 5\n")
        maps = parse_bpftrace.parse_maps(raw)
        self.assertEqual(maps["dwell"][("fio",)], 350)
        summary = parse_bpftrace.summarize(maps, 10)
        self.assertEqual(summary["per_op"]["enters"], 1.2)

    def test_req5_bpf_loss_marks_lossy(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-loss"
        (session / "runs").mkdir(parents=True)
        d = make_run(session, "A1__sluice-threadpool__trace-bpf__rep1",
                     fio_json=fio_doc(4096 * 100))
        write(d / "bpftrace.stdout.txt", "@enter[fio]: 10\n")
        write(d / "bpftrace.stderr.txt",
              "Attached 6 probes\nLost 927693 events\n")
        analysis = report.analyze_session(session)
        row = analysis["normalized"]["A1/sluice-threadpool"]["trace-bpf"][0]
        self.assertEqual(row["label"], "EBPF_LOSSY")
        self.assertIn("CAPTURE_DROPPED", row["reliability"])
        self.assertTrue(row["summary"]["dwell_invalid"])
        self.assertNotIn("fio_dwell_us_per_op", row["summary"])

    def test_bpftrace_lost_events(self):
        self.assertEqual(
            parse_bpftrace.detect_lost("Lost 927693 events\n"), 927693)
        self.assertIsNone(parse_bpftrace.detect_lost("no warning"))

    def test_root_pmu_kernel_scope(self):
        run = {"run_id": "A1__io_uring__pmu-root-g2__rep1",
               "metrics": {"lifecycle_ios": 1000,
                           "directions": {"read": {"iops": 100000.0}}},
               "job_text": "[job]\nramp_time=0\n",
               "perf": parse_perf.parse_perf_stat_csv(
                   "500000,,cycles:u,1,100.00,,\n"
                   "2500000,,cycles:k,1,100.00,,\n")}
        row = report._root_pmu_row(run)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["values"]["cycles:u"]["scope"], "user")
        self.assertEqual(row["values"]["cycles:k"]["scope"], "kernel")
        self.assertEqual(row["values"]["cycles:k"]["per_op"], 2500.0)

    def test_root_pmu_not_counted_stays_unavailable(self):
        run = {"run_id": "A1__x__pmu-root-g3__rep1",
               "metrics": {"lifecycle_ios": 1000,
                           "directions": {"read": {"iops": 1.0}}},
               "job_text": "[job]\nramp_time=0\n",
               "perf": parse_perf.parse_perf_stat_csv(
                   "<not counted>,,LLC-loads,0,0.00,,\n")}
        row = report._root_pmu_row(run)
        self.assertEqual(row["values"]["LLC-loads"]["class"], "UNAVAILABLE")
        self.assertEqual(row["status"], "REFUSED")


class TestWindowContract(unittest.TestCase):
    def test_window_delta_computed(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-win"
        session.mkdir()
        write(session / "binary-manifest.json",
              json.dumps({"sluice_revision": "rev"}))
        ios = 4096 * 1000
        make_run(session, "A2__io_uring__pmu__rep1",
                 fio_json=fio_doc(ios), wall_s=10.0,
                 job_text="[job]\nramp_time=0\n",
                 perf_raw="10000000,,instructions:u,1,100.00,,\n"
                          "20000000,,cycles:u,1,100.00,,\n"
                          "1000.0,msec,task-clock:u,1,100.00,,\n")
        make_run(session, "C2__io_uring__pmu__rep1",
                 fio_json=fio_doc(ios), wall_s=9.0,
                 job_text="[job]\nramp_time=0\n",
                 perf_raw="9900000,,instructions:u,1,100.00,,\n"
                          "19800000,,cycles:u,1,100.00,,\n"
                          "990.0,msec,task-clock:u,1,100.00,,\n")
        cells = {}
        for run_dir in sorted((session / "runs").iterdir()):
            run = report.load_run(run_dir)
            parts = report.split_run_id(run["run_id"])
            cells.setdefault((parts["cell"], parts["engine"]),
                             {}).setdefault(
                "modes", {}).setdefault(parts["mode"], []).append(run)
        window = report._window_contract(cells)
        native = window["per_engine"]["io_uring"]
        self.assertAlmostEqual(native["delta_pct_of_C"], 1.0, places=1)
        self.assertTrue(window["startup_share_bound_ok"])
        self.assertEqual(window["startup_share_bound_pct"],
                         native["delta_pct_of_C"])


class TestBpfAttribution(unittest.TestCase):
    """#639/audit C: eBPF counters are comm-keyed host aggregates."""

    def test_cqe_is_comm_filtered(self):
        raw = ("@cqe[fio]: 400\n"
               "@cqe[iou-wrk-7]: 600\n"
               "@cqe[node]: 5\n")
        maps = parse_bpftrace.parse_maps(raw)
        summary = parse_bpftrace.summarize(maps, 1000)
        self.assertEqual(summary["counts"]["cqe_events"], 400)
        self.assertEqual(
            summary["counts"]["cqe_events_non_fio_comms_system_wide"], 605)
        self.assertEqual(
            summary["counts"]["iou_wrk_cqe_events_system_wide"], 600)
        self.assertEqual(summary["per_op"]["cqe_events"], 0.4)

    def test_foreign_comm_never_enters_run_per_op(self):
        # A foreign io_uring application must not inflate this run's cqe/op.
        maps = parse_bpftrace.parse_maps(
            "@enter[fio]: 900\n@cqe[fio]: 900\n"
            "@cqe[other-ioapp]: 50000\n")
        summary = parse_bpftrace.summarize(maps, 1000)
        self.assertEqual(summary["counts"]["cqe_events"], 900)
        self.assertEqual(summary["per_op"]["cqe_events"], 0.9)

    def test_system_wide_keys_never_enter_per_op(self):
        maps = parse_bpftrace.parse_maps(
            "@dwell[iou-wrk-7]: 3000000000\n@cqe[iou-wrk-7]: 3\n")
        summary = parse_bpftrace.summarize(maps, 1000)
        self.assertNotIn("iou_wrk_dwell_ns_system_wide", summary["per_op"])
        self.assertNotIn("iou_wrk_cqe_events_system_wide", summary["per_op"])

    def test_positive_count_not_silently_zero(self):
        maps = parse_bpftrace.parse_maps(
            "@enter[fio]: 3208449\n@punt[fio]: 1\n")
        summary = parse_bpftrace.summarize(maps, 3208449)
        self.assertEqual(summary["counts"]["punts"], 1)
        self.assertEqual(summary["per_op"]["punts"], 0.0)
        # The raw count stays visible; a rounded zero must not be the
        # only representation of a real event.
        self.assertIn("punts", summary["counts_nonzero_but_per_op_zero"])
        self.assertEqual(summary["scope"], "HOST_ISOLATED_AGGREGATE")


class TestBpfRowEvidence(unittest.TestCase):
    def _run(self, maps, *, stderr="Attached 8 probes\n", lost=None,
             with_bpftrace=True, run_id="A1__io_uring__trace-bpf__rep1",
             meta=None):
        run = {"run_id": run_id,
               "meta": ({"bpf_window_s": 16, "wall_s": 10.0,
                         "bpf_attach_s": 0.2} if meta is None else meta),
               "metrics": {"lifecycle_ios": 1000,
                           "directions": {"read": {"iops": 100.0}}}}
        if with_bpftrace:
            attach = ""
            for line in stderr.splitlines():
                if line.startswith("Attached "):
                    attach = line.strip()
            run["bpftrace"] = {"maps": maps, "lost_events": lost,
                               "stderr": stderr, "attach_line": attach}
        return run

    def test_missing_receipt_is_unavailable(self):
        row = report._bpf_row(self._run({}, with_bpftrace=False))
        self.assertEqual(row["label"], "EBPF_UNAVAILABLE")
        self.assertEqual(row["scope"], "UNAVAILABLE")
        self.assertEqual(row["summary"], {})
        self.assertIn("UNAVAILABLE", row["reliability"])

    def test_empty_dump_is_unavailable(self):
        row = report._bpf_row(self._run({}))
        self.assertEqual(row["label"], "EBPF_UNAVAILABLE")
        self.assertEqual(row["scope"], "UNAVAILABLE")

    def test_missing_attach_receipt_is_unavailable(self):
        maps = parse_bpftrace.parse_maps("@enter[fio]: 10\n")
        row = report._bpf_row(self._run(maps, stderr=""))
        self.assertEqual(row["label"], "EBPF_UNAVAILABLE")
        self.assertIn("attach", row["reliability"].lower())

    def test_counted_row_has_host_scope(self):
        maps = parse_bpftrace.parse_maps("@enter[fio]: 10\n@cqe[fio]: 10\n")
        row = report._bpf_row(self._run(maps))
        self.assertEqual(row["label"], "EBPF_COUNTED")
        self.assertEqual(row["scope"], "HOST_ISOLATED_AGGREGATE")
        self.assertIn("not proof of zero loss", row["reliability"])

    def test_window_overshoot_is_not_counted(self):
        maps = parse_bpftrace.parse_maps("@enter[fio]: 10\n@cqe[fio]: 10\n")
        row = report._bpf_row(self._run(
            maps, meta={"bpf_window_s": 16, "wall_s": 15.5,
                        "bpf_attach_s": 1.0}))
        self.assertEqual(row["label"], "EBPF_WINDOW_OVERSHOOT")
        self.assertIn("lower bounds", row["reliability"])
        self.assertNotEqual(row["label"], "EBPF_COUNTED")

    def test_retry_outside_window_is_unavailable(self):
        maps = parse_bpftrace.parse_maps("@enter[fio]: 10\n@cqe[fio]: 10\n")
        row = report._bpf_row(self._run(
            maps, meta={"bpf_window_s": 16, "wall_s": 10.0,
                        "bpf_attach_s": 0.2,
                        "bpf_retry_outside_window": True}))
        self.assertEqual(row["label"], "EBPF_UNAVAILABLE")
        self.assertIn("retry", row["reliability"])

    def test_missing_window_receipt_is_unverified(self):
        maps = parse_bpftrace.parse_maps("@enter[fio]: 10\n@cqe[fio]: 10\n")
        row = report._bpf_row(self._run(maps, meta={}))
        self.assertEqual(row["label"], "EBPF_WINDOW_UNVERIFIED")

    def test_missing_window_reconstructed_from_job(self):
        maps = parse_bpftrace.parse_maps("@enter[fio]: 10\n@cqe[fio]: 10\n")
        run = self._run(maps, meta={"wall_s": 10.0, "bpf_attach_s": 0.2})
        run["job_text"] = "[job]\nruntime=10\nramp_time=0\n"
        row = report._bpf_row(run)
        self.assertEqual(row["label"], "EBPF_COUNTED")
        self.assertEqual(row["window"]["window_s"], 14)
        self.assertEqual(row["window"]["window_basis"],
                         "RECONSTRUCTED_FROM_JOB")

    def test_lossy_row_is_lower_bound_and_no_exact_dwell(self):
        maps = parse_bpftrace.parse_maps(
            "@enter[fio]: 10\n@cqe[fio]: 10\n")
        row = report._bpf_row(self._run(maps, lost=927693))
        self.assertEqual(row["label"], "EBPF_LOSSY")
        self.assertIn("lower bound", row["reliability"])
        self.assertNotIn("fio_dwell_us_per_op", row["summary"])
        self.assertTrue(row["summary"]["dwell_invalid"])

    def test_renderer_consumes_lossy_rows(self):
        maps = parse_bpftrace.parse_maps(
            "@enter[fio]: 10\n@cqe[fio]: 10\n")
        row = report._bpf_row(self._run(maps, lost=7))
        out = render_cost_ledger.render_bpf(
            {"A1/sluice-uring": {"trace-bpf": [row]}})
        self.assertIn("EBPF_LOSSY", out)
        self.assertIn("lower bound", out)
        self.assertIn("dwell INVALID", out)

    def test_threadpool_io_uring_counters_are_not_applicable(self):
        maps = parse_bpftrace.parse_maps(
            "@futex[fio]: 100\n@dwell[fio]: 1000\n")
        summary = parse_bpftrace.summarize(
            maps, 100, io_uring_applicable=False)
        self.assertIsNone(summary["counts"]["enters"])
        self.assertNotIn("enters", summary["per_op"])
        self.assertNotIn("cqe_events", summary["per_op"])
        self.assertIn("enters", summary["not_applicable"])
        self.assertEqual(summary["per_op"]["futex"], 1.0)
        row = report._bpf_row(self._run(
            maps, run_id="A1__sluice-threadpool__trace-bpf__rep1"))
        self.assertEqual(row["label"], "EBPF_COUNTED")
        self.assertIsNone(row["summary"]["counts"]["enters"])
        self.assertNotIn("enters", row["summary"]["per_op"])

    def test_maps_without_fio_keys_are_unavailable(self):
        maps = parse_bpftrace.parse_maps(
            "@futex[node]: 500\n@cqe[other-ioapp]: 900\n")
        row = report._bpf_row(self._run(maps))
        self.assertEqual(row["label"], "EBPF_UNAVAILABLE")
        self.assertIn("fio-keyed", row["reliability"])

    def test_missing_required_io_uring_maps_are_unavailable(self):
        maps = parse_bpftrace.parse_maps("@futex[fio]: 100\n")
        row = report._bpf_row(self._run(
            maps, run_id="A1__sluice-uring__trace-bpf__rep1"))
        self.assertEqual(row["label"], "EBPF_UNAVAILABLE")
        self.assertIn("required io_uring maps missing", row["reliability"])
        self.assertEqual(row["scope"], "UNAVAILABLE")


class TestWindowAndLabels(unittest.TestCase):
    def test_per_op_refuses_nonzero_ramp(self):
        run = {"run_id": "A1__io_uring__pmu__rep1",
               "job_text": "[job]\nramp_time=2\n",
               "metrics": {"lifecycle_ios": 1000},
               "perf": parse_perf.parse_perf_stat_csv(MAIN_PERF_OK)}
        row = report.per_op_row(run, "process_lifecycle")
        self.assertEqual(row["status"], "REFUSED")
        self.assertFalse(row["window_verified"])
        self.assertIn("ramp_time", row["reason"])
        for value in row["values"].values():
            self.assertNotEqual(value.get("class"), "counted")

    def test_per_op_refuses_missing_ramp_receipt(self):
        run = {"run_id": "A1__io_uring__pmu__rep1",
               "metrics": {"lifecycle_ios": 1000},
               "perf": parse_perf.parse_perf_stat_csv(MAIN_PERF_OK)}
        row = report.per_op_row(run, "process_lifecycle")
        self.assertEqual(row["status"], "REFUSED")
        self.assertFalse(row["window_verified"])

    def test_ramp_zero_substring_is_not_matched(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-ramp-substring"
        session.mkdir()
        make_run(session, "A1__io_uring__clean0__rep1",
                 fio_json=fio_doc(4096 * 1000),
                 job_text="[job]\nramp_time=0.5\n",
                 extra_meta={"rusage": {"cpu_user_s": 1.0,
                                        "cpu_sys_s": 0.5,
                                        "ctx_voluntary": 10,
                                        "ctx_involuntary": 2}})
        analysis = report.analyze_session(session)
        clean0 = analysis["normalized"]["A1/io_uring"]["clean0"]
        self.assertFalse(clean0["window_matched"])
        self.assertNotIn("rusage_per_op", clean0)

    def test_root_pmu_row_labels_and_scope(self):
        run = {"run_id": "A1__io_uring__pmu-root-g2__rep1",
               "job_text": "[job]\nramp_time=0\n",
               "metrics": {"lifecycle_ios": 1000,
                           "directions": {"read": {"iops": 1.0}}},
               "perf": parse_perf.parse_perf_stat_csv(
                   "500000,,cycles:u,1,100.00,,\n"
                   "2500000,,cycles:k,1,100.00,,\n")}
        row = report._root_pmu_row(run)
        self.assertEqual(row["denominator_kind"], "lifecycle_ios")
        self.assertIn("PROCESS_LIFECYCLE", row["label"])
        self.assertIn("user+sys", row["scope_note"])
        self.assertTrue(row["window_verified"])

    def test_root_pmu_row_refuses_nonzero_ramp(self):
        run = {"run_id": "A1__io_uring__pmu-root-g2__rep1",
               "job_text": "[job]\nramp_time=2\n",
               "metrics": {"lifecycle_ios": 1000,
                           "directions": {"read": {"iops": 1.0}}},
               "perf": parse_perf.parse_perf_stat_csv(
                   "500000,,cycles:u,1,100.00,,\n")}
        row = report._root_pmu_row(run)
        self.assertEqual(row["status"], "REFUSED")
        self.assertIn("ramp_time", row["reason"])

    def test_aux_row_status_ok_when_aux_counted(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-aux"
        session.mkdir()
        make_run(session, "A1__io_uring__pmu-aux1__rep1",
                 fio_json=fio_doc(4096 * 1000),
                 job_text="[job]\nramp_time=0\n",
                 perf_raw="500000,,branches:u,1,100.00,,\n")
        analysis = report.analyze_session(session)
        row = analysis["normalized"]["A1/io_uring"]["pmu-aux1"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["main_status"], "REFUSED")
        self.assertEqual(row["aux_events"]["branches:u"]["class"], "counted")

    def test_per_op_class_only_native_within_gate(self):
        self.assertEqual(report._per_op_class("io_uring", 1.01),
                         "lifecycle_primary")
        self.assertEqual(report._per_op_class("io_uring", 10.01),
                         "lifecycle_only")
        self.assertEqual(report._per_op_class("sluice-uring", 7.408),
                         "lifecycle_only")
        self.assertEqual(report._per_op_class("sluice-threadpool", 4.143),
                         "lifecycle_only")


class TestLedgerRenderer(unittest.TestCase):
    def _session(self, tmp: pathlib.Path) -> pathlib.Path:
        session = tmp / "session-render"
        (session / "analysis").mkdir(parents=True)
        norm = {"A1/io_uring": {
            "cell": "A1", "engine": "io_uring",
            "pmu": [{"status": "ok", "denominator": 1000,
                     "values": {
                         "PROCESS_LIFECYCLE_INSTRUCTIONS_per_op":
                             {"value": 2000.0, "class": "counted"},
                         "PROCESS_LIFECYCLE_CYCLES_per_op":
                             {"value": 1000.0, "class": "counted"},
                         "PROCESS_LIFECYCLE_CPU_TIME_us_per_op":
                             {"value": 1.5, "class": "counted"}}}]}}
        (session / "analysis" / "normalized-costs.json").write_text(
            json.dumps(norm))
        (session / "analysis" / "window-contract.json").write_text(
            json.dumps({"per_engine": {}, "verdict": "X"}))
        return session

    def test_fill_ledger_replaces_marked_sections(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = self._session(tmp)
        ledger = tmp / "LEDGER.md"
        ledger.write_text(
            "intro\n<!-- LEDGER-DATA:BEGIN -->\nold\n"
            "<!-- LEDGER-DATA:END -->\n"
            "<!-- TRACE-DATA:BEGIN -->\nold\n<!-- TRACE-DATA:END -->\n"
            "<!-- PERTURBATION:BEGIN -->\nold\n<!-- PERTURBATION:END -->\n"
            "tail\n")
        render_cost_ledger.fill_ledger(session, ledger)
        text = ledger.read_text()
        self.assertIn("| A1 | io_uring | 1 |", text)
        self.assertNotIn("\nold\n", text)
        self.assertIn("intro", text)
        self.assertIn("tail", text)

    def test_fill_ledger_errors_without_markers(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = self._session(tmp)
        ledger = tmp / "LEDGER.md"
        ledger.write_text("no markers here\n")
        with self.assertRaises(SystemExit):
            render_cost_ledger.fill_ledger(session, ledger)

    def test_render_h_reads_per_engine_delta(self):
        out = render_cost_ledger.render_h(
            {}, {"verdict": "V",
                 "per_engine": {"io_uring": {"delta_pct_of_C": 1.009}}})
        self.assertIn("1.009", out)
        self.assertNotIn("None", out)


class TestShaGate(unittest.TestCase):
    def test_record_run_without_manifest_fails_closed(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-nomanifest"
        (session / "runs").mkdir(parents=True)
        make_run(session, "A1__sluice-uring__record__rep1",
                 fio_json=fio_doc(4096 * 1000))
        analysis = report.analyze_session(session)
        self.assertFalse(analysis["sha_mismatch_free"])
        self.assertFalse(analysis["binary_manifest"]["present"])

    def test_record_manifest_without_engine_proof_fails_closed(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-noproof"
        (session / "runs").mkdir(parents=True)
        rev = "5d94f233967c3aef993eb19ba9e3252c532b0951"
        write(session / "binary-manifest.json", json.dumps(
            {"sluice_revision": rev, "engines": {},
             "fio_diag": {"sha256": "x"}}))
        make_run(session, "A1__sluice-uring__record__rep1",
                 fio_json=fio_doc(4096 * 1000),
                 engine_load=[rev, "ring", "fio-3.40", "uring", "16", "4"])
        analysis = report.analyze_session(session)
        self.assertFalse(analysis["sha_mismatch_free"])
        self.assertEqual(analysis["binary_manifest"]["gate"],
                         "TEXT_SECTION_MISMATCH")
        self.assertIn("sluice-uring", analysis["sha_mismatch_text"])

    def test_record_run_without_engine_load_fails_closed(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-noload"
        (session / "runs").mkdir(parents=True)
        rev = "5d94f233967c3aef993eb19ba9e3252c532b0951"
        write(session / "binary-manifest.json", json.dumps({
            "sluice_revision": rev,
            "engines": {"sluice-uring": {"text_section_match": True}},
            "fio_diag": {"sha256": "x"}}))
        make_run(session, "A1__sluice-uring__record__rep1",
                 fio_json=fio_doc(4096 * 1000), engine_load=None)
        analysis = report.analyze_session(session)
        self.assertFalse(analysis["sha_mismatch_free"])
        self.assertEqual(analysis["binary_manifest"]["gate"],
                         "ENGINE_LOAD_MISSING")
        self.assertEqual(analysis["sha_mismatch_provenance"],
                         ["A1__sluice-uring__record__rep1"])

    def test_text_section_mismatch_fails_closed(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-textmismatch"
        (session / "runs").mkdir(parents=True)
        rev = "5d94f233967c3aef993eb19ba9e3252c532b0951"
        write(session / "binary-manifest.json", json.dumps({
            "sluice_revision": rev,
            "engines": {"sluice-uring": {"text_section_match": False}},
            "fio_diag": {"sha256": "x"}}))
        make_run(session, "A1__sluice-uring__record__rep1",
                 fio_json=fio_doc(4096 * 1000),
                 engine_load=[rev, "ring", "fio-3.40", "uring", "16", "4"])
        analysis = report.analyze_session(session)
        self.assertFalse(analysis["sha_mismatch_free"])
        self.assertIn("sluice-uring", analysis["sha_mismatch_text"])


class TestRunnerRepairs(unittest.TestCase):
    def test_retry_wrap_paths_retargeted(self):
        import sys as _sys
        _sys.path.insert(0, str(TOOLS))
        from attribution import runner
        attempt = "A1__io_uring__record__rep1"
        retry = attempt + "r"
        kwargs = {"wrap": ["perf", "record", "-o",
                           "/base/runs/%s/perf.data" % attempt, "--"],
                  "job_text": "x"}
        out = runner.Session._retarget_wrap(kwargs, attempt, retry)
        self.assertIn("/base/runs/%s/perf.data" % retry,
                      " ".join(out["wrap"]))
        self.assertIn("/base/runs/%s/perf.data" % attempt,
                      " ".join(kwargs["wrap"]))

    def test_attach_barrier_reads_receipt(self):
        import sys as _sys
        _sys.path.insert(0, str(TOOLS))
        from attribution import runner
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        err = tmp / "err.txt"
        err.write_text("Attached 8 probes\n")
        proc = mock.Mock()
        proc.poll.return_value = None
        line = runner.Session._wait_for_attach(err, proc, timeout=1.0)
        self.assertEqual(line, "Attached 8 probes")

    def test_attach_barrier_times_out_without_receipt(self):
        import sys as _sys
        _sys.path.insert(0, str(TOOLS))
        from attribution import runner
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        err = tmp / "err.txt"
        err.write_text("no attach line\n")
        proc = mock.Mock()
        proc.poll.return_value = None
        line = runner.Session._wait_for_attach(err, proc, timeout=0.2)
        self.assertEqual(line, "")

    def test_mode_record_postprocess_uses_retry_dir(self):
        import sys as _sys
        _sys.path.insert(0, str(TOOLS))
        from attribution import runner
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = runner.Session(tmp / "results", tmp / "scratch")
        session.prep_file = lambda label, size: tmp / "dummy.bin"
        attempt = "A1__io_uring__record__rep1"
        retry = attempt + "r"

        def fake_retry(run_id, **kwargs):
            rd = session.runs_dir / retry
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "perf.data").write_bytes(b"perfdata")
            (rd / "meta.json").write_text(json.dumps(
                {"run_id": retry, "status": "ok"}))
            return {"run_id": retry, "status": "ok", "failures": []}

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "report", "")

        session.run_with_retry = fake_retry
        with mock.patch.object(runner.subprocess, "run",
                               side_effect=fake_run) as run_patch:
            session.mode_record(["A1"], ["io_uring"])
        accepted = session.runs_dir / retry
        for name in ("perf-header.txt", "perf-report.txt",
                     "perf-report-flat.txt", "perf.data.sha256"):
            self.assertTrue((accepted / name).exists(), name)
        self.assertFalse(
            (session.runs_dir / attempt / "perf-report.txt").exists())
        for call in run_patch.call_args_list:
            argv = call.args[0]
            if "-i" in argv:
                self.assertIn(retry, argv[argv.index("-i") + 1])

    def test_mode_trace_bpf_retry_marks_outside_window(self):
        import sys as _sys
        _sys.path.insert(0, str(TOOLS))
        from attribution import runner
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = runner.Session(tmp / "results", tmp / "scratch",
                                 sudo_pw="TEST_SENTINEL_NOT_A_SECRET")
        session.prep_file = lambda label, size: tmp / "dummy.bin"
        attempt = "A1__io_uring__trace-bpf__rep1"
        retry = attempt + "r"

        def fake_retry(run_id, **kwargs):
            # Simulate run_fio re-using a previously failed run_id: the
            # first attempt directory (with the live bpftrace files) is
            # renamed to -failed before the retry directory is created.
            attempt_dir = session.runs_dir / attempt
            if attempt_dir.exists():
                attempt_dir.rename(session.runs_dir / (attempt + "-failed"))
            rd = session.runs_dir / retry
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "meta.json").write_text(json.dumps(
                {"run_id": retry, "status": "ok"}))
            return {"run_id": retry, "status": "ok", "failures": []}

        proc = mock.Mock()
        proc.returncode = 0
        proc.wait.return_value = 0
        session.run_with_retry = fake_retry
        with mock.patch.object(runner.subprocess, "Popen",
                               return_value=proc), \
                mock.patch.object(runner.Session, "_wait_for_attach",
                                  return_value="Attached 8 probes"):
            session.mode_trace_bpf(["A1"], ["io_uring"])
        accepted = session.runs_dir / retry
        failed = session.runs_dir / (attempt + "-failed")
        self.assertTrue((failed / "bpftrace.stdout.txt").exists())
        self.assertTrue((failed / "bpftrace.stderr.txt").exists())
        self.assertTrue((accepted / "bpf-counts.bt").exists())
        self.assertFalse((accepted / "bpftrace.stdout.txt").exists())
        self.assertFalse((accepted / "bpftrace.stderr.txt").exists())
        scope = json.loads((accepted / "bpf-scope.json").read_text())
        self.assertFalse(scope["counts_attributable"])
        self.assertEqual(scope["trace_attempt"], attempt)
        meta = json.loads((accepted / "meta.json").read_text())
        self.assertEqual(meta["bpf_attach"], "Attached 8 probes")
        self.assertEqual(meta["bpftrace_returncode"], 0)
        self.assertGreater(meta["bpf_window_s"], 0)
        self.assertGreaterEqual(meta["bpf_attach_s"], 0)
        self.assertTrue(meta["bpf_retry_outside_window"])

    def test_mode_record_root_reports_follow_retry_dir(self):
        import sys as _sys
        _sys.path.insert(0, str(TOOLS))
        from attribution import runner
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = runner.Session(tmp / "results", tmp / "scratch",
                                 sudo_pw="TEST_SENTINEL_NOT_A_SECRET")
        session.prep_file = lambda label, size: tmp / "dummy.bin"

        def fake_retry(run_id, **kwargs):
            rd = session.runs_dir / (run_id + "r")
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "perf.data").write_bytes(b"perfdata")
            (rd / "meta.json").write_text(json.dumps(
                {"run_id": run_id + "r", "status": "ok"}))
            return {"run_id": run_id + "r", "status": "ok",
                    "failures": []}

        reported = []
        session.run_with_retry = fake_retry
        session._perf_reports = (
            lambda run_dir, prefix, callgraph:
            reported.append(pathlib.Path(run_dir)))
        session.mode_record_root(["A1"], ["io_uring"],
                                 kernel_cycles=True)
        self.assertEqual(reported, [
            session.runs_dir / "A1__io_uring__record-root__rep1r",
            session.runs_dir / "A1__io_uring__record-root-k__rep1r"])

    def test_mode_trace_event_loss_follows_retry_dir(self):
        import sys as _sys
        _sys.path.insert(0, str(TOOLS))
        from attribution import runner
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = runner.Session(tmp / "results", tmp / "scratch")
        session.prep_file = lambda label, size: tmp / "dummy.bin"

        def fake_retry(run_id, **kwargs):
            rd = session.runs_dir / (run_id + "r")
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "meta.json").write_text(json.dumps(
                {"run_id": run_id + "r", "status": "ok"}))
            return {"run_id": run_id + "r", "status": "ok",
                    "failures": []}

        session.run_with_retry = fake_retry
        session.mode_trace(["A1"], ["io_uring"])
        accepted = (session.runs_dir /
                    "A1__io_uring__trace-straced__rep1r")
        self.assertTrue((accepted / "event-loss.json").exists())
        self.assertFalse(
            (session.runs_dir / "A1__io_uring__trace-straced__rep1" /
             "event-loss.json").exists())


class TestCapabilityScopes(unittest.TestCase):
    def test_verdict_name_and_scope_lists(self):
        matrix = capability.probe_all(env={"PATH": "/nonexistent"})
        self.assertIn("cpu_accounting_scopes", matrix)
        self.assertIn("cpu_accounting_verdict", matrix)
        scopes = matrix["cpu_accounting_scopes"]
        self.assertIn("not_measured", scopes)
        self.assertIn("io-wq/kworker CPU attributed to this run",
                      scopes["not_measured"])
        self.assertIn("IRQ/softirq system cost", scopes["not_measured"])

    def test_probe_perf_event_uses_sudo_password_via_stdin(self):
        calls = []

        def fake_run(argv, timeout=20.0, env=None, input_text=None):
            calls.append({"argv": argv, "input_text": input_text})
            return {"argv": argv, "returncode": 0,
                    "stdout": "1,,cycles:k,1,100.00,,\n", "stderr": ""}

        sentinel = "TEST_SENTINEL_NOT_A_SECRET"
        with mock.patch.object(capability, "_which",
                               return_value="/usr/bin/perf"), \
                mock.patch.object(capability, "_run", side_effect=fake_run):
            row = capability.probe_perf_event("cycles:k", sudo_pw=sentinel)
        self.assertEqual(row["status"], "SUPPORTED")
        self.assertTrue(any(c["argv"][:3] == ["sudo", "-S", "-p"]
                            for c in calls))
        for call in calls:
            self.assertNotIn(sentinel, " ".join(call["argv"]))
        self.assertTrue(any(c["input_text"] == sentinel + "\n"
                            for c in calls))

    def test_probe_perf_record_uses_sudo_password_via_stdin(self):
        calls = []

        def fake_run(argv, timeout=20.0, env=None, input_text=None):
            calls.append({"argv": argv, "input_text": input_text})
            if "-o" in argv:
                pathlib.Path(argv[argv.index("-o") + 1]).write_bytes(b"x")
            return {"argv": argv, "returncode": 0,
                    "stdout": "true\n", "stderr": ""}

        sentinel = "TEST_SENTINEL_NOT_A_SECRET"
        with mock.patch.object(capability, "_which",
                               return_value="/usr/bin/perf"), \
                mock.patch.object(capability, "_run", side_effect=fake_run):
            row = capability.probe_perf_record(sudo_pw=sentinel)
        self.assertEqual(row["status"], "SUPPORTED")
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["argv"][:4], ["sudo", "-S", "-p", ""])
            self.assertNotIn(sentinel, " ".join(call["argv"]))
            self.assertEqual(call["input_text"], sentinel + "\n")


class TestWindowContractRepairs(unittest.TestCase):
    def test_root_pmu_modes_count_as_option_b(self):
        cells = {("A1", "sluice-uring"):
                 {"modes": {"pmu-root-g1": [], "pmu-root-g2": []}}}
        window = report._window_contract(cells)
        self.assertEqual(window["option_B"], "present")
        self.assertEqual(window["option_C"], "absent")
        self.assertEqual(window["option_C_kind"], "ABSENT")
        self.assertEqual(window["verdict"],
                         "LIFECYCLE_ONLY_NO_STEADY_STATE_EXTRAPOLATION")
        self.assertIn("reason", window)

    def test_direction_opposite_is_not_startup_proof(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-direction"
        (session / "runs").mkdir(parents=True)
        ios = 4096 * 1000
        make_run(session, "A2__io_uring__pmu__rep1",
                 fio_json=fio_doc(ios), wall_s=10.0,
                 job_text="[job]\nramp_time=0\n",
                 perf_raw="10000000,,instructions:u,1,100.00,,\n")
        # C2 is SHORTER yet has a LOWER per-op count: the opposite of what
        # startup amortization predicts.
        make_run(session, "C2__io_uring__pmu__rep1",
                 fio_json=fio_doc(ios), wall_s=8.5,
                 job_text="[job]\nramp_time=0\n",
                 perf_raw="9900000,,instructions:u,1,100.00,,\n")
        cells = {}
        for run_dir in sorted((session / "runs").iterdir()):
            run = report.load_run(run_dir)
            parts = report.split_run_id(run["run_id"])
            cells.setdefault((parts["cell"], parts["engine"]), {}) \
                .setdefault("modes", {}).setdefault(
                    parts["mode"], []).append(run)
        window = report._window_contract(cells)
        native = window["per_engine"]["io_uring"]
        self.assertEqual(native["direction_vs_startup"], "OPPOSITE")
        self.assertEqual(native["cross_mode_mechanism"],
                         "MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED")
        self.assertEqual(native["cross_mode_uncertainty_pct"],
                         native["delta_pct_of_C"])
        self.assertEqual(window["startup_share_bound_scope"],
                         "NATIVE_TOTAL_CROSS_MODE_DIFF_UPPER_BOUND")
        self.assertIn("not", window["note"].lower())


class TestAnalyzeEbpfRows(unittest.TestCase):
    def test_analysis_exposes_ebpf_row_classes(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        session = tmp / "session-ebpf"
        (session / "runs").mkdir(parents=True)
        d = make_run(session, "A1__sluice-uring__trace-bpf__rep1",
                     fio_json=fio_doc(4096 * 1000))
        write(d / "bpftrace.stdout.txt",
              "@enter[fio]: 1000\n@cqe[fio]: 1000\n")
        write(d / "bpftrace.stderr.txt",
              "Attached 8 probes\nLost 5 events\n")
        analysis = report.analyze_session(session)
        self.assertEqual(analysis["ebpf"]["lossy_runs"],
                         ["A1__sluice-uring__trace-bpf__rep1"])
        self.assertEqual(analysis["ebpf"]["counted_runs"], [])


if __name__ == "__main__":
    unittest.main()
