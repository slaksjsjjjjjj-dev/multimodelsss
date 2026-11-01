from __future__ import annotations

import argparse
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "global-nwp-"


def slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip())
    return value.strip("-_.").lower() or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="release-input")
    parser.add_argument("--output", default="release-assets")
    parser.add_argument("--date", default="")
    parser.add_argument("--cycle", default="")
    args = parser.parse_args()

    source_root = Path(args.input)
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    date_stamp = args.date or datetime.now(timezone.utc).strftime("%Y%m%d")
    cycle_stamp = args.cycle or "auto"
    source_catalog = json.loads((Path(__file__).resolve().parent / "catalog.json").read_text(encoding="utf-8"))
    region_catalog = json.loads((Path(__file__).resolve().parent / "regions.json").read_text(encoding="utf-8"))
    catalog = {
        "schema": "sideral-global-nwp-release-v1",
        "model_definitions": source_catalog.get("models", []),
        "region_definitions": region_catalog.get("regions", []),
        "run_date": date_stamp,
        "cycle_utc": cycle_stamp,
        "coverage": ["south_america", "south_pacific", "brazil_*", "argentina_*"],
        "format": "ZIP por modelo/região com manifest.json e forecast/Fxxx.json.gz",
        "models": [],
        "regional_assets": [],
        "source_attributions": [
            {"provider": "ECMWF", "license": "CC-BY-4.0", "text": "Contains ECMWF data. © ECMWF."},
            {"provider": "DWD", "text": "Source: Deutscher Wetterdienst Open Data."},
            {"provider": "NOAA", "text": "Source: NOAA/NCEP operational model data."},
            {"provider": "ECCC", "text": "Source: Environment and Climate Change Canada Open Data."},
            {"provider": "UK Met Office", "text": "UKMET is only published when a valid Weather DataHub subscription and API configuration are supplied."}
        ]
    }
    expected_dirs = sorted(path for path in source_root.glob(PREFIX + "*") if path.is_dir())
    if not expected_dirs:
        raise SystemExit(f"Nenhum artefato {PREFIX}* foi baixado.")

    for model_dir in expected_dirs:
        model_key = model_dir.name[len(PREFIX):]
        status_path = model_dir / "model_status.json"
        model_status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {
            "model_key": model_key, "status": "missing-status", "message": "O coletor não enviou model_status.json."
        }
        model_entry = {
            "model_key": model_key,
            "status": model_status.get("status"),
            "message": model_status.get("message"),
            "run_utc": model_status.get("run_utc"),
            "errors": model_status.get("errors", []),
            "regions": [],
        }
        for manifest_path in sorted(model_dir.glob("*/manifest.json")):
            region_dir = manifest_path.parent
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            region_id = manifest.get("region", {}).get("id") or region_dir.name
            json_frames = sorted((region_dir / "forecast").glob("f*.json.gz"))
            if not json_frames:
                continue
            asset_name = f"{slug(model_key)}--{slug(region_id)}.zip"
            asset_path = output_root / asset_name
            with zipfile.ZipFile(asset_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                archive.write(manifest_path, "manifest.json")
                for frame in json_frames:
                    archive.write(frame, f"forecast/{frame.name}")
            validities = manifest.get("validities", [])
            region_entry = {
                "region_id": region_id,
                "label": manifest.get("region", {}).get("label", region_id),
                "asset": asset_name,
                "forecast_hours": manifest.get("forecast_hours", []),
                "frame_count": len(json_frames),
                "field_count": manifest.get("field_count", 0),
                "variable_level_count": manifest.get("variable_level_count", 0),
                "variables": manifest.get("variables", []),
                "validities": validities,
                "run_utc": manifest.get("run_utc"),
                "resolution": manifest.get("resolution"),
            }
            model_entry["regions"].append(region_entry)
            catalog["regional_assets"].append({
                "model_key": model_key,
                **region_entry,
            })
        catalog["models"].append(model_entry)

    (output_root / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = {
        "schema": "sideral-global-nwp-run-report-v1",
        "run_date": date_stamp,
        "cycle_utc": cycle_stamp,
        "model_count": len(catalog["models"]),
        "models_with_regional_data": sum(1 for item in catalog["models"] if item["regions"]),
        "blocked_models": [item["model_key"] for item in catalog["models"] if item["status"] == "blocked"],
        "failed_models": [item["model_key"] for item in catalog["models"] if item["status"] == "failed"],
        "partial_models": [item["model_key"] for item in catalog["models"] if item["status"] == "partial"],
        "regional_assets": len(catalog["regional_assets"]),
        "total_frames": sum(item["frame_count"] for item in catalog["regional_assets"]),
        "total_fields": sum(item["field_count"] for item in catalog["regional_assets"]),
    }
    (output_root / "run-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
