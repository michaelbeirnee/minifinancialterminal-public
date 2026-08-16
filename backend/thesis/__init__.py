"""Thesis engine: signal detection, calibration and (later) thesis tracking.

The engine is built bottom-up and deterministic-first. ``bulk`` ingests SEC's
quarterly insider-transaction archives, ``collapse`` folds the filing artefacts
that make one decision look like several, and ``families`` classifies and scores
what survives. None of it needs an API key or a model.
"""
