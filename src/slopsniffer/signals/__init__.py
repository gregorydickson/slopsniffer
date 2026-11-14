"""L2: deterministic waste signals and cost rollups, computed in SQL over the ingested tables."""

from slopsniffer.signals.runner import KINDS, Filters, compute, report, write_report

__all__ = ["KINDS", "Filters", "compute", "report", "write_report"]
