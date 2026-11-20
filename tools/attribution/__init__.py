"""Cost-attribution modules for tools/profile_fio_sluice.py.

Layers (COST-ATTRIBUTION-METHOD.md):
  capability.py  live environment/PMU/eBPF admission -> capability.json
  runner.py      serial session orchestration (clean/pmu/record/trace)
  parse_perf.py  perf stat -x, CSV and perf report parsing
  parse_trace.py strace summaries and join-ambiguity guards
  report.py      analyze: recompute normalized costs from raw artifacts
"""
