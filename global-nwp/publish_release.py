from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

PREFIX = "global-nwp-"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="release-input")
    parser.add_argument("--output", default="release-assets")
    parser.add_argument("--date", default="")
    parser.add_argument("--cycle", default="")
    args = parser.parse_args()

    source_root = Path(args.input)
    output_root = Path(args.output)
    data_root = output_root / "data"
    output_root.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    reference = datetime.now(timezone.utc) - timedelta(hours=9)
    date_stamp = args.date or reference.strftime("%Y%m%d")
    cycle_stamp = args.cycle or f"{(reference.hour // 6) * 6:02d}"
    repository = os.getenv("GITHUB_REPOSITORY", "slaksjsjjjjjj-dev/multimodelsss")
    raw_data_base_url = f"https://raw.githubusercontent.com/{repository}/nwp-data/data/"
    source_catalog = json.loads(
        (Path(__file__).resolve().parent / "catalog.json").read_text(encoding="utf-8")
    )
    region_catalog = json.loads(
        (Path(__file__).resolve().parent / "regions.json").read_text(encoding="utf-8")
    )

    catalog = {
        "schema": "sideral-global-nwp-release-v2",
        "model_definitions": source_catalog.get("models", []),
        "region_definitions": region_catalog.get("regions", []),
        "run_date": date_stamp,
        "cycle_utc": cycle_stamp,
        "coverage": ["south_america", "south_pacific"],
        "format": (
            "JSON gzip independente por validade; validades grandes podem ser divididas "
            "em FNNN.json.gz e FNNN-partNNN.json.gz. O catálogo lista todas as partes."
        ),
        "data_branch": "nwp-data",
        "data_root": "data/",
        "repository": repository,
        "data_base_url": raw_data_base_url,
        "data_tree_url": f"https://github.com/{repository}/tree/nwp-data/data",
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

    expected_dirs = sorted(
        path for path in source_root.glob(PREFIX + "*") if path.is_dir()
    )

    for model_dir in expected_dirs:
        model_key = model_dir.name[len(PREFIX):]
        model_data_dir = data_root / model_key
        model_data_dir.mkdir(parents=True, exist_ok=True)

        status_path = model_dir / "model_status.json"
        model_status = (
            json.loads(status_path.read_text(encoding="utf-8"))
            if status_path.is_file()
            else {
                "model_key": model_key,
                "status": "missing-status",
                "message": "O coletor não enviou model_status.json."
            }
        )
        model_entry = {
            "model_key": model_key,
            "status": model_status.get("status"),
            "message": model_status.get("message"),
            "run_utc": model_status.get("run_utc"),
            "errors": model_status.get("errors", []),
            "regions": [],
        }

        # Keep model-level diagnostics beside its regional forecast folders.
        for diagnostic_name in (
            "model_status.json", "collection-summary.json", "collection-progress.json"
        ):
            source_file = model_dir / diagnostic_name
            if source_file.is_file():
                shutil.move(str(source_file), str(model_data_dir / diagnostic_name))

        for manifest_path in sorted(model_dir.glob("*/manifest.json")):
            region_dir = manifest_path.parent
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            region_id = manifest.get("region", {}).get("id") or region_dir.name
            region_data_dir = model_data_dir / region_id
            region_data_dir.mkdir(parents=True, exist_ok=True)

            forecast_dir = region_dir / "forecast"
            json_frames = sorted(
                forecast_dir.glob("F*.json.gz"),
                key=lambda item: (
                    int(re.match(r"F(\d+)", item.name).group(1))
                    if re.match(r"F(\d+)", item.name) else 10**9,
                    int(re.search(r"-part(\d+)", item.name).group(1))
                    if re.search(r"-part(\d+)", item.name) else 1,
                ),
            )
            if not json_frames:
                # Accept the previous lower-case naming convention only when
                # it still has exactly one frame for each lead.
                json_frames = sorted(forecast_dir.glob("f*.json.gz"))

            files = []
            files_by_lead: dict[int, list[str]] = {}
            for frame in json_frames:
                match = re.match(r"^[fF](\d{3})(?:-part\d{3})?\.json\.gz$", frame.name)
                if not match:
                    raise SystemExit(
                        f"Nome de frame inesperado em {model_key}/{region_id}: {frame.name}"
                    )
                lead = int(match.group(1))
                normalized_name = frame.name[0].upper() + frame.name[1:]
                destination = region_data_dir / normalized_name
                if destination.exists():
                    raise SystemExit(
                        f"Arquivo duplicado em {model_key}/{region_id}: {normalized_name}"
                    )
                shutil.move(str(frame), str(destination))
                files.append(destination.name)
                files_by_lead.setdefault(lead, []).append(destination.name)

            # The manifest lists every part for a validity. "file" and
            # "download_url" remain aliases for the first part for older clients.
            manifest = dict(manifest)
            manifest["data_directory"] = f"data/{model_key}/{region_id}/"
            manifest["data_format"] = (
                "JSON gzip independente por validade; validades grandes podem ser "
                "divididas em FNNN.json.gz e FNNN-partNNN.json.gz. Use "
                "validities[].files/download_urls para recuperar todos os campos."
            )
            manifest["files"] = files
            manifest["download_base_url"] = raw_data_base_url + f"{model_key}/{region_id}/"
            manifest["file_urls"] = {
                file_name: manifest["download_base_url"] + quote(file_name, safe="")
                for file_name in files
            }
            for validity in manifest.get("validities", []):
                lead = int(validity.get("forecast_hour", -1))
                lead_files = files_by_lead.get(lead, [])
                if lead_files:
                    validity["files"] = lead_files
                    validity["part_count"] = len(lead_files)
                    validity["file"] = lead_files[0]
                    validity["download_urls"] = [
                        manifest["file_urls"][name] for name in lead_files
                    ]
                    validity["download_url"] = validity["download_urls"][0]

            (region_data_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            if not files:
                continue

            forecast_hours = sorted({
                int(re.match(r"^F(\d{3})", file_name).group(1))
                for file_name in files
            })
            region_entry = {
                "region_id": region_id,
                "label": manifest.get("region", {}).get("label", region_id),
                "data_directory": f"data/{model_key}/{region_id}/",
                "download_base_url": manifest["download_base_url"],
                "file_urls": manifest["file_urls"],
                "manifest_url": raw_data_base_url + f"{model_key}/{region_id}/manifest.json",
                "files": files,
                "file_count": len(files),
                "forecast_hours": forecast_hours,
                "missing_forecast_hours": manifest.get("missing_forecast_hours", []),
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

    # Ensure every configured model is visible, even when no artifact arrived.
    present_models = {item["model_key"] for item in catalog["models"]}
    for definition in source_catalog.get("models", []):
        model_key = definition.get("key")
        if model_key and model_key not in present_models:
            catalog["models"].append({
                "model_key": model_key,
                "status": "missing-artifact",
                "message": "Nenhum artefato do job deste modelo foi recebido nesta execução.",
                "run_utc": None,
                "errors": [
                    "O job pode ter sido cancelado ou atingido o timeout antes do upload."
                ],
                "regions": [],
            })

    catalog["models"].sort(
        key=lambda item: next(
            (
                index for index, spec in enumerate(source_catalog.get("models", []))
                if spec.get("key") == item.get("model_key")
            ),
            10_000,
        )
    )

    # The complete publication (data files, catalog and run report) is committed
    # to the dedicated nwp-data branch in its folder hierarchy.
    (output_root / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    progress_by_model = {}
    for model in catalog["models"]:
        model_data_dir = data_root / model["model_key"]
        summary_file = model_data_dir / "collection-summary.json"
        progress_file = model_data_dir / "collection-progress.json"
        summary = (
            json.loads(summary_file.read_text(encoding="utf-8"))
            if summary_file.is_file() else {}
        )
        progress = (
            json.loads(progress_file.read_text(encoding="utf-8"))
            if progress_file.is_file() else {}
        )
        requested_hours = summary.get("requested_forecast_hours", [])
        progress_by_model[model["model_key"]] = {
            "requested_forecast_hours": requested_hours,
            "requested_forecast_hour_count": progress.get(
                "requested_forecast_hours", len(requested_hours)
            ),
            "completed_forecast_hours": progress.get(
                "completed_forecast_hours", summary.get("source_hours", [])
            ),
            "available_forecast_hours": progress.get(
                "available_forecast_hours", summary.get("source_hours", [])
            ),
            "missing_forecast_hours": summary.get(
                "missing_forecast_hours", progress.get("missing_forecast_hours", [])
            ),
            "errors": progress.get("errors", summary.get("download_errors", []))[:250],
            "status": model.get("status"),
        }

    report = {
        "schema": "sideral-global-nwp-run-report-v2",
        "run_date": date_stamp,
        "cycle_utc": cycle_stamp,
        "model_count": len(catalog["models"]),
        "models_with_regional_data": sum(
            1 for item in catalog["models"] if item["regions"]
        ),
        "blocked_models": [
            item["model_key"] for item in catalog["models"]
            if item["status"] == "blocked"
        ],
        "failed_models": [
            item["model_key"] for item in catalog["models"]
            if item["status"] not in {"published-ready", "partial", "blocked"}
        ],
        "partial_models": [
            item["model_key"] for item in catalog["models"]
            if item["status"] == "partial"
        ],
        "regional_directories": len(catalog["regional_assets"]),
        "individual_json_files": sum(
            item["file_count"] for item in catalog["regional_assets"]
        ),
        "models_without_regional_data": [
            item["model_key"] for item in catalog["models"] if not item["regions"]
        ],
        "forecast_coverage": progress_by_model,
        "has_regional_data": bool(catalog["regional_assets"]),
        "run_status": "published-with-data" if catalog["regional_assets"] else "diagnostic-only",
        "total_fields": sum(
            item["field_count"] for item in catalog["regional_assets"]
        ),
    }
    (output_root / "run-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
