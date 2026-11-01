from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
REGION_CATALOG = json.loads((ROOT / "regions.json").read_text(encoding="utf-8"))
REGIONS = REGION_CATALOG["regions"]
CATALOG_BY_KEY = {item["key"]: item for item in CATALOG["models"]}
USER_AGENT = "Sideral-Global-NWP/1.0 (+https://github.com/slaksjsjjjjjj-dev/multimodelsss)"


def utc_run(date_text: str | None, cycle_text: str | None) -> datetime:
    if date_text:
        day = datetime.strptime(date_text, "%Y%m%d").date()
    else:
        day = datetime.now(timezone.utc).date()
    if cycle_text:
        cycle = int(cycle_text)
    else:
        cycle = (datetime.now(timezone.utc).hour // 6) * 6
    if cycle not in (0, 6, 12, 18):
        raise ValueError("Ciclo UTC deve ser 00, 06, 12 ou 18.")
    return datetime(day.year, day.month, day.day, cycle, tzinfo=timezone.utc)


def leads_for(model_key: str, run: datetime) -> list[int]:
    if model_key == "ecmwf_ifs":
        if run.hour in (0, 12):
            return list(range(0, 145, 3)) + list(range(150, 361, 6))
        return list(range(0, 145, 3))
    if model_key == "ecmwf_eps":
        if run.hour in (0, 12):
            return list(range(0, 145, 3)) + list(range(150, 361, 6))
        return list(range(0, 145, 3))
    if model_key in {"ecmwf_aifs", "ecmwf_aifs_ens"}:
        return list(range(0, 361, 6))
    if model_key == "gfs":
        return list(range(0, 121)) + list(range(123, 385, 3))
    if model_key == "gfs_ai":
        return list(range(0, 385, 3))
    if model_key == "gefs_ai":
        return list(range(0, 385, 3))
    if model_key == "hgefs":
        return list(range(0, 241, 3))
    if model_key == "cmc_gdps":
        return list(range(0, 85)) + list(range(87, 241, 3))
    if model_key == "cmc_geps":
        # GEPS raw: 000–192 h em passos de 3 h; 198–384 h em passos de 6 h.
        # Em 00Z de segunda/quinta, o produto estendido avança até F936.
        leads = list(range(0, 193, 3)) + list(range(198, 385, 6))
        if run.hour == 0 and run.weekday() in (0, 3):
            leads.extend(range(390, 937, 6))
        return leads
    if model_key == "ukmet":
        return list(range(0, 169, 3))
    return []


def ensure_request(url: str, destination: Path, timeout: tuple[int, int] = (20, 180)) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        return True
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True)
    if response.status_code == 404:
        return False
    response.raise_for_status()
    temp = destination.with_suffix(destination.suffix + ".part")
    with temp.open("wb") as stream:
        for chunk in response.iter_content(1024 * 1024):
            if chunk:
                stream.write(chunk)
    if not temp.is_file() or temp.stat().st_size == 0:
        temp.unlink(missing_ok=True)
        return False
    temp.replace(destination)
    return True


def make_herbie(model: str, product: str, run: datetime, lead: int, save_dir: Path, **kwargs):
    from herbie import Herbie

    args = {
        "date": run,
        "model": model,
        "product": product,
        "fxx": lead,
        "save_dir": str(save_dir),
        **kwargs,
    }
    return Herbie(**args)


def download_herbie_file(model: str, product: str, run: datetime, lead: int,
                         raw_dir: Path, **kwargs) -> list[Path]:
    before = set(raw_dir.rglob("*")) if raw_dir.exists() else set()
    h = make_herbie(model, product, run, lead, raw_dir, **kwargs)
    result = h.download()
    files: list[Path] = []
    if result:
        try:
            path = Path(result)
            if path.is_file():
                files.append(path)
        except (TypeError, OSError):
            pass
    if not files:
        for path in raw_dir.rglob("*"):
            if path.is_file() and path not in before and path.suffix.lower() in {".grib2", ".grb2", ".grb"}:
                files.append(path)
    return sorted(set(files))


