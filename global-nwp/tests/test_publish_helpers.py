from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish_release


class PublishHelpersTest(unittest.TestCase):
    def test_empty_model_status_uses_fallback_instead_of_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status = Path(temp) / "model_status.json"
            status.write_text("", encoding="utf-8")
            fallback = {"model_key": "cmc_gdps", "status": "failed"}
            result = publish_release.read_json_safe(
                status, fallback, "cmc_gdps/model_status.json"
            )
            self.assertEqual(result, fallback)

    def test_truncated_progress_uses_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            progress = Path(temp) / "collection-progress.json"
            progress.write_text('{"completed_count":', encoding="utf-8")
            fallback = {"status": "unavailable"}
            result = publish_release.read_json_safe(
                progress, fallback, "test/collection-progress.json"
            )
            self.assertEqual(result, fallback)

    def test_valid_json_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status = Path(temp) / "model_status.json"
            expected = {"model_key": "gfs", "status": "partial"}
            status.write_text(json.dumps(expected), encoding="utf-8")
            result = publish_release.read_json_safe(status, {}, "gfs/model_status.json")
            self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
