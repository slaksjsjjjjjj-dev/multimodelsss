#!/usr/bin/env python3
"""Generate a point-specific native WRF Skew-T PNG/JSON for one forecast hour.

The extraction and SPC-style rendering routines are reused from the established
Sideral WRF native Skew-T core (wrf/metbr_skewt_south.py). This worker is
model-configurable; the model identity and resolution are supplied by the
workflow, while all meteorological fields are read from the selected WRFOUT.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import shutil
from pathlib import Path

from netCDF4 import Dataset

# This script lives in .github/workflows in the workflow repository. The
# meteorological core is checked out separately by the reusable workflow.
# Keeping the root configurable avoids coupling the worker to either checkout.
ROOT = Path(
    os.environ.get("SIDERAL_SOURCE_DIR")
    or Path(__file__).resolve().parents[2]
).resolve()
NATIVE_PATH = ROOT / "wrf" / "metbr_skewt_south.py"


def load_native():
    spec = importlib.util.spec_from_file_location("sideral_native_wrf_skewt_core", NATIVE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Não foi possível carregar o núcleo nativo WRF: {NATIVE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_time(value: str) -> dt.datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Horário ausente nos metadados do modelo.")
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def choose_click_gridpoint(native, dataset, latitude: float, longitude: float,
                           max_distance_km: float) -> dict:
    """Find the nearest WRF mass-grid cell and reject points outside usable coverage."""
    import numpy as np

    lat_grid, lon_grid = native.grid_latlon(dataset)
    lat_r = np.radians(lat_grid)
    lon_r = np.radians(lon_grid)
    lat0 = math.radians(float(latitude))
    lon0 = math.radians(float(longitude))
    dlat = lat_r - lat0
    dlon = (lon_r - lon0 + np.pi) % (2.0 * np.pi) - np.pi
    a = (
        np.sin(dlat / 2.0) ** 2
        + math.cos(lat0) * np.cos(lat_r) * np.sin(dlon / 2.0) ** 2
    )
    distance = 6371.0 * 2.0 * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    distance[~np.isfinite(distance)] = np.inf

    j, i = np.unravel_index(int(np.argmin(distance)), distance.shape)
    distance_km = float(distance[j, i])
    ny, nx = lat_grid.shape

    if not math.isfinite(distance_km) or distance_km > max_distance_km:
        raise RuntimeError(
            f"O ponto está fora da grade utilizável: célula mais próxima a "
            f"{distance_km:.2f} km (limite {max_distance_km:.1f} km)."
        )
    if not (1 <= j < ny - 1 and 1 <= i < nx - 1):
        raise RuntimeError("O ponto selecionado está próximo demais da borda da grade WRF.")

    return {
        "i": int(i),
        "j": int(j),
        "distance_km": distance_km,
        "grid_latitude": float(lat_grid[j, i]),
        "grid_longitude": float(lon_grid[j, i]),
        "grid_shape": {"ny": int(ny), "nx": int(nx)},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wrfout", required=True)
    parser.add_argument("--forecast-hour", required=True, type=int)
    parser.add_argument("--latitude", required=True, type=float)
    parser.add_argument("--longitude", required=True, type=float)
    parser.add_argument("--run-time", required=True)
    parser.add_argument("--valid-time", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--model-label", required=True)
    parser.add_argument("--model-key", required=True)
    parser.add_argument("--resolution-km", required=True, type=float)
    parser.add_argument("--max-distance-km", type=float, default=8.0)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    if not (0 <= args.forecast_hour <= 48):
        raise ValueError("Validade fora do intervalo operacional F000–F048.")
    if not (-90 <= args.latitude <= 90 and -180 <= args.longitude <= 180):
        raise ValueError("Latitude/longitude inválidas.")
    if args.resolution_km <= 0 or args.max_distance_km <= 0:
        raise ValueError("Resolução ou distância máxima inválida.")

    run_time = parse_time(args.run_time)
    valid_time = parse_time(args.valid_time)
    actual_hour = (valid_time - run_time).total_seconds() / 3600.0
    if abs(actual_hour - args.forecast_hour) > 0.02:
        raise RuntimeError(
            f"Validade inconsistente: metadados indicam F{actual_hour:.2f}, "
            f"mas foi solicitado F{args.forecast_hour:03d}."
        )

    wrfout = Path(args.wrfout).resolve()
    if not wrfout.is_file() or wrfout.stat().st_size < 5 * 1024 * 1024:
        raise FileNotFoundError(f"WRFOUT ausente ou incompleto: {wrfout}")

    native = load_native()
    point_name = f"Ponto selecionado ({args.latitude:.4f}, {args.longitude:.4f})"
    location = {
        "name": point_name,
        "state": "",
        "latitude": args.latitude,
        "longitude": args.longitude,
    }

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with Dataset(str(wrfout), "r") as dataset:
        time_index = None
        for index in range(native.read_time_count(dataset)):
            stamp = native.time_at(dataset, index)
            if stamp is not None and abs((stamp - valid_time).total_seconds()) <= 60:
                time_index = index
                break
        if time_index is None:
            raise RuntimeError(
                f"O WRFOUT {wrfout.name} não contém {valid_time:%Y-%m-%d %H:%MZ}."
            )

        point = choose_click_gridpoint(
            native, dataset, args.latitude, args.longitude, args.max_distance_km
        )
        sounding, rows, terrain, thermal_advection = native.make_profile(
            dataset, time_index, location, point, valid_time
        )

        renderer = native.load_renderer()
        temp_dir = output_dir / ".render-tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        render_meta = {
            "location": point_name,
            "state": "",
            "model": args.model_label,
            "station": f"{point_name} / {args.model_label}",
            "latitude": args.latitude,
            "longitude": args.longitude,
            "elevation": terrain,
            "fh": args.forecast_hour,
            "valid": valid_time,
            "run": run_time,
            "source": f"WRF nativo {args.resolution_km:g} km · sondagem pontual",
            "thermal_advection": thermal_advection,
        }
        rendered_path = renderer.render_native_spc(sounding, temp_dir, render_meta)
        candidate = Path(rendered_path) if rendered_path else temp_dir / "full.png"
        if not candidate.is_file() or candidate.stat().st_size < 10000:
            candidate = temp_dir / "full.png"
        if not candidate.is_file() or candidate.stat().st_size < 10000:
            raise RuntimeError(
                f"O renderizador não produziu PNG válido para F{args.forecast_hour:03d}."
            )

        png_path = output_dir / f"f{args.forecast_hour:03d}.png"
        png_path.write_bytes(candidate.read_bytes())
        shutil.rmtree(temp_dir, ignore_errors=True)

    profile = native.profile_json(
        location, point, run_time, valid_time, args.forecast_hour, rows, terrain
    )
    profile.update({
        "schema": "sideral-wrf-skewt-clickpoint-profile-v1",
        "request_id": args.request_id,
        "model": args.model_label,
        "model_key": args.model_key,
        "source": "WRF-ARW native WRFOUT",
        "native_grid_spacing_km": args.resolution_km,
        "latitude": args.latitude,
        "longitude": args.longitude,
        "grid_latitude": point["grid_latitude"],
        "grid_longitude": point["grid_longitude"],
        "grid_distance_km": round(float(point["distance_km"]), 3),
        "run_utc": run_time.strftime("%Y-%m-%d %H:%MZ"),
        "forecast_hour": args.forecast_hour,
        "valid_utc": valid_time.strftime("%Y-%m-%d %H:%MZ"),
        "terrain_elevation_m": float(terrain),
        "renderer": "SHARPpy · Sideral native SPC",
    })
    json_path = output_dir / f"f{args.forecast_hour:03d}.json"
    json_path.write_text(
        json.dumps(profile, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(
        f"[Sideral Click-to-Point] {args.model_key} F{args.forecast_hour:03d} concluído · "
        f"{len(rows)} níveis · célula a {point['distance_km']:.2f} km · "
        f"{png_path.stat().st_size} bytes",
        flush=True,
    )


if __name__ == "__main__":
    main()
