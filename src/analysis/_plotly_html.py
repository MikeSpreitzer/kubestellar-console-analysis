# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Helper to write a Plotly figure to a self-contained HTML file with a
short browser-tab title and (optionally) a selectable, wrapping
in-page heading.

Plotly's ``Figure.write_html`` produces an HTML document with no
``<title>`` element, so the browser tab is just the file name. The
chart's title set via ``fig.update_layout(title=...)`` is rendered as
SVG text, which is not text-selectable and does not wrap. This
wrapper:

1. Injects a ``<title>`` element so the browser tab shows
   ``tab_title``.
2. Optionally injects an HTML ``<h2>`` heading above the figure so
   readers can select it and so it wraps on narrow viewports. The
   caller is expected to clear the SVG title (``fig.update_layout(
   title=None)``) before calling this if they're using the in-page
   heading instead, otherwise both will render.
"""

from __future__ import annotations

import html
from pathlib import Path

import plotly.graph_objects as go


def write_html_with_title(
    fig: go.Figure,
    out_path: Path,
    tab_title: str,
    *,
    page_heading: str | None = None,
) -> None:
    """Write ``fig`` to ``out_path`` as a self-contained HTML file.

    ``tab_title`` becomes the browser-tab title (escaped, single-line).
    If ``page_heading`` is given, it is inserted as a wrapping,
    selectable ``<h2>`` element above the figure in the body.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw = fig.to_html(include_plotlyjs=True, full_html=True)
    safe_tab = html.escape(tab_title, quote=False)
    title_tag = f"<title>{safe_tab}</title>"
    if "<head>" in raw:
        raw = raw.replace("<head>", f"<head>{title_tag}", 1)
    else:
        # Defensive fallback; plotly's full_html=True always emits <head>.
        raw = title_tag + raw

    if page_heading is not None:
        safe_heading = html.escape(page_heading, quote=False)
        # Wrap in a div with a max-width so very long headings don't
        # extend past the chart on wide viewports, but still wrap on
        # narrow ones. font-family/-size mirror plotly's default
        # title for visual continuity.
        heading_html = (
            f"<div style=\"max-width: 95%; margin: 0.5em auto 0; "
            f"font-family: 'Open Sans', verdana, arial, sans-serif; "
            f"font-size: 17px; font-weight: 500; color: #444; "
            f"text-align: center;\">{safe_heading}</div>"
        )
        # Plotly's full_html output puts the figure inside the body;
        # insert the heading right after the opening <body>.
        if "<body>" in raw:
            raw = raw.replace("<body>", f"<body>{heading_html}", 1)
        else:
            raw = heading_html + raw

    out_path.write_text(raw)
