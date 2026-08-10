"""Unit tests for the carousel/quotes gating behind the dashboard's Entregables
toggles. Monkeypatches the two generator functions so no Remotion/agent runs.
"""
import contenido_bionico.pipeline as pl


def test_build_extras_parallel_gating(monkeypatch):
    calls = []
    monkeypatch.setattr(pl, "generate_and_publish_carousel", lambda vid: calls.append("carousel"))
    monkeypatch.setattr(pl, "generate_and_publish_quotes", lambda vid: calls.append("quotes"))

    calls.clear()
    pl._build_extras_parallel(1, carousel=True, quotes=True)
    assert sorted(calls) == ["carousel", "quotes"]

    calls.clear()
    pl._build_extras_parallel(1, carousel=True, quotes=False)
    assert calls == ["carousel"]

    calls.clear()
    pl._build_extras_parallel(1, carousel=False, quotes=True)
    assert calls == ["quotes"]

    calls.clear()
    pl._build_extras_parallel(1, carousel=False, quotes=False)
    assert calls == []


def test_build_short_extras_forwards_toggles(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(pl, "run_output_dir", lambda vid: tmp_path)
    monkeypatch.setattr(
        pl, "_build_extras_parallel",
        lambda vid, *, carousel, quotes: seen.update(carousel=carousel, quotes=quotes),
    )
    monkeypatch.setattr(pl, "_publish_run_caption", lambda vid: None)

    pl.build_short_extras(7, carousel=False, quotes=True)
    assert seen == {"carousel": False, "quotes": True}
