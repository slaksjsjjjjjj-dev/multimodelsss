from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

BRANCH = "multimodels-data"


def raw_url(repository: str, relative: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{repository}/{BRANCH}/"
        + quote(relative.replace("\\", "/"), safe="/")
    )


def main() -> int:
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    run_id = os.environ["GITHUB_RUN_ID"]
    source = Path(os.environ.get("MULTIMODEL_OUTPUT", "sideral-multimodels-05/output")).resolve()
    if not source.is_dir():
        raise SystemExit(f"Saída multimodelo não encontrada: {source}")

    with tempfile.TemporaryDirectory(prefix="sideral-multimodel-release-") as temp:
        stage = Path(temp) / "publication"
        data_root = stage / "data"
        data_root.mkdir(parents=True)
        shutil.copy2(source / "README.md", stage / "README.md") if (source / "README.md").is_file() else None

        copied = []
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            # O consenso NetCDF é intermediário do processamento; a publicação
            # para o SIDERAL contém os frames JSON/PNG e seus manifests.
            if path.suffix.lower() not in {".json", ".png"}:
                continue
            relative = path.relative_to(source)
            destination = data_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied.append((path, destination, relative.as_posix()))

        manifests = sorted(data_root.rglob("manifest.json"))
        if not manifests:
            raise SystemExit("Nenhum manifest.json encontrado na saída multimodelo.")
        if not any(path.suffix.lower() == ".json" for path in data_root.rglob("*.json")):
            raise SystemExit("Nenhum frame JSON foi encontrado para publicação.")

        regions = []
        total_json = 0
        total_png = 0
        total_bytes = 0

        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_relative = manifest_path.relative_to(stage).as_posix()
            manifest_url = raw_url(repository, manifest_relative)
            run_dir = manifest_path.parent
            run_relative = run_dir.relative_to(data_root).as_posix()

            url_map = {}
            lead_map: dict[int, dict[str, str]] = {}
            for frame in sorted(run_dir.rglob("*")):
                if not frame.is_file() or frame.name == "manifest.json":
                    continue
                ext = frame.suffix.lower()
                if ext not in {".json", ".png"}:
                    continue
                relative = frame.relative_to(stage).as_posix()
                url = raw_url(repository, relative)
                url_map[frame.relative_to(run_dir).as_posix()] = url
                match = re.search(r"(?i)f(\d+)", frame.stem)
                if match:
                    lead = int(match.group(1))
                    lead_map.setdefault(lead, {})["json" if ext == ".json" else "png"] = url
                if ext == ".json":
                    total_json += 1
                else:
                    total_png += 1
                total_bytes += frame.stat().st_size

            for product in manifest.get("forecastProducts", []):
                try:
                    lead = int(product.get("forecastHour"))
                except (TypeError, ValueError):
                    continue
                urls = lead_map.get(lead, {})
                if "json" in urls:
                    product["jsonUrl"] = urls["json"]
                if "png" in urls:
                    product["pngUrl"] = urls["png"]

            manifest["publication"] = {
                "branch": BRANCH,
                "directory": f"data/{run_relative}/",
                "manifest_url": manifest_url,
                "download_urls": url_map,
                "permanent": True,
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            relative_parts = manifest_path.relative_to(data_root).parts
            region_id = relative_parts[1] if len(relative_parts) > 2 and relative_parts[0] == "regions" else run_dir.parent.name
            region_info = manifest.get("region", {})
            if not isinstance(region_info, dict):
                region_info = {}
            forecast_hours = manifest.get(
                "productForecastHours",
                manifest.get("forecastHours", sorted(lead_map)),
            )
            regions.append({
                "region_id": region_info.get("id", region_info.get("key", region_id)),
                "label": region_info.get("label", region_info.get("name", region_id)),
                "run_directory": f"data/{run_relative}/",
                "manifest_url": manifest_url,
                "download_base_url": raw_url(repository, f"data/{run_relative}/"),
                "forecast_hours": forecast_hours,
                "forecast_hour_count": len(forecast_hours or []),
                "field_count": manifest.get("fieldCount", manifest.get("field_count")),
                "resolution": manifest.get("resolution"),
                "files": [
                    {"path": name, "url": url}
                    for name, url in sorted(url_map.items())
                ],
            })

        # Build the catalog after manifest URLs have been added.
        catalog = {
            "schema": "sideral-multimodels-025-release-v1",
            "project": "SIDERAL MULTIMODELS 0.25",
            "run_id": run_id,
            "data_branch": BRANCH,
            "data_root": "data/",
            "data_format": "Frames JSON e PNG individuais, organizados por região e rodada; sem ZIP.",
            "regions": regions,
            "region_count": len(regions),
            "json_frame_count": total_json,
            "png_frame_count": total_png,
            "total_frame_bytes": total_bytes,
        }
        report = {
            "schema": "sideral-multimodels-025-run-report-v1",
            "run_id": run_id,
            "region_count": len(regions),
            "json_frame_count": total_json,
            "png_frame_count": total_png,
            "published_file_count": len(copied),
            "branch": BRANCH,
            "status": "ready-to-publish",
        }

        (stage / ".nojekyll").touch()
        (stage / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (stage / "run-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        gh([
            "python", "global-nwp/publish_tree.py",
            "--source", str(stage),
            "--branch", BRANCH,
            "--repository", repository,
            "--token", token,
            "--commit-message", f"Publish SIDERAL MULTIMODELS {run_id}",
            "--batch-mb", "8",
            "--batch-files", "200",
        ])

        # A publicação permanente fica toda na branch de dados:
        # frames JSON/PNG, manifests, catálogo e relatório.
        print(f"Dados permanentes: https://github.com/{repository}/tree/{BRANCH}/data")
        print(f"Catálogo: https://github.com/{repository}/blob/{BRANCH}/catalog.json")
        print(f"Relatório: https://github.com/{repository}/blob/{BRANCH}/run-report.json")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
