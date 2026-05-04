"""Operational tooling for the BTC PM arb agent.

Lives outside ``src/btc_pm_arb`` because these modules are not part of
the runtime agent — they're invoked manually (or by CI) for analysis,
diagnostics, and one-off data work.  Adding them to the runtime package
would force every deployment to ship pandas / pyarrow / matplotlib
even when no analysis is being run; instead, the heavyweight deps live
in the ``analysis`` extras group in pyproject.toml.
"""
