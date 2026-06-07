"""Performance & portfolio analytics — pure, deterministic reporting helpers.

These modules consume return series, equity curves, weights, and trade lists and
produce summary statistics, tables, and rolling diagnostics. They are I/O-free and
reuse :mod:`apex.validation.metrics`; nothing here places orders or touches a broker.
"""
