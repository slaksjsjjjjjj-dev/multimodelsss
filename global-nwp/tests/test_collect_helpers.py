from __future__ import annotations

import gzip
import json
import sys
import threading
import types
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect


class CollectHelpersTest(unittest.TestCase):
    def test_noaa_ai_forecast_steps_match_six_hour_products(self) -> None:
        run = datetime(2026, 11, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(collect.leads_for("gfs_ai", run), list(range(0, 385, 6)))
        self.assertEqual(collect.leads_for("gefs_ai", run), list(range(0, 385, 6)))
        self.assertEqual(collect.leads_for("hgefs", run), list(range(0, 241, 6)))

    def test_gfs_operational_schedule_is_preserved(self) -> None:
        run = datetime(2026, 11, 9, 0, tzinfo=timezone.utc)
        leads = collect.leads_for("gfs", run)
        self.assertEqual(leads[:4], [0, 1, 2, 3])
        self.assertIn(120, leads)
        self.assertIn(123, leads)
        self.assertEqual(leads[-1], 384)

    def test_grib_integrity_check_accepts_plain_and_gzip(self) -> None:
        payload = b"GRIB" + (b"\x00" * 12) + b"7777"
        with tempfile.TemporaryDirectory() as temp:
            plain = Path(temp) / "sample.grib2"
            plain.write_bytes(payload)
            self.assertTrue(collect.is_valid_grib2(plain))

            compressed = Path(temp) / "sample.grib2.gz"
            with gzip.open(compressed, "wb") as stream:
                stream.write(payload)
            self.assertTrue(collect.is_valid_grib2(compressed))

    def test_large_forecast_frame_is_written_as_valid_json_parts(self) -> None:
        original_regions = collect.REGIONS
        original_limit = collect.MAX_FRAME_PART_RAW_BYTES
        try:
            collect.REGIONS = [{
                "id": "test_region",
                "label": "Test Region",
                "bounds": [0, 0, 1, 1],
            }]
            collect.MAX_FRAME_PART_RAW_BYTES = 50
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                spool = root / "spool"
                output = root / "output"
                fields_dir = spool / "test_region" / "f000"
                fields_dir.mkdir(parents=True)
                (fields_dir / "fields.ndjson").write_text(
                    '{"field":"first","values":[1,2,3]}\n'
                    '{"field":"second","values":[4,5,6]}\n',
                    encoding="utf-8",
                )
                stats = {
                    "_inventory": {"test_region": {"variables": []}},
                    "_requested_hours": [0],
                    "test_region": {
                        0: {
                            "valid_time": "2026-11-09T00:00:00Z",
                            "run_utc": "2026-11-09T00:00:00Z",
                            "field_count": 2,
                        }
                    },
                }
                result = collect.finalize_spool(
                    spool,
                    output,
                    "test_model",
                    datetime(2026, 11, 9, 0, tzinfo=timezone.utc),
                    {"label": "Test Model", "provider": "Test", "resolution": "test"},
                    stats,
                )
                forecast_dir = output / "test_model" / "test_region" / "forecast"
                first = forecast_dir / "F000.json.gz"
                second = forecast_dir / "F000-part002.json.gz"
                self.assertTrue(first.is_file())
                self.assertTrue(second.is_file())
                with gzip.open(first, "rt", encoding="utf-8") as stream:
                    first_doc = json.load(stream)
                with gzip.open(second, "rt", encoding="utf-8") as stream:
                    second_doc = json.load(stream)
                self.assertEqual(first_doc["metadata"]["part_index"], 1)
                self.assertEqual(second_doc["metadata"]["part_index"], 2)
                self.assertEqual(len(first_doc["fields"]), 1)
                self.assertEqual(len(second_doc["fields"]), 1)
                manifest = json.loads(
                    (forecast_dir.parent / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["validities"][0]["part_count"], 2)
                self.assertEqual(
                    manifest["validities"][0]["files"],
                    ["forecast/F000.json.gz", "forecast/F000-part002.json.gz"],
                )
                self.assertEqual(result[0]["forecast_hours"], 1)
        finally:
            collect.REGIONS = original_regions
            collect.MAX_FRAME_PART_RAW_BYTES = original_limit

    def test_ecmwf_collection_checkpoints_each_processed_forecast_hour(self) -> None:
        original_regions = collect.REGIONS
        try:
            collect.REGIONS = [{
                "id": "test_region",
                "label": "Test Region",
                "bounds": [0, 0, 1, 1],
            }]

            class FakeClient:
                def __init__(self, **kwargs) -> None:
                    pass

                def retrieve(self, **kwargs) -> None:
                    Path(kwargs["target"]).write_bytes(
                        b"GRIB" + (b"\x00" * 12) + b"7777"
                    )

            fake_ecmwf = types.ModuleType("ecmwf")
            fake_ecmwf.__path__ = []
            fake_opendata = types.ModuleType("ecmwf.opendata")
            fake_opendata.Client = FakeClient

            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                raw_dir = root / "_raw" / "ecmwf_ifs"
                output = root / "ecmwf_ifs" / "test_region" / "forecast"
                checkpointed = []

                def process_file(path: Path) -> None:
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "F000.json.gz").write_bytes(b"frame")

                with (
                    mock.patch.object(
                        collect,
                        "_ecmwf_tasks",
                        return_value=[("oper", "fc", [0], "atmosphere")],
                    ),
                    mock.patch.dict(
                        sys.modules,
                        {
                            "ecmwf": fake_ecmwf,
                            "ecmwf.opendata": fake_opendata,
                        },
                    ),
                ):
                    _, available, errors = collect.collect_ecmwf(
                        "ecmwf_ifs",
                        datetime(2026, 11, 9, 0, tzinfo=timezone.utc),
                        raw_dir,
                        [0],
                        process_file,
                        checkpointed.append,
                    )

                self.assertEqual(available, [0])
                self.assertEqual(errors, [])
                self.assertEqual(checkpointed, [0])
                progress = json.loads(
                    (root / "ecmwf_ifs" / "collection-progress.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(progress["status"], "download-finished")
                self.assertEqual(progress["available_forecast_hours"], [0])
        finally:
            collect.REGIONS = original_regions

    def test_dwd_parameter_groups_keep_level_types_separate(self) -> None:
        pressure = types.SimpleNamespace(code=100, alias="pressure")
        model_levels = types.SimpleNamespace(code=105, alias="model")
        level_types = {
            "T_2M": (),
            "PMSL": (),
            "T": (pressure, model_levels),
            "U": (pressure,),
            "V": (pressure,),
        }

        class FakeModel:
            def parameters(self):
                return list(level_types)

            def parameter(self, name):
                return types.SimpleNamespace(level_types=level_types[name])

        groups, errors = collect.group_dwd_parameters(
            FakeModel(), max_parameters_per_group=2
        )
        normalized = {
            (tuple(parameters), suffix)
            for parameters, _level_type, suffix, _chunk in groups
        }
        self.assertIn((("T_2M", "PMSL"), "single"), normalized)
        self.assertIn((("T", "U"), "pressure"), normalized)
        self.assertIn((("V",), "pressure"), normalized)
        self.assertIn((("T",), "model"), normalized)
        self.assertEqual(sum(len(parameters) for parameters, *_ in groups), 6)
        self.assertEqual(errors, [])

    def test_gfs_downloads_both_products_concurrently_per_lead(self) -> None:
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        run = datetime(2026, 11, 9, 0, tzinfo=timezone.utc)

        def fake_download(product, _run, lead, raw_dir):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                barrier.wait(timeout=5)
                target = raw_dir / product / f"f{lead:03d}.grib2"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"GRIB" + (b"\\x00" * 12) + b"7777")
                return target
            finally:
                with lock:
                    state["active"] -= 1

        with tempfile.TemporaryDirectory() as temp:
            raw_dir = Path(temp) / "_raw" / "gfs"
            with mock.patch.object(
                collect, "download_gfs_regional", side_effect=fake_download
            ):
                downloaded, available, errors = collect.collect_herbie(
                    "gfs", run, raw_dir, [0]
                )
            self.assertEqual(available, [0])
            self.assertEqual(errors, [])
            self.assertEqual(len(downloaded), 2)
            self.assertEqual(state["peak"], 2)

    def test_eccc_tries_hpfx_before_the_primary_datamart(self) -> None:
        run = datetime(2026, 11, 9, 0, tzinfo=timezone.utc)
        urls = collect.eccc_base_urls("model_gdps/15km", run, "00", 0)
        self.assertTrue(urls[0].startswith("http://hpfx.collab.science.gc.ca/"))
        self.assertTrue(urls[1].startswith("https://dd.weather.gc.ca/"))

    def test_cmc_downloads_files_concurrently_but_keeps_one_lead(self) -> None:
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        run = datetime(2026, 11, 9, 0, tzinfo=timezone.utc)
        urls = [f"https://example.test/field-{index}.grib2" for index in range(4)]

        def fake_ensure(_url, destination, timeout):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                barrier.wait(timeout=5)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"GRIB" + (b"\\x00" * 12) + b"7777")
                return True
            finally:
                with lock:
                    state["active"] -= 1

        with tempfile.TemporaryDirectory() as temp:
            raw_dir = Path(temp) / "_raw" / "cmc_gdps"
            with (
                mock.patch.object(collect, "eccc_base_urls", return_value=["https://example.test/"]),
                mock.patch.object(collect, "list_grib_links", return_value=urls),
                mock.patch.object(collect, "ensure_request", side_effect=fake_ensure),
            ):
                downloaded, available, errors = collect.collect_eccc(
                    "cmc_gdps", run, raw_dir, [0]
                )
            self.assertEqual(available, [0])
            self.assertEqual(errors, [])
            self.assertEqual(len(downloaded), 4)
            self.assertEqual(state["peak"], 4)

    def test_dwd_run_hour_uses_reference_time(self) -> None:
        run_type = type(
            "DwdRunLike",
            (),
            {"reference_time": datetime(2026, 11, 9, 12, tzinfo=timezone.utc)},
        )
        self.assertEqual(collect.dwd_run_hour(run_type()), 12)

    def test_checkpoint_frees_ndjson_and_keeps_prior_validities(self) -> None:
        original_regions = collect.REGIONS
        try:
            collect.REGIONS = [{
                "id": "test_region",
                "label": "Test Region",
                "bounds": [0, 0, 1, 1],
            }]
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                spool = root / "spool"
                output = root / "output"
                run = datetime(2026, 11, 9, 0, tzinfo=timezone.utc)
                spec = {"label": "Test Model", "provider": "Test", "resolution": "test"}
                stats = {
                    "_inventory": {"test_region": {"variables": []}},
                    "_requested_hours": [0, 1],
                    "test_region": {
                        0: {
                            "valid_time": "2026-11-09T00:00:00Z",
                            "run_utc": "2026-11-09T00:00:00Z",
                            "field_count": 1,
                        },
                        1: {
                            "valid_time": "2026-11-09T01:00:00Z",
                            "run_utc": "2026-11-09T00:00:00Z",
                            "field_count": 1,
                        },
                    },
                }

                def write_field(lead: int) -> None:
                    folder = spool / "test_region" / f"f{lead:03d}"
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / "fields.ndjson").write_text(
                        json.dumps({
                            "short_name": "t",
                            "name": "temperature",
                            "units": "K",
                            "level_type": "surface",
                            "level": 0,
                            "grid_type": "regular_ll",
                            "grid_id": "test-grid",
                            "values": [lead + 1],
                        }) + "\\n",
                        encoding="utf-8",
                    )

                write_field(0)
                collect.finalize_spool(
                    spool, output, "test_model", run, spec, stats, only_leads={0}
                )
                self.assertFalse(
                    (spool / "test_region" / "f000" / "fields.ndjson").exists()
                )

                write_field(1)
                collect.finalize_spool(
                    spool, output, "test_model", run, spec, stats, only_leads={1}
                )
                self.assertFalse(
                    (spool / "test_region" / "f001" / "fields.ndjson").exists()
                )

                # Final model-level pass has no raw spool left; it must retain
                # both forecast hours from the checkpoint manifest.
                collect.finalize_spool(
                    spool, output, "test_model", run, spec, stats
                )
                manifest = json.loads(
                    (output / "test_model" / "test_region" / "manifest.json")
                    .read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["forecast_hours"], [0, 1])
                self.assertTrue(
                    (output / "test_model" / "test_region" / "forecast" / "F000.json.gz").is_file()
                )
                self.assertTrue(
                    (output / "test_model" / "test_region" / "forecast" / "F001.json.gz").is_file()
                )
        finally:
            collect.REGIONS = original_regions

    def test_grib_integrity_check_rejects_truncated_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "broken.grib2"
            broken.write_bytes(b"GRIB" + (b"\x00" * 16))
            self.assertFalse(collect.is_valid_grib2(broken))


if __name__ == "__main__":
    unittest.main()
