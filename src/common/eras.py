# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""ACMM era boundaries and plot annotation helpers.

Per DESIGN.md the kubestellar/console corpus has gone through six
ACMM-paper layers in its short history. Time-series plots should
mark those boundaries as point annotations on the time axis so a
reader can see categorical era differences across a continuous curve.

The dates here are user-supplied estimates of the *start* of each
era. They live in this single module so every analysis module that
plots time series gets the same boundaries; updating them here
affects every plot.

This module also hosts the shared ``to_week`` helper used by every
weekly-binned analysis module, so the week-anchor convention is
defined once.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd


# (label, ISO date for the start of the era).
ERA_BOUNDARIES: list[tuple[str, str]] = [
    ("L1", "2026-01-09"),
    ("L2", "2026-01-23"),
    ("L3", "2026-02-06"),
    ("L4", "2026-03-06"),
    ("L5", "2026-04-03"),
    ("L6", "2026-05-01"),
]

# Pre-parsed (label, tz-aware UTC Timestamp) pairs. Built once at
# module import so per-plot annotation calls don't re-parse the same
# six ISO strings. annotate_matplotlib / annotate_plotly / era_dates
# all iterate this rather than ERA_BOUNDARIES directly.
_ERA_TIMESTAMPS: list[tuple[str, pd.Timestamp]] = [
    (label, pd.to_datetime(iso, utc=True))
    for label, iso in ERA_BOUNDARIES
]


def to_week(ts: pd.Series) -> pd.Series:
    """Floor a tz-aware UTC datetime Series to ISO Monday (start of week).

    Shared between every weekly-binned analysis module so the
    week-anchor convention is single-sourced. The ``W-SUN`` period
    spec means weeks are Mon..Sun; ``start_time`` returns the Monday.

    NaT in ``ts`` is preserved as NaT in the output (callers using
    ``groupby([week, ...])`` will silently drop NaT keys, so any
    upstream that wants those rows must dropna before grouping).
    """
    return (
        ts.dt.tz_convert("UTC")
        .dt.to_period("W-SUN").dt.start_time.dt.tz_localize("UTC")
    )


def annotate_matplotlib(
    ax,
    *,
    xlim: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    color: str = "gray",
    alpha: float = 0.45,
    linestyle: str = "--",
    linewidth: float = 0.8,
    label_y: float = 0.97,
) -> None:
    """Draw vertical dashed lines on a matplotlib Axes for each era
    boundary, with a small label at the top.

    ``xlim``, when given, restricts which boundaries are drawn (only
    those falling inside the range). Useful when an analysis only
    plots a subset of the corpus's lifetime.
    """
    for label, ts in _ERA_TIMESTAMPS:
        if xlim is not None and (ts < xlim[0] or ts > xlim[1]):
            continue
        ax.axvline(
            ts, color=color, alpha=alpha,
            linestyle=linestyle, linewidth=linewidth,
        )
        ax.text(
            ts, label_y, label,
            transform=ax.get_xaxis_transform(),
            ha="center", va="top",
            fontsize=8, color=color,
        )


def annotate_plotly(
    fig,
    *,
    xlim: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    color: str = "gray",
    line_dash: str = "dash",
) -> None:
    """Draw vertical dashed lines on a Plotly Figure for each era
    boundary, with a small label at the top of the plotting area.

    We add the line shape and the label annotation as separate calls
    instead of using ``add_vline(..., annotation_text=...)``. Plotly's
    annotated-vline path computes ``float(sum([x, x]))/2`` to position
    the annotation, which raises ``TypeError`` when ``x`` is a
    ``pd.Timestamp`` (newer pandas no longer supports ``int +
    Timestamp``). Splitting the call sidesteps that.
    """
    for label, ts in _ERA_TIMESTAMPS:
        if xlim is not None and (ts < xlim[0] or ts > xlim[1]):
            continue
        fig.add_shape(
            type="line",
            xref="x", yref="paper",
            x0=ts, x1=ts, y0=0, y1=1,
            line=dict(color=color, dash=line_dash, width=1),
            opacity=0.45,
        )
        fig.add_annotation(
            x=ts, y=1.0,
            xref="x", yref="paper",
            text=label,
            showarrow=False,
            yanchor="bottom",
            font=dict(size=10, color=color),
        )


def era_dates() -> Iterable[pd.Timestamp]:
    """Iterator of era-boundary timestamps as tz-aware UTC pd.Timestamp.
    Useful for callers that want the dates without the labels (e.g.,
    to compute the date range of a particular era)."""
    for _, ts in _ERA_TIMESTAMPS:
        yield ts
