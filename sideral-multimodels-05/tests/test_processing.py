from pathlib import Path

from sideral_multimodels.processing import generate_output, parse_hours


def test_parse_hours_defaults():
    assert parse_hours("") == [0, 6, 12, 18, 24]
    assert parse_hours("0,12,24") == [0, 12, 24]


def test_generate_output_creates_manifest(tmp_path):
    run_dir = generate_output(tmp_path / "out", hours=[0, 6, 12], run_name="demo")
    manifest = run_dir / "manifest.json"
    assert manifest.exists()
    assert (run_dir / "forecast" / "f000.json").exists()
    assert (run_dir / "forecast" / "f006.png").exists()
