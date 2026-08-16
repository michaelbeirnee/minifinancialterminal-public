"""Thesis engine: signal detection, thesis tracking and the loop between them.

The engine is built bottom-up and deterministic-first. ``bulk`` ingests SEC's
quarterly insider-transaction archives, ``collapse`` folds the filing artefacts
that make one decision look like several, and ``families`` classifies and scores
what survives. None of it needs an API key or a model.

``spine`` holds the theses themselves and grades them against their own
falsifiers. ``memory`` records everything both halves emit, ``scheduler`` runs
the grading that stamps realised outcomes onto those records, and the base rates
that fall out go back into ``triage`` — which is the whole point: a gate weight
that has been measured beats one that was guessed.
"""
