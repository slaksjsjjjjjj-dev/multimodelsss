from __future__ import annotations

import gzip
import json
import sys
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
                        b"GRIB" + (b"\\x00" * 12) + b"7777"
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

    def test_grib_integrity_check_rejects_truncated_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "broken.grib2"
            broken.write_bytes(b"GRIB" + (b"\x00" * 16))
            self.assertFalse(collect.is_valid_grib2(broken))


if __name__ == "__main__":
    unittest.main()
