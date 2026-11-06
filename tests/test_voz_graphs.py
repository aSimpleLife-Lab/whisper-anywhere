from __future__ import annotations

import json

import pytest

from core import voz_graphs as vg

REVISION = "12802bf4e2b5e97e1289b723bc6283b6f1a1ace5"


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """Stand-in model files, and a graph rewrite that only records that it ran."""
    for name, size in (("encoder.onnx", 10), ("encoder.onnx.data", 300), ("decoder.onnx", 20)):
        (tmp_path / name).write_bytes(bytes(size))
    builds: list[int] = []

    def build(folder) -> None:
        builds.append(1)
        for name in vg.CACHE:
            (folder / name).write_bytes(b"cpu graph")

    monkeypatch.setattr(vg, "_build", build)
    return tmp_path, builds


def test_the_cache_is_built_once_and_reused(bundle) -> None:
    folder, builds = bundle

    assert vg.ensure_cpu_graphs(folder, REVISION) == (folder / vg.ENCODER_CPU, folder / vg.DECODER_CPU)
    vg.ensure_cpu_graphs(folder, REVISION)

    assert len(builds) == 1


def test_a_cache_from_other_model_files_is_rebuilt(bundle) -> None:
    # The cached encoder still reads its weights from encoder.onnx.data: after
    # an update laid that file out anew, every dictation came back empty.
    folder, builds = bundle
    vg.ensure_cpu_graphs(folder, REVISION)
    (folder / "encoder.onnx").write_bytes(bytes(11))

    vg.ensure_cpu_graphs(folder, REVISION)

    assert len(builds) == 2


def test_a_cache_from_another_revision_is_rebuilt(bundle) -> None:
    folder, builds = bundle
    vg.ensure_cpu_graphs(folder, REVISION)

    vg.ensure_cpu_graphs(folder, "a newer revision")

    assert len(builds) == 2


def test_a_cut_short_cache_is_rebuilt(bundle) -> None:
    # A crash mid-write left half of encoder.cpu.data, which failed to load forever.
    folder, builds = bundle
    vg.ensure_cpu_graphs(folder, REVISION)
    (folder / vg.ENCODER_CPU_DATA).write_bytes(b"cpu")

    vg.ensure_cpu_graphs(folder, REVISION)

    assert len(builds) == 2


def test_a_cache_without_a_record_is_rebuilt(bundle) -> None:
    # Graphs cached by an older version carry no record of their source files.
    folder, builds = bundle
    for name in vg.CACHE:
        (folder / name).write_bytes(b"cpu graph")

    vg.ensure_cpu_graphs(folder, REVISION)

    assert len(builds) == 1
    record = json.loads((folder / vg.CACHE_INFO).read_text(encoding="utf-8"))
    assert record["revision"] == REVISION
    assert record["sources"] == {"encoder.onnx": 10, "encoder.onnx.data": 300, "decoder.onnx": 20}


def test_an_interrupted_build_is_never_trusted(bundle, monkeypatch) -> None:
    folder, builds = bundle
    vg.ensure_cpu_graphs(folder, REVISION)

    def crash(folder) -> None:
        (folder / vg.ENCODER_CPU_DATA).write_bytes(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(vg, "_build", crash)
    with pytest.raises(OSError):
        vg.ensure_cpu_graphs(folder, REVISION, rebuild=True)

    assert not (folder / vg.CACHE_INFO).exists()


def test_a_rebuild_can_be_asked_for(bundle) -> None:
    folder, builds = bundle
    vg.ensure_cpu_graphs(folder, REVISION)

    vg.ensure_cpu_graphs(folder, REVISION, rebuild=True)

    assert len(builds) == 2