def collect_ecmwf(model_key: str, run: datetime, raw_dir: Path,
                   requested: list[int],
                   process_file: Callable[[Path], None] | None = None
                   ) -> tuple[list[Path], list[int], list[str]]:
    """Retrieve every available ECMWF parameter/level for each native step.

    EPS and AIFS-ENS publish ensemble summary products (mean, spread and
    probabilities) here; perturbation members are not duplicated into every
    regional JSON. The catalogue documents that aggregation choice explicitly.
    """
    from ecmwf.opendata import Client

    if model_key == "ecmwf_ifs":
        upstream_model = "ifs"
        products = (("oper", "fc", "all"), ("wave", "fc", "all"))
    elif model_key == "ecmwf_eps":
        upstream_model = "ifs"
        products = (
            ("enfo", "cf", "all"), ("enfo", "em", "all"),
            ("enfo", "es", "all"), ("enfo", "ep", "12h"),
            ("waef", "cf", "all"), ("waef", "pf", "all"),
            ("waef", "ep", "12h"),
        )
    elif model_key == "ecmwf_aifs":
        upstream_model = "aifs-single"
        products = (("oper", "fc", "all"),)
    elif model_key == "ecmwf_aifs_ens":
        upstream_model = "aifs-ens"
        products = (
            ("enfo", "cf", "all"), ("enfo", "pf", "all"),
            ("enfo", "ep", "12h"),
        )
    else:
        raise ValueError(f"Modelo ECMWF desconhecido: {model_key}")

    client = Client(
        source="ecmwf",
        model=upstream_model,
        resol="0p25",
        infer_stream_keyword=False,
    )
    raw_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    found_hours: set[int] = set()
    errors: list[str] = []
    run_date = run.strftime("%Y%m%d")
    for lead in requested:
        lead_downloaded = False
        for stream, product_type, cadence in products:
            if cadence == "12h" and (lead % 12 != 0 or run.hour not in (0, 12)):
                continue
            target = raw_dir / f"f{lead:03d}" / f"{stream}-{product_type}.grib2"
            try:
                if not target.is_file() or target.stat().st_size == 0:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    client.retrieve(
                        date=run_date,
                        time=run.hour,
                        stream=stream,
                        type=product_type,
                        step=lead,
                        target=str(target),
                    )
                if target.is_file() and target.stat().st_size > 0:
                    if process_file:
                        process_file(target)
                    else:
                        downloaded.append(target)
                    lead_downloaded = True
                else:
                    errors.append(
                        f"F{lead:03d}/{product_type}: a API ECMWF não criou arquivo."
                    )
            except Exception as exc:
                errors.append(
                    f"F{lead:03d}/{product_type}: {type(exc).__name__}: {exc}"
                )
                target.unlink(missing_ok=True)
            time.sleep(0.15)
        if lead_downloaded:
            found_hours.add(lead)
        else:
            errors.append(f"F{lead:03d}: nenhum produto ECMWF disponível.")
    return sorted(set(downloaded)), sorted(found_hours), errors


