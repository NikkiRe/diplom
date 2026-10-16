"""Метрики прогона (спецификация §7): вызовы LLM по источникам, токены, ₽ по замороженному прайсу, трафик,
p50/p95 реакции, полнота значимых ситуаций, точность ответов, N_unsafe, PSS(t) и наклон Сена, CPU-с.

    from bench.metrics import compute_metrics, summary_table
    m = compute_metrics("experiments/results/<run_id>")
    print(summary_table(m))

CLI: python -m bench.metrics <run_dir> [--out metrics.json]
"""
from .compute import MetricsConfig, analyze_trace, compute_metrics, summary_table
from .loaders import clock_seconds, discover, epoch_seconds

__all__ = ["MetricsConfig", "analyze_trace", "compute_metrics", "summary_table", "clock_seconds", "discover",
           "epoch_seconds"]
