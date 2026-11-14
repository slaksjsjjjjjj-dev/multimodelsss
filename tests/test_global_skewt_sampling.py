import importlib.util
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "global_nwp_skewt_clickpoint.py"
spec = importlib.util.spec_from_file_location("global_skewt_runner_test", SCRIPT)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def fake_sharppy(captured):
    profile_module = types.ModuleType("sharppy.sharptab.profile")

    def create_profile(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(**kwargs)

    profile_module.create_profile = create_profile
    sharptab_module = types.ModuleType("sharppy.sharptab")
    sharptab_module.profile = profile_module
    sharppy_module = types.ModuleType("sharppy")
    sharppy_module.sharptab = sharptab_module
    return {
        "sharppy": sharppy_module,
        "sharppy.sharptab": sharptab_module,
        "sharppy.sharptab.profile": profile_module,
    }


class GlobalSkewtSamplingTests(unittest.TestCase):
    def test_nearest_grid_point(self):
        grid = {
            "latitudes": [-23.0, -23.0, -22.75, -22.75],
            "longitudes": [-43.25, -43.0, -43.25, -43.0],
        }
        index, lat, lon, distance = runner.nearest_grid_sample(grid, -22.94, -43.22)
        self.assertEqual(index, 0)
        self.assertAlmostEqual(lat, -23.0)
        self.assertAlmostEqual(lon, -43.25)
        self.assertLess(distance, 20)

    def test_specific_humidity_dewpoint(self):
        td = runner.dewpoint_from_specific_humidity(0.010, 850)
        self.assertTrue(math.isfinite(td))
        self.assertGreater(td, -40)
        self.assertLess(td, 35)

    def test_relative_humidity_fraction_and_percent(self):
        percent = runner.dewpoint_from_relative_humidity(20.0, 50.0, "%")
        fraction = runner.dewpoint_from_relative_humidity(20.0, 0.5, "")
        self.assertAlmostEqual(percent, fraction, places=6)

    def test_model_registry_uses_only_requested_three_models(self):
        self.assertEqual(set(runner.MODEL_CONFIG), {"gfs_ai", "icon_ai", "ecmwf_ifs"})
        self.assertEqual(runner.MODEL_CONFIG["gfs_ai"]["directory"], "gfs_ai")
        self.assertEqual(runner.MODEL_CONFIG["icon_ai"]["directory"], "icon_ai")

    def test_ecmwf_ifs_native_isobaric_profile(self):
        captured = {}
        levels = [1000, 925, 850, 700, 500, 300, 200, 100]
        samples = {
            ("sp", "surface", "0"): 100800.0,
            ("z", "isobaricinhpa", "0"): 100.0 * runner.G,
            ("2t", "heightaboveground", "2"): 293.15,
            ("2d", "heightaboveground", "2"): 289.15,
            ("10u", "heightaboveground", "10"): 3.0,
            ("10v", "heightaboveground", "10"): 4.0,
        }
        variables = [
            {"short_name": "sp", "level_type": "surface", "level": "0", "units": "Pa"},
            {"short_name": "z", "level_type": "isobaricInhPa", "level": "0", "units": "m**2 s**-2"},
            {"short_name": "2t", "level_type": "heightAboveGround", "level": "2", "units": "K"},
            {"short_name": "2d", "level_type": "heightAboveGround", "level": "2", "units": "K"},
            {"short_name": "10u", "level_type": "heightAboveGround", "level": "10", "units": "m s**-1"},
            {"short_name": "10v", "level_type": "heightAboveGround", "level": "10", "units": "m s**-1"},
        ]
        for pressure in levels:
            height = 150.0 + (1000.0 - pressure) * 15.0
            for short_name, value, units in (
                ("t", 285.0 - (1000 - pressure) * 0.04, "K"),
                ("q", 0.008, "kg kg**-1"),
                ("u", 8.0, "m s**-1"),
                ("v", 3.0, "m s**-1"),
                ("gh", height, "gpm"),
                ("w", -0.05, "Pa s**-1"),
            ):
                samples[(short_name, "isobaricinhpa", str(pressure))] = value
                variables.append({
                    "short_name": short_name,
                    "level_type": "isobaricInhPa",
                    "level": str(pressure),
                    "units": units,
                })
        manifest = {
            "model_key": "ecmwf_ifs",
            "run_utc": "2026-11-14T00:00:00Z",
            "variables": variables,
        }
        with patch.dict(sys.modules, fake_sharppy(captured)):
            sounding, rows, terrain, meta = runner.extract_profile(
                samples, manifest, -23.0, -43.0, "ECMWF IFS"
            )
        self.assertEqual(len(rows), 9)
        self.assertAlmostEqual(terrain, 100.0, places=5)
        self.assertEqual(captured["profile"], "convective")
        self.assertEqual(len(captured["pres"]), 9)
        self.assertAlmostEqual(meta["surface_pressure_hpa"], 1008.0)
        self.assertFalse(meta["terrain_is_estimated"])
        self.assertIsNotNone(sounding)

    def test_gfs_ai_uses_its_own_isobaric_folder_and_near_surface_proxy(self):
        captured = {}
        levels = [1000, 925, 850, 700, 500, 300, 200, 100]
        samples = {
            ("2t", "heightaboveground", "2"): 294.15,
            ("10u", "heightaboveground", "10"): 4.0,
            ("10v", "heightaboveground", "10"): 2.0,
            ("prmsl", "meansea", "0"): 101000.0,
        }
        variables = [
            {"short_name": "2t", "level_type": "heightAboveGround", "level": "2", "units": "K"},
            {"short_name": "10u", "level_type": "heightAboveGround", "level": "10", "units": "m s**-1"},
            {"short_name": "10v", "level_type": "heightAboveGround", "level": "10", "units": "m s**-1"},
            {"short_name": "prmsl", "level_type": "meanSea", "level": "0", "units": "Pa"},
        ]
        for pressure in levels:
            height = 120.0 + (1000.0 - pressure) * 15.0
            for short_name, value, units in (
                ("t", 288.0 - (1000 - pressure) * 0.04, "K"),
                ("q", 0.009, "kg kg**-1"),
                ("u", 7.0, "m s**-1"),
                ("v", 2.0, "m s**-1"),
                ("gh", height, "gpm"),
                ("w", -0.03, "Pa s**-1"),
            ):
                samples[(short_name, "isobaricinhpa", str(pressure))] = value
                variables.append({"short_name": short_name, "level_type": "isobaricInhPa", "level": str(pressure), "units": units})
        manifest = {"model_key": "gfs_ai", "run_utc": "2026-11-14T00:00:00Z", "variables": variables}
        with patch.dict(sys.modules, fake_sharppy(captured)):
            sounding, rows, terrain, meta = runner.extract_profile(
                samples, manifest, -23.0, -43.0, "GFS-AI"
            )
        self.assertEqual(len(rows), 8)
        self.assertAlmostEqual(terrain, 120.0)
        self.assertIn("Proxy AIGFS", meta["terrain_label"])
        self.assertTrue(meta["terrain_is_estimated"])
        self.assertGreater(rows[0]["dewpoint_c"], -30)
        self.assertIsNotNone(sounding)

    def test_icon_ai_general_vertical_profile_has_relative_height_reference(self):
        captured = {}
        samples = {}
        variables = []
        pressures = [100000, 95000, 90000, 85000, 80000, 75000, 70000, 65000, 60000, 50000, 40000, 30000, 20000]
        for index, p_pa in enumerate(pressures, start=1):
            level = str(index)
            values = (
                ("pres", p_pa, "Pa"),
                ("t", 294.0 - index * 5.0, "K"),
                ("q", 0.012 - index * 0.0007, "kg kg**-1"),
                ("u", 3.0 + index, "m s**-1"),
                ("v", 2.0 + index * 0.5, "m s**-1"),
            )
            for short_name, value, units in values:
                samples[(short_name, "generalvertical", level)] = float(value)
                variables.append({"short_name": short_name, "level_type": "generalVertical", "level": level, "units": units})
        manifest = {"model_key": "icon_ai", "run_utc": "2026-11-14T00:00:00Z", "variables": variables}
        with patch.dict(sys.modules, fake_sharppy(captured)):
            sounding, rows, terrain, meta = runner.extract_profile(
                samples, manifest, -23.0, -43.0, "ICON-AI"
            )
        self.assertEqual(len(rows), 13)
        self.assertEqual(terrain, 0.0)
        self.assertEqual(rows[0]["height_m"], 0.0)
        self.assertGreater(rows[-1]["height_m"], rows[0]["height_m"])
        self.assertIsNone(meta.get("terrain_elevation_m"))
        self.assertIn("não é altitude MSL/AGL", meta["vertical_reference"])
        self.assertIsNotNone(sounding)


if __name__ == "__main__":
    unittest.main()
