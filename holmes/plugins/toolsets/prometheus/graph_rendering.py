"""Render Prometheus range-query results into PNG charts, inside Holmes.

Historically the Robusta *runner* turned ``execute_prometheus_range_query``
results into chart images (pygal -> SVG -> PNG via cairosvg). To decouple bots
from the runner, that rendering moves here: when enabled, the Prometheus
toolset attaches a rendered PNG to the tool result's ``images`` field, which
already flows to vision-capable LLMs and to chat clients.

Design notes:
- ``pygal`` and ``cairosvg`` are **optional** and imported lazily. If they are
  not installed (cairosvg needs native cairo libs), rendering degrades to a
  no-op rather than breaking the tool — exactly how the runner treats cairosvg
  as optional.
- Rendering never raises into the toolset: any failure logs and returns
  ``None`` / leaves ``images`` untouched.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

PNG_MIME_TYPE = "image/png"


def _format_value(value: float, output_type: str) -> float:
    """Best-effort y-value normalization mirroring the runner's ChartValuesFormat.

    Only ``Percentage`` (ratio -> percent) is transformed; everything else is
    passed through. The chart is illustrative, so exact unit formatting is not
    critical for M1.
    """
    if output_type == "Percentage":
        return value * 100.0
    return value


def _extract_series(
    prometheus_data: Dict[str, Any], output_type: str
) -> List[Tuple[str, List[Tuple[float, float]]]]:
    """Turn a Prometheus matrix payload into ``[(label, [(ts, value), ...]), ...]``.

    Accepts the ``data`` object of a ``query_range`` response, i.e.
    ``{"resultType": "matrix", "result": [{"metric": {...}, "values": [[ts, "v"], ...]}]}``.
    Non-numeric points are skipped.
    """
    series: List[Tuple[str, List[Tuple[float, float]]]] = []
    for item in prometheus_data.get("result", []) or []:
        metric = item.get("metric", {}) or {}
        label = metric.get("__name__") or ", ".join(
            f"{k}={v}" for k, v in sorted(metric.items())
        ) or "series"
        points: List[Tuple[float, float]] = []
        for pair in item.get("values", []) or []:
            try:
                ts = float(pair[0])
                val = float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            points.append((ts, _format_value(val, output_type)))
        if points:
            series.append((label, points))
    return series


def render_prometheus_range_chart_png(
    prometheus_data: Dict[str, Any],
    description: str = "graph",
    output_type: str = "Plain",
) -> Optional[bytes]:
    """Render a Prometheus matrix result to PNG bytes, or ``None`` on failure.

    Returns ``None`` (logging the reason) when the optional rendering deps are
    missing, the payload has no plottable series, or rendering raises.
    """
    try:
        import pygal  # type: ignore
        from cairosvg import svg2png  # type: ignore
    except ImportError:
        logging.info(
            "Graph rendering requested but 'pygal'/'cairosvg' are not installed; "
            "skipping image rendering."
        )
        return None

    try:
        series = _extract_series(prometheus_data or {}, output_type)
        if not series:
            return None

        chart = pygal.DateTimeLine(
            title=description or "graph",
            show_dots=False,
            stroke_style={"width": 2},
            x_label_rotation=25,
        )
        import datetime as _dt

        for label, points in series:
            chart.add(
                label,
                [
                    (_dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc), val)
                    for ts, val in points
                ],
            )
        svg_bytes = chart.render()
        return svg2png(bytestring=svg_bytes)
    except Exception:
        logging.exception("Failed to render Prometheus range chart")
        return None


def attach_graph_image(
    images: Optional[List[Dict[str, str]]],
    prometheus_data: Dict[str, Any],
    description: str = "graph",
    output_type: str = "Plain",
    *,
    renderer: Callable[..., Optional[bytes]] = render_prometheus_range_chart_png,
) -> Optional[List[Dict[str, str]]]:
    """Return ``images`` with a rendered PNG appended, or unchanged on failure.

    Pure and side-effect free (returns a new list), so the toolset hook stays a
    one-liner and the behaviour is trivially testable with a fake ``renderer``.
    """
    png = renderer(prometheus_data, description, output_type)
    if not png:
        return images
    image_entry = {
        "data": base64.b64encode(png).decode("ascii"),
        "mimeType": PNG_MIME_TYPE,
    }
    return [*(images or []), image_entry]
