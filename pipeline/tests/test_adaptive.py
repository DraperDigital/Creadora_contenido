"""Tests for the machine-adaptive render-worker sizing (`cpu_workers`)."""
import sys
import types

import contenido_bionico.shared.runtime.adaptive as adaptive


def _fake_psutil(monkeypatch, available_bytes):
    fake = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(available=available_bytes)
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)


def test_cpu_workers_env_override(monkeypatch):
    monkeypatch.setenv("BIONICO_RENDER_WORKERS", "3")
    assert adaptive.cpu_workers() == 3          # explicit override wins outright


def test_cpu_workers_targets_core_fraction(monkeypatch):
    monkeypatch.delenv("BIONICO_RENDER_WORKERS", raising=False)
    monkeypatch.setattr(adaptive.os, "cpu_count", lambda: 12)
    _fake_psutil(monkeypatch, 64e9)             # lots of RAM -> core target applies
    assert adaptive.cpu_workers(0.70) == 8      # round(12 * 0.70)


def test_cpu_workers_ram_capped(monkeypatch):
    monkeypatch.delenv("BIONICO_RENDER_WORKERS", raising=False)
    monkeypatch.setattr(adaptive.os, "cpu_count", lambda: 12)
    _fake_psutil(monkeypatch, 3e9)              # only ~3 GB free -> 3/1.5 = 2, below 8
    assert adaptive.cpu_workers(0.70, ram_per_worker_gb=1.5) == 2


def test_cpu_workers_minimum_one(monkeypatch):
    monkeypatch.delenv("BIONICO_RENDER_WORKERS", raising=False)
    monkeypatch.setattr(adaptive.os, "cpu_count", lambda: 1)
    _fake_psutil(monkeypatch, 64e9)
    assert adaptive.cpu_workers(0.70) >= 1      # never zero
