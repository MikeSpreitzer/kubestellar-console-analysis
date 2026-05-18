# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Helper to write a Plotly figure to a self-contained HTML file with a
short browser-tab title.

Plotly's ``Figure.write_html`` produces an HTML document with no
``<title>`` element by default, so the browser tab is just the file
name. This wrapper writes the figure to a string, injects a
``<title>`` element with the caller-supplied short title, and writes
the result to disk.
"""

from __future__ import annotations

import html
from pathlib import Path

import plotly.graph_objects as go


def write_html_with_title(fig: go.Figure, out_path: Path, tab_title: str) -> None:
    """Write ``fig`` to ``out_path`` as a self-contained HTML file with
    ``tab_title`` shown in the browser tab.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw = fig.to_html(include_plotlyjs=True, full_html=True)
    # Inject a <title> after the opening <head>, replacing any existing
    # one. Plotly does not produce a <title> by default, so the
    # straightforward approach of inserting one near <head> is enough.
    safe = html.escape(tab_title, quote=False)
    title_tag = f"<title>{safe}</title>"
    if "<head>" in raw:
        raw = raw.replace("<head>", f"<head>{title_tag}", 1)
    else:
        # Defensive fallback; plotly's full_html=True always emits <head>.
        raw = title_tag + raw
    out_path.write_text(raw)