def collect_herbie(model_key: str, run: datetime, raw_dir: Path, requested: list[int],
                   process_file: Callable[[Path], None] | None = None
                   ) -> tuple[list[Path], list[int], list[str]]:
    spec = {
        "gfs": [("gfs", "pgrb2.0p25", {}), ("gfs", "pgrb2b.0p25", {})],
        "gfs_ai": [("aigfs", "sfc", {}), ("aigfs", "pres", {})],
        "gefs_ai": [
            ("aigefs", "sfc", {"member": "avg"}),
            ("aigefs", "sfc", {"member": "spr"}),
            ("aigefs", "pres", {"member": "avg"}),
            ("aigefs", "pres", {"member": "spr"}),
        ],
        "hgefs": [
            ("hgefs", "sfc", {"member": "avg"}),
            ("hgefs", "sfc", {"member": "spr"}),
            ("hgefs", "pres", {"member": "avg"}),
            ("hgefs", "pres", {"member": "spr"}),
        ],
    }
    products = spec[model_key]
    downloaded: list[Path] = []
    found_hours: set[int] = set()
    errors: list[str] = []
    for lead in requested:
        lead_files: list[Path] = []
        lead_downloaded = False
        for source_model, product, kwargs in products:
            try:
                options = dict(kwargs)
                if source_model == "ifs":
                    options["resolution"] = "0p25"
                dest = raw_dir / f"f{lead:03d}" / source_model / product / "_".join(
                    f"{key}-{value}" for key, value in sorted(options.items())
                )
                found = download_herbie_file(
                    source_model, product, run, lead, dest, **options
                )
                if found:
                    lead_downloaded = True
                if process_file:
                    for path in found:
                        process_file(path)
                else:
                    lead_files.extend(found)
            except Exception as exc:
                errors.append(f"F{lead:03d} {source_model}/{product}: {type(exc).__name__}: {exc}")
            time.sleep(0.25)
        if lead_downloaded or lead_files:
            found_hours.add(lead)
            downloaded.extend(lead_files)
        else:
            message = f"F{lead:03d}: nenhuma das fontes/product streams devolveu arquivo."
            print(f"[{model_key}] {message}")
            errors.append(message)
    return sorted(set(downloaded)), sorted(found_hours), errors


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href", "")
        if href and href.lower().endswith((".grib2", ".grb2", ".grb")):
            self.links.append(href)


def list_grib_links(url: str) -> list[str]:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(20, 90))
    if response.status_code == 404:
        return []
    response.raise_for_status()
    parser = LinkParser()
    parser.feed(response.text)
    return sorted(set(urljoin(url, link) for link in parser.links))


def collect_eccc(model_key: str, run: datetime, raw_dir: Path, requested: list[int],
                 process_file: Callable[[Path], None] | None = None
                 ) -> tuple[list[Path], list[int], list[str]]:
    if model_key == "cmc_gdps":
        product_path = "model_gdps/15km"
    else:
        product_path = "ensemble/geps/grib2/raw"
    # GDPS/GEPS são publicados em 00Z e 12Z. Para agendamentos 06Z/18Z,
    # usar o ciclo de 12 horas imediatamente anterior.
    source_run = run.replace(hour=0 if run.hour < 12 else 12)
    date_text = source_run.strftime("%Y%m%d")
    cycle = f"{source_run.hour:02d}"
    downloaded: list[Path] = []
    found_hours: set[int] = set()
    errors: list[str] = []
    for lead in requested:
        if model_key == "cmc_gdps":
            base = f"https://dd.weather.gc.ca/today/{product_path}/{cycle}/{lead:03d}/"
        else:
            base = f"https://dd.weather.gc.ca/{date_text}/WXO-DD/{product_path}/{cycle}/{lead:03d}/"
        try:
            links = list_grib_links(base)
            if not links:
                errors.append(f"F{lead:03d}: nenhum GRIB2 publicado no Datamart para este ciclo.")
                continue
            lead_dir = raw_dir / f"f{lead:03d}"
            lead_dir.mkdir(parents=True, exist_ok=True)
            lead_downloaded = False
            for index, url in enumerate(links):
                name = url.rsplit("/", 1)[-1]
                target = lead_dir / name
                try:
                    if ensure_request(url, target, timeout=(20, 240)):
                        lead_downloaded = True
                        if process_file:
                            process_file(target)
                        else:
                            downloaded.append(target)
                except Exception as exc:
                    errors.append(f"F{lead:03d} {name}: {type(exc).__name__}: {exc}")
                if index % 8 == 7:
                    time.sleep(1)
            if lead_downloaded:
                found_hours.add(lead)
        except Exception as exc:
            errors.append(f"F{lead:03d} {type(exc).__name__}: {exc}")
        time.sleep(0.25)
    return sorted(set(downloaded)), sorted(found_hours), errors


