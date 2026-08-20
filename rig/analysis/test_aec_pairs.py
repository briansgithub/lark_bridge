from __future__ import annotations

from rig.analysis.aec_pairs import relative_reduction, summary_stats


def test_summary_stats_reports_confidence_interval() -> None:
    summary = summary_stats([1.0] * 10)
    assert summary["n"] == 10
    assert summary["mean_ci95_lower"] == 1.0
    assert summary["mean_ci95_upper"] == 1.0


def test_relative_reduction_uses_requested_statistic() -> None:
    baseline = [10.0] * 10
    candidate = [8.0] * 10
    assert relative_reduction(baseline, candidate, "median") == 20.0
    assert relative_reduction(baseline, candidate, "p95") == 20.0
