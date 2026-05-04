"""Round 9b2 paper-ledger analysis library.

Pure-function helpers consumed by ``tools/analyze_paper_ledger.py`` to
turn the joined DataFrame produced by 9b1 into a calibration report,
chart bundle, and machine-readable summary stats blob.

The split is deliberate: the orchestrator (``analyze_paper_ledger.py``)
owns I/O, argparse, and ledger-loading; this package owns the
statistics, plotting, and rendering.  Two reasons:

1. Library-direct unit tests can exercise the analysis surface against
   synthetic in-memory DataFrames without round-tripping through
   ``main()`` and JSONL files.  9b2a ships those tests; 9b2b adds
   end-to-end orchestrator tests on top.
2. A future round that wants the same analyses against a different
   data source (e.g. a SQLite migration of the JSONL ledger) reuses
   this package without dragging the JSONL-specific orchestrator with
   it.

Public entry point for the orchestrator is :func:`report.render_all`.
"""