def collect_dwd(model_key: str, raw_dir: Path,
                process_file: Callable[[Path], None] | None = None
                ) -> tuple[list[Path], list[int], list[str]]:
    upstream = {
        "icon_dwd": "icon",
        "icon_ai": "aicon",
    }[model_key]
    try:
        from dwdopen import DWD
        raw_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        errors: list[str] = []
        weight_dir = Path(os.environ.get("DWD_WEIGHT_DIR", str(ROOT / "dwd-weights")))
        target_grid = next(weight_dir.rglob("target_grid_world_0125.txt"), None)
        weights_file = next(weight_dir.rglob("weights_icogl2world_0125.nc"), None)
        if target_grid is None or weights_file is None:
            raise RuntimeError("Pesos oficiais de regrade ICON Global do DWD não encontrados.")
        regridded_dir = raw_dir / "latlon-0p125"
        regridded_dir.mkdir(parents=True, exist_ok=True)
        with DWD(max_connections=12, progress=False) as dwd:
            available_models = dwd.nwp.models()
            if upstream not in available_models:
                raise RuntimeError(
                    f"DWD não publicou o modelo {upstream!r}. "
                    f"Modelos disponíveis: {', '.join(available_models)}"
                )
            model = dwd.nwp.model(upstream)
            parameters = model.parameters()
            if not parameters:
                raise RuntimeError(f"DWD não informou parâmetros para {upstream}.")
            available_runs = model.runs()
            if not available_runs:
                raise RuntimeError(f"DWD não publicou uma rodada para {upstream}.")
            source_run = available_runs[-1]
            print(f"[{model_key}] rodada DWD fixada: {source_run}")
            for parameter in parameters:
                try:
                    info = model.parameter(parameter)
                    level_types = list(info.level_types) or [None]
                    for level_type in level_types:
                        level_suffix = (
                            "single" if level_type is None
                            else str(getattr(level_type, "alias", None) or getattr(level_type, "code", level_type))
                        )
                        safe_parameter = re.sub(r"[^A-Za-z0-9_.-]+", "-", parameter)
                        safe_level = re.sub(r"[^A-Za-z0-9_.-]+", "-", level_suffix)
                        target = raw_dir / safe_parameter / safe_level / f"{safe_parameter}-{safe_level}-all-members-all-steps.grib2"
                        try:
                            options = {"parameters": parameter, "steps": "all"}
                            if level_type is not None:
                                options["level_type"] = level_type
                                options["levels"] = "all"
                            query = model.select(**options)
                            # DWD separa parâmetros, níveis e membros na árvore
                            # de arquivos. Resolver cada coordenada vertical
                            # separadamente evita escolhas ambíguas e inclui todos
                            # os membros e passos disponíveis.
                            plan = query.resolve(run=source_run, require="partial")
                            if not plan.assets:
                                errors.append(f"{parameter}/{level_suffix}: nenhum asset disponível.")
                                continue
                            if getattr(plan, "missing", None):
                                errors.append(
                                    f"{parameter}/{level_suffix}: {len(plan.missing)} combinações de nível/membro/validade ausentes no run."
                                )
                            plan.download(str(target))
                            if target.is_file() and target.stat().st_size > 0:
                                destination = regridded_dir / f"{safe_parameter}-{safe_level}.grib2"
                                command = [
                                    "cdo", "-s", "-f", "grb2",
                                    f"remap,{target_grid},{weights_file}",
                                    str(target), str(destination),
                                ]
                                subprocess.run(command, check=True, timeout=900)
                                if destination.is_file() and destination.stat().st_size > 0:
                                    if process_file:
                                        process_file(destination)
                                        target.unlink(missing_ok=True)
                                    else:
                                        downloaded.append(destination)
                                else:
                                    errors.append(f"{parameter}/{level_suffix}: CDO não gerou a grade 0.125°.")
                                    destination.unlink(missing_ok=True)
                                if process_file:
                                    target.unlink(missing_ok=True)
                        except Exception as exc:
                            errors.append(
                                f"{parameter}/{level_suffix}: {type(exc).__name__}: {exc}"
                            )
                except Exception as exc:
                    errors.append(f"{parameter}: {type(exc).__name__}: {exc}")
                print(f"[{model_key}] parâmetro {parameter}: concluído.")
        if process_file:
            return [], [], errors
        if not downloaded:
            raise RuntimeError("DWD não forneceu GRIB2 com os parâmetros/níveis solicitados.")
        return sorted(set(downloaded)), [], errors
    except Exception as exc:
        return [], [], [f"DWD {upstream}: {type(exc).__name__}: {exc}"]

