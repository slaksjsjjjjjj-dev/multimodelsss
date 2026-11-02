from __future__ import annotations

import argparse
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
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
    reference = datetime.now(timezone.utc) - timedelta(hours=9)
    date_stamp = args.date or reference.strftime("%Y%m%d")
    cycle_stamp = args.cycle or f"{(reference.hour // 6) * 6:02d}"
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
            {"provider": "ECCC", "license": "MSC Open Data / Datamart End-use Licence", "text": "Source: Environment and Climate Change Canada (ECCC) — https://dd.weather.gc.ca/; use subject to the MSC Open Data licence."},
            {"provider": "UK Met Office", "text": "UKMET is only published when a valid Weather DataHub subscription and API configuration are supplied."}
        ]
    }
    # Missing artifacts are still represented in the diagnostic catalog below.
    # This keeps a failed/cancelled matrix run observable instead of losing its report.
    expected_dirs = sorted(path for path in source_root.glob(PREFIX + "*") if path.is_dir())

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
            validities_by_name = {
                Path(item.get("file", "")).name: item
                for item in manifest.get("validities", [])
            }

            # GitHub Release assets have a 2 GiB per-file ceiling. Create adaptive
            # ZIP shards at <=1.5 GB instead of one unbounded region archive.
            max_asset_bytes = 1_500_000_000
            frame_groups: list[list[Path]] = []
            current_group: list[Path] = []
            current_size = 0
            for frame in json_frames:
                frame_size = frame.stat().st_size
                if frame_size > max_asset_bytes:
                    raise SystemExit(
                        f"{model_key}/{region_id}/{frame.name} excede 1.5 GB sozinho; "
                        "é necessário dividir o frame por variável/nível."
                    )
                if current_group and current_size + frame_size > max_asset_bytes:
                    frame_groups.append(current_group)
                    current_group = []
                    current_size = 0
                current_group.append(frame)
                current_size += frame_size
            if current_group:
                frame_groups.append(current_group)

            parts = []
            for part_number, group in enumerate(frame_groups, start=1):
                asset_name = (
                    f"{slug(model_key)}--{slug(region_id)}"
                    f"--part{part_number:03d}.zip"
                )
                asset_path = output_root / asset_name
                with zipfile.ZipFile(
                    asset_path, "w", compression=zipfile.ZIP_STORED
                ) as archive:
                    archive.write(manifest_path, "manifest.json")
                    for frame in group:
                        archive.write(frame, f"forecast/{frame.name}")
                part_validities = [
                    validities_by_name.get(frame.name, {
                        "forecast_hour": int(frame.stem[1:4]),
                        "file": f"forecast/{frame.name}",
                    })
                    for frame in group
                ]
                parts.append({
                    "asset": asset_name,
                    "forecast_hours": [
                        item.get("forecast_hour") for item in part_validities
                    ],
                    "frame_count": len(group),
                    "uncompressed_bytes": sum(frame.stat().st_size for frame in group),
                    "validities": part_validities,
                })

            region_entry = {
                "region_id": region_id,
                "label": manifest.get("region", {}).get("label", region_id),
                "assets": [part["asset"] for part in parts],
                "asset_parts": parts,
                "forecast_hours": manifest.get("forecast_hours", []),
                "frame_count": len(json_frames),
                "field_count": manifest.get("field_count", 0),
                "variable_level_count": manifest.get("variable_level_count", 0),
                "variables": manifest.get("variables", []),
                "validities": manifest.get("validities", []),
                "run_utc": manifest.get("run_utc"),
                "resolution": manifest.get("resolution"),
                "collection_status": manifest.get("collection_status", "unknown"),
            }
            model_entry["regions"].append(region_entry)
            catalog["regional_assets"].append({
                "model_key": model_key,
                **region_entry,
            })
        catalog["models"].append(model_entry)

    # Keep the full configured model inventory visible even when a matrix job
    # was cancelled before it could upload its artifact.
    present_models = {item["model_key"] for item in catalog["models"]}
    for definition in source_catalog.get("models", []):
        model_key = definition.get("key")
        if model_key and model_key not in present_models:
            catalog["models"].append({
                "model_key": model_key,
                "status": "missing-artifact",
                "message": "Nenhum artefato do job deste modelo foi recebido nesta execução.",
                "run_utc": None,
                "errors": ["O job pode ter sido cancelado ou atingido o timeout antes do upload."],
                "regions": [],
            })
    catalog["models"].sort(
        key=lambda item: next(
            (i for i, spec in enumerate(source_catalog.get("models", []))
             if spec.get("key") == item.get("model_key")),
            10_000,
        )
    )

    # Always publish the diagnostic catalog and run report. If no provider
    # delivered usable regional data, the release contains these two JSON files
    # only and clearly reports the missing/failed models.
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
        "failed_models": [
            item["model_key"] for item in catalog["models"]
            if item["status"] not in {"published-ready", "partial", "blocked"}
        ],
        "partial_models": [item["model_key"] for item in catalog["models"] if item["status"] == "partial"],
        "regional_assets": len(catalog["regional_assets"]),
        "models_without_regional_data": [
            item["model_key"] for item in catalog["models"] if not item["regions"]
        ],
        "has_regional_data": bool(catalog["regional_assets"]),
        "run_status": "published-with-data" if catalog["regional_assets"] else "diagnostic-only",
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
