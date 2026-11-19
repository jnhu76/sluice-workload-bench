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
import sys
import tempfile
import unittest

TOOLS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))

from attribution import capability, parse_perf, parse_trace, report  # noqa: E402


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_run(session: pathlib.Path, run_id: str, *, fio_json: str | None,
             perf_raw: str | None = None, strace_raw: str | None = None,
             engine_load: list | None = None, discarded: bool = False,
             extra_meta: dict | None = None) -> pathlib.Path:
    d = session / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "status": "ok", "engine_load": engine_load,
            "discarded": discarded}
    if extra_meta:
        meta.update(extra_meta)
    write(d / "meta.json", json.dumps(meta))
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
             "perf": events, "run_id": "A1__io_uring__pmu__rep1"},
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
             "perf": events, "run_id": "A1__io_uring__pmu__rep1"},
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
               "run_id": "A1__io_uring__pmu__rep1"}
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
        raw = (" 0.000012    0.000012    12        11         0 io_uring_enter\n"
               " 3.100000    0.041000    74000     73000      2 futex\n")
        syscalls = parse_trace.parse_strace_summary(raw)
        self.assertEqual(syscalls["io_uring_enter"]["calls"], 11)
        self.assertEqual(syscalls["futex"]["calls"], 73000)
        self.assertEqual(syscalls["futex"]["errors"], 2)

    def test_enter_shape_parsing(self):
        raw = ("io_uring_enter(6, 1, 0, ...) = 1\n"
               "io_uring_enter(6, 1, 0, ...) = 1\n"
               "io_uring_enter(6, 16, 1, ...) = 16\n")
        shape = parse_trace.parse_io_uring_enter_lines(raw)
        self.assertEqual(shape["enters"], 3)
        self.assertEqual(shape["to_submit_min_complete"][(1, 0)], 2)
        self.assertEqual(shape["to_submit_min_complete"][(16, 1)], 1)


def build_session(tmp: pathlib.Path, variant: str = "ok") -> pathlib.Path:
    session = tmp / ("session-" + variant)
    session.mkdir()
    rev = "5d94f233967c3aef993eb19ba9e3252c532b0951"
    write(session / "binary-manifest.json", json.dumps(
        {"sluice_revision": rev}))
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
             perf_raw=MAIN_PERF_OK, engine_load=None)
    make_run(session, "A1__sluice-uring__pmu__rep1",
             fio_json=fio_doc(pmu_ios, iops=420000.0),
             perf_raw="7440000,,instructions:u,1,100.00,,\n"
                      "12040000,,cycles:u,1,100.00,,\n"
                      "1881.0,msec,task-clock:u,1,100.00,,\n",
             engine_load=load)
    # crosscheck C2
    make_run(session, "C2__io_uring__pmu__rep1",
             fio_json=fio_doc(pmu_ios, iops=600000.0),
             perf_raw="1490000,,instructions:u,1,100.00,,\n"
                      "2410000,,cycles:u,1,100.00,,\n"
                      "941.0,msec,task-clock:u,1,100.00,,\n",
             engine_load=None)
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
        # A2 io_uring absent here -> no delta; verdict stays lifecycle
        self.assertEqual(window["verdict"], "PROCESS_LIFECYCLE_PRIMARY")

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
        # clean IOPS comes ONLY from clean runs; pmu rows carry no iops
        self.assertNotIn("iops", norm["pmu"][0])
        self.assertEqual(len(norm["clean"]["runs"]), 2)

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
                 fio_json=fio_doc(ios),
                 perf_raw="10000000,,instructions:u,1,100.00,,\n"
                          "20000000,,cycles:u,1,100.00,,\n"
                          "1000.0,msec,task-clock:u,1,100.00,,\n")
        make_run(session, "C2__io_uring__pmu__rep1",
                 fio_json=fio_doc(ios),
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
        self.assertAlmostEqual(window["A2_vs_C2_instructions_delta_pct"],
                               1.0, places=1)
        self.assertTrue(window["startup_share_bound_ok"])


if __name__ == "__main__":
    unittest.main()