def message_value(gid, key: str, default=None):
    from eccodes import codes_get
    try:
        return codes_get(gid, key)
    except Exception:
        return default


def message_datetime(gid, prefix: str) -> datetime | None:
    try:
        date_value = int(message_value(gid, prefix + "Date"))
        time_value = int(message_value(gid, prefix + "Time", 0))
        return datetime.strptime(f"{date_value:08d}{time_value:04d}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def lead_from_message(gid) -> tuple[int | None, str | None, str | None]:
    reference = message_datetime(gid, "data")
    valid = message_datetime(gid, "validity")
    if reference is not None and valid is not None:
        lead = int(round((valid - reference).total_seconds() / 3600))
        return lead, reference.isoformat(), valid.isoformat()
    try:
        lead = int(message_value(gid, "endStep", message_value(gid, "forecastTime")))
        return lead, reference.isoformat() if reference else None, valid.isoformat() if valid else None
    except Exception:
        return None, reference.isoformat() if reference else None, valid.isoformat() if valid else None


def safe_json_line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def regionalize(raw_files: list[Path], spool_root: Path, model_key: str,
                requested_hours: list[int], max_hour: int) -> dict[str, dict[int, dict]]:
    from eccodes import (
        codes_grib_new_from_file, codes_get_array, codes_get_values, codes_release
    )
    spool_root.mkdir(parents=True, exist_ok=True)
    stats: dict[str, dict[int, dict]] = defaultdict(dict)
    field_inventory: dict[str, set[tuple]] = defaultdict(set)
    requested = set(requested_hours)
    errors: list[str] = []

    for source in raw_files:
        if source.suffix.lower() not in {".grib2", ".grb2", ".grb"}:
            continue
        try:
            stream = source.open("rb")
        except OSError as exc:
            errors.append(f"{source.name}: {exc}")
            continue
        with stream:
            while True:
                gid = codes_grib_new_from_file(stream)
                if gid is None:
                    break
                try:
                    lead, run_utc, valid_utc = lead_from_message(gid)
                    if lead is None or lead < 0 or lead > max_hour:
                        continue
                    if requested and lead not in requested and model_key not in {"icon_dwd", "icon_ai"}:
                        continue
                    lats = np.asarray(codes_get_array(gid, "latitudes"), dtype=np.float64)
                    lons = np.asarray(codes_get_array(gid, "longitudes"), dtype=np.float64)
                    vals = np.asarray(codes_get_values(gid), dtype=np.float64)
                    if lats.size == 0 or lats.size != lons.size or lats.size != vals.size:
                        continue
                    lons = ((lons + 180.0) % 360.0) - 180.0
                    meta = {
                        "short_name": str(message_value(gid, "shortName", "unknown")),
                        "name": str(message_value(gid, "name", "")),
                        "units": str(message_value(gid, "units", "")),
                        "parameter_id": message_value(gid, "paramId"),
                        "level_type": str(message_value(gid, "typeOfLevel", "")),
                        "level": message_value(gid, "level"),
                        "step_type": str(message_value(gid, "stepType", "")),
                        "step_range": str(message_value(gid, "stepRange", "")),
                        "member": message_value(gid, "number", message_value(gid, "perturbationNumber")),
                        "valid_time": valid_utc,
                    }
                    for region in REGIONS:
                        west, south, east, north = [float(x) for x in region["bounds"]]
                        indices = np.flatnonzero(
                            (lats >= south) & (lats <= north) &
                            (lons >= west) & (lons <= east)
                        )
                        if indices.size == 0:
                            continue
                        region_id = region["id"]
                        field_dir = spool_root / region_id / f"f{lead:03d}"
                        grids_dir = field_dir / "grids"
                        grids_dir.mkdir(parents=True, exist_ok=True)
                        selected_lats = np.round(lats[indices], 4)
                        selected_lons = np.round(lons[indices], 4)
                        signature = hashlib.sha1(
                            selected_lats.tobytes() + selected_lons.tobytes()
                        ).hexdigest()[:16]
                        grid_path = grids_dir / f"{signature}.json"
                        if not grid_path.exists():
                            grid_path.write_text(
                                safe_json_line({
                                    "latitudes": selected_lats.tolist(),
                                    "longitudes": selected_lons.tolist(),
                                }),
                                encoding="utf-8",
                            )
                        selected_values = vals[indices]
                        value_list = [
                            round(float(value), 3) if np.isfinite(value) else None
                            for value in selected_values
                        ]
                        field = {
                            **meta,
                            "grid_id": signature,
                            "values": value_list,
                            "source_file": source.name,
                        }
                        with (field_dir / "fields.ndjson").open("a", encoding="utf-8") as output:
                            output.write(safe_json_line(field) + "\n")
                        entry = stats[region_id].setdefault(lead, {
                            "valid_time": valid_utc,
                            "run_utc": run_utc,
                            "field_count": 0,
                            "source_files": set(),
                        })
                        entry["field_count"] += 1
                        entry["source_files"].add(source.name)
                        field_inventory[region_id].add((
                            meta["short_name"], meta["name"], meta["units"],
                            meta["level_type"], str(meta["level"]),
                        ))
                except Exception as exc:
                    errors.append(f"{source.name}: {type(exc).__name__}: {exc}")
                finally:
                    codes_release(gid)

    for region_id, leads in stats.items():
        for lead, entry in leads.items():
            entry["source_files"] = sorted(entry["source_files"])
    stats["_inventory"] = {
        region_id: {
            "variables": [
                {"short_name": row[0], "name": row[1], "units": row[2],
                 "level_type": row[3], "level": row[4]}
                for row in sorted(rows)
            ]
        }
        for region_id, rows in field_inventory.items()
    }
    stats["_errors"] = {"items": errors[:500]}
    return stats


def finalize_spool(spool_root: Path, output_root: Path, model_key: str,
                   run: datetime, catalog_spec: dict, stats: dict) -> list[dict]:
    written: list[dict] = []
    inventory = stats.get("_inventory", {})
    for region in REGIONS:
        region_id = region["id"]
        leads = stats.get(region_id, {})
        region_root = output_root / model_key / region_id
        forecast_dir = region_root / "forecast"
        forecast_dir.mkdir(parents=True, exist_ok=True)
        validity: list[dict] = []
        for lead in sorted(leads):
            entry = leads[lead]
            field_dir = spool_root / region_id / f"f{lead:03d}"
            grids = {}
            grids_dir = field_dir / "grids"
            if grids_dir.exists():
                for grid_path in sorted(grids_dir.glob("*.json")):
                    grids[grid_path.stem] = json.loads(grid_path.read_text(encoding="utf-8"))
            fields_path = field_dir / "fields.ndjson"
            if not fields_path.is_file() or fields_path.stat().st_size == 0:
                continue
            out_path = forecast_dir / f"f{lead:03d}.json.gz"
            header = {
                "schema": "sideral-global-nwp-field-frame-v1",
                "model_key": model_key,
                "model": catalog_spec["label"],
                "provider": catalog_spec["provider"],
                "resolution": catalog_spec.get("resolution"),
                "run_utc": entry.get("run_utc") or run.isoformat(),
                "forecast_hour": lead,
                "valid_time": entry.get("valid_time"),
                "region": region_id,
                "coordinate_system": "WGS84",
                "field_count": entry["field_count"],
            }
            with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=6) as compressed:
                compressed.write("{")
                compressed.write('"metadata":')
                compressed.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")))
                compressed.write(',"grids":')
                compressed.write(json.dumps(grids, ensure_ascii=False, separators=(",", ":")))
                compressed.write(',"fields":[')
                first = True
                with fields_path.open("r", encoding="utf-8") as source:
                    for line in source:
                        line = line.strip()
                        if not line:
                            continue
                        if not first:
                            compressed.write(",")
                        compressed.write(line)
                        first = False
                compressed.write("]}")
            validity.append({
                "forecast_hour": lead,
                "valid_time": entry.get("valid_time"),
                "file": f"forecast/f{lead:03d}.json.gz",
                "field_count": entry["field_count"],
            })
        if not validity:
            continue
        manifest = {
            "schema": "sideral-global-nwp-region-manifest-v1",
            "model_key": model_key,
            "model": catalog_spec["label"],
            "provider": catalog_spec["provider"],
            "source": catalog_spec.get("source"),
            "resolution": catalog_spec.get("resolution"),
            "run_utc": run.isoformat(),
            "cycle_utc": f"{run.hour:02d}",
            "region": region,
            "forecast_hours": [item["forecast_hour"] for item in validity],
            "validities": validity,
            "variables": inventory.get(region_id, {}).get("variables", []),
            "variable_level_count": len(inventory.get(region_id, {}).get("variables", [])),
            "field_count": sum(item["field_count"] for item in validity),
            "data_format": "JSON UTF-8 compactado com gzip (.json.gz)",
            "attribution": {
                "ECMWF": "Contains ECMWF data. © ECMWF. Licensed under CC-BY-4.0 where applicable."
            } if catalog_spec["provider"] == "ECMWF" else None,
        }
        (region_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append({
            "region": region_id,
            "forecast_hours": len(validity),
            "field_count": manifest["field_count"],
            "variables": manifest["variable_level_count"],
        })
    return written


def write_model_status(path: Path, model_key: str, status: str, run: datetime,
                       message: str, errors: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "sideral-global-nwp-model-status-v1",
        "model_key": model_key,
        "status": status,
        "run_utc": run.isoformat(),
        "message": message,
        "errors": (errors or [])[:250],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_regional_stats(destination: dict, source: dict) -> None:
    """Merge one GRIB file's regional inventory into a model-wide accumulator."""
    for region_id, lead_map in source.items():
        if region_id.startswith("_"):
            continue
        region_target = destination.setdefault(region_id, {})
        for lead, entry in lead_map.items():
            target = region_target.setdefault(lead, {
                "valid_time": entry.get("valid_time"),
                "run_utc": entry.get("run_utc"),
                "field_count": 0,
                "source_files": set(),
            })
            target["field_count"] += int(entry.get("field_count", 0))
            target["valid_time"] = target.get("valid_time") or entry.get("valid_time")
            target["run_utc"] = target.get("run_utc") or entry.get("run_utc")
            target["source_files"].update(entry.get("source_files") or [])

    inventory_keys = destination.setdefault("_inventory_keys", {})
    inventory = destination.setdefault("_inventory", {})
    for region_id, item in source.get("_inventory", {}).items():
        seen = inventory_keys.setdefault(region_id, set())
        rows = inventory.setdefault(region_id, {"variables": []})["variables"]
        for row in item.get("variables", []):
            key = (
                row.get("short_name"), row.get("name"), row.get("units"),
                row.get("level_type"), str(row.get("level")),
            )
            if key not in seen:
                seen.add(key)
                rows.append(row)

    destination.setdefault("_errors", {"items": []})["items"].extend(
        source.get("_errors", {}).get("items", [])
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, choices=sorted(CATALOG_BY_KEY))
    parser.add_argument("--date", default="")
    parser.add_argument("--cycle", default="")
    parser.add_argument("--output-dir", default="global-nwp-output")
    parser.add_argument("--hours", default="")
    args = parser.parse_args()

    model_key = args.model
    spec = CATALOG_BY_KEY[model_key]
    run = utc_run(args.date or None, args.cycle or None)
    output_root = Path(args.output_dir)
    model_output = output_root / model_key
    model_output.mkdir(parents=True, exist_ok=True)
    raw_dir = output_root / "_raw" / model_key
    spool_root = output_root / "_spool" / model_key

    if model_key == "ukmet":
        write_model_status(
            model_output / "model_status.json", model_key, "blocked", run,
            "UKMET requer assinatura Global Atmospheric e credenciais do Met Office Weather DataHub. Nenhuma chave foi incluída no código."
        )
        return 0

    requested = [int(x) for x in args.hours.split(",") if x.strip()] if args.hours else leads_for(model_key, run)
    if not requested and model_key not in {"icon_dwd", "icon_ai"}:
        write_model_status(model_output / "model_status.json", model_key, "failed", run, "Nenhuma validade configurada.")
        return 1

    errors: list[str] = []
    processed_files: list[str] = []
    stats: dict = {
        "_inventory": {},
        "_inventory_keys": {},
        "_errors": {"items": []},
    }
    max_hour = int(spec["max_forecast_hour"])

    def process_file(path: Path) -> None:
        path = Path(path)
        try:
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError(f"Arquivo GRIB vazio/inexistente: {path.name}")
            piece = regionalize([path], spool_root, model_key, requested, max_hour)
            merge_regional_stats(stats, piece)
            processed_files.append(path.name)
        finally:
            path.unlink(missing_ok=True)

    try:
        if spec["adapter"] == "ecmwf-opendata":
            _, discovered_hours, errors = collect_ecmwf(
                model_key, run, raw_dir, requested, process_file
            )
        elif spec["adapter"] == "herbie":
            _, discovered_hours, errors = collect_herbie(
                model_key, run, raw_dir, requested, process_file
            )
        elif spec["adapter"] == "dwdopen":
            _, discovered_hours, errors = collect_dwd(
                model_key, raw_dir, process_file
            )
        elif spec["adapter"] == "datamart":
            if model_key not in {"cmc_gdps", "cmc_geps"}:
                raise RuntimeError("Adaptador Datamart desconhecido.")
            _, discovered_hours, errors = collect_eccc(
                model_key, run, raw_dir, requested, process_file
            )
        else:
            raise RuntimeError(f"Adaptador desconhecido: {spec['adapter']}")

        available_hours = sorted({
            int(lead)
            for region_id, lead_map in stats.items()
            if not region_id.startswith("_")
            for lead in lead_map
        })
        if not available_hours:
            raise RuntimeError("A fonte não forneceu campos GRIB convertíveis para esta rodada.")
        written = finalize_spool(spool_root, output_root, model_key, run, spec, stats)
        if not written:
            raise RuntimeError("Nenhum campo GRIB2 foi convertido para as regiões configuradas.")
        regional_errors = stats.get("_errors", {}).get("items", [])
        all_errors = errors + regional_errors
        for item in written:
            manifest_path = output_root / model_key / item["region"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["collection_status"] = "complete" if not all_errors else "partial"
            manifest["collection_errors_count"] = len(all_errors)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        summary = {
            "run_utc": run.isoformat(),
            "processed_grib_files": len(processed_files),
            "processed_file_examples": processed_files[:30],
            "regions": written,
            "source_hours": available_hours or discovered_hours,
            "regionalizer_errors": stats.get("_errors", {}).get("items", []),
            "download_errors": errors[:250],
        }
        (model_output / "collection-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        total_fields = sum(item["field_count"] for item in written)
        final_status = "published-ready" if not all_errors else "partial"
        write_model_status(
            model_output / "model_status.json", model_key, final_status, run,
            f"{len(written)} regiões preparadas; {total_fields} campos GRIB convertidos para JSON; "
            f"{len(all_errors)} ocorrências de coleta/regrade registradas.",
            all_errors
        )
        return 0
    except Exception as exc:
        write_model_status(
            model_output / "model_status.json", model_key, "failed", run,
            f"{type(exc).__name__}: {exc}", errors
        )
        print(f"[{model_key}] FALHA: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(spool_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
