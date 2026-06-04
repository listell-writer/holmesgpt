"""Unit tests for moving Prometheus graph rendering into Holmes (item 4).

These exercise the integration logic with a fake renderer, so they do NOT
require the optional native deps (pygal/cairosvg). The real pixel rendering is
verified separately (it needs cairosvg's native cairo libs + a running Holmes).
"""

import base64

from holmes.plugins.toolsets.prometheus.graph_rendering import (
    PNG_MIME_TYPE,
    _extract_series,
    attach_graph_image,
    render_prometheus_range_chart_png,
)

MATRIX = {
    "resultType": "matrix",
    "result": [
        {
            "metric": {"__name__": "up", "instance": "a"},
            "values": [[1000, "1"], [1060, "0"]],
        }
    ],
}


def test_attach_appends_png_image_when_renderer_succeeds():
    images = attach_graph_image(
        None,
        prometheus_data=MATRIX,
        description="cpu",
        renderer=lambda *a, **k: b"PNGBYTES",
    )
    assert images == [
        {"data": base64.b64encode(b"PNGBYTES").decode("ascii"), "mimeType": PNG_MIME_TYPE}
    ]


def test_attach_preserves_existing_images():
    existing = [{"data": "x", "mimeType": PNG_MIME_TYPE}]
    images = attach_graph_image(
        existing,
        prometheus_data=MATRIX,
        description="cpu",
        renderer=lambda *a, **k: b"PNG",
    )
    assert len(images) == 2
    assert images[0] == existing[0]
    assert existing == [{"data": "x", "mimeType": PNG_MIME_TYPE}]  # input not mutated


def test_attach_is_noop_when_renderer_returns_none():
    assert attach_graph_image(None, prometheus_data=MATRIX, renderer=lambda *a, **k: None) is None
    existing = [{"data": "x", "mimeType": PNG_MIME_TYPE}]
    assert (
        attach_graph_image(existing, prometheus_data=MATRIX, renderer=lambda *a, **k: None)
        is existing
    )


def test_render_returns_none_without_native_deps(monkeypatch):
    # Simulate pygal/cairosvg not being importable; rendering must degrade to
    # None rather than raising into the toolset.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("pygal", "cairosvg"):
            raise ImportError(f"no {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert render_prometheus_range_chart_png(MATRIX, "cpu") is None


def test_extract_series_parses_matrix_and_skips_bad_points():
    data = {
        "result": [
            {"metric": {"__name__": "m1"}, "values": [[1, "2.5"], ["bad", "x"], [3, "4"]]},
            {"metric": {"job": "j", "inst": "i"}, "values": [[5, "1"]]},
        ]
    }
    series = _extract_series(data, "Plain")
    assert series[0][0] == "m1"
    assert series[0][1] == [(1.0, 2.5), (3.0, 4.0)]  # bad point skipped
    # No __name__ -> label built from sorted metric labels.
    assert series[1][0] == "inst=i, job=j"


def test_extract_series_percentage_scales_values():
    data = {"result": [{"metric": {"__name__": "ratio"}, "values": [[1, "0.5"]]}]}
    series = _extract_series(data, "Percentage")
    assert series[0][1] == [(1.0, 50.0)]


def test_extract_series_empty_for_no_result():
    assert _extract_series({}, "Plain") == []
    assert _extract_series({"result": []}, "Plain") == []
