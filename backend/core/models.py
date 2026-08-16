"""The container every platform command hands back.

Mirrors the shape of an OpenBB ``OBBject``: the payload lives in ``results``,
with the serving provider, any non-fatal warnings and command-specific extras
alongside it, plus convenience converters to pandas/JSON.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

import pandas as pd

from .utils import Records, to_records


class Result:
    """What a provider function returns before normalisation.

    Wrapping the payload lets a provider report which source actually served
    the request (important when a command falls back through several) and
    attach warnings without changing the data's shape.
    """

    __slots__ = ("data", "provider", "warnings", "extra", "index_name")

    def __init__(
        self,
        data: Any,
        provider: Optional[str] = None,
        warnings: Optional[Sequence[str]] = None,
        extra: Optional[Dict[str, Any]] = None,
        index_name: Optional[str] = None,
    ) -> None:
        self.data = data
        self.provider = provider
        self.warnings = list(warnings or [])
        self.extra = dict(extra or {})
        self.index_name = index_name


class MFTObject:
    """Normalised command output."""

    __slots__ = ("results", "provider", "warnings", "extra", "chart", "command")

    def __init__(
        self,
        results: Union[Records, Dict[str, Any], None],
        provider: Optional[str] = None,
        warnings: Optional[Sequence[str]] = None,
        extra: Optional[Dict[str, Any]] = None,
        chart: Optional[Dict[str, Any]] = None,
        command: Optional[str] = None,
    ) -> None:
        self.results = results if results is not None else []
        self.provider = provider
        self.warnings = list(warnings or [])
        self.extra = dict(extra or {})
        self.chart = chart
        self.command = command

    # -- conversions -------------------------------------------------------
    def to_df(self) -> pd.DataFrame:
        """Records as a DataFrame, date-indexed when the rows carry a date."""
        rows = self.results if isinstance(self.results, list) else [self.results]
        df = pd.DataFrame(rows)
        for col in ("date", "period_ending", "timestamp"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce", format="mixed")
                df = df.set_index(col).sort_index()
                break
        return df

    # OpenBB muscle-memory aliases.
    to_dataframe = to_df

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "results": self.results,
            "provider": self.provider,
            "warnings": self.warnings,
        }
        if self.extra:
            payload["extra"] = self.extra
        if self.chart is not None:
            payload["chart"] = self.chart
        return payload

    def to_records(self) -> Records:
        return self.results if isinstance(self.results, list) else [self.results]

    # -- container sugar ---------------------------------------------------
    def __len__(self) -> int:
        return len(self.results) if isinstance(self.results, list) else 1

    def __iter__(self) -> Iterator[Any]:
        return iter(self.results if isinstance(self.results, list) else [self.results])

    def __getitem__(self, item: Any) -> Any:
        return self.results[item]

    def __repr__(self) -> str:
        n = len(self)
        cols: List[str] = []
        rows = self.to_records()
        if rows and isinstance(rows[0], dict):
            cols = list(rows[0].keys())[:8]
        return (
            "MFTObject(command={c!r}, provider={p!r}, rows={n}, columns={cols})".format(
                c=self.command, p=self.provider, n=n, cols=cols
            )
        )


def build_object(raw: Any, command: str, provider_hint: Optional[str] = None) -> MFTObject:
    """Coerce whatever a command returned into an :class:`MFTObject`."""
    if isinstance(raw, MFTObject):
        raw.command = raw.command or command
        return raw
    if isinstance(raw, Result):
        return MFTObject(
            results=to_records(raw.data, index_name=raw.index_name),
            provider=raw.provider or provider_hint,
            warnings=raw.warnings,
            extra=raw.extra,
            command=command,
        )
    return MFTObject(results=to_records(raw), provider=provider_hint, command=command)
