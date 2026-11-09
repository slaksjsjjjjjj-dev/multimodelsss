from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
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

    def test_grib_integrity_check_rejects_truncated_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "broken.grib2"
            broken.write_bytes(b"GRIB" + (b"\x00" * 16))
            self.assertFalse(collect.is_valid_grib2(broken))


if __name__ == "__main__":
    unittest.main()
