from __future__ import annotations

import argparse
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from typing import Callable

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
REGION_CATALOG = json.loads((ROOT / "regions.json").read_text(encoding="utf-8"))
REGIONS = REGION_CATALOG["regions"]
CATALOG_BY_KEY = {item["key"]: item for item in CATALOG["models"]}
USER_AGENT = "Sideral-Global-NWP/1.0 (+https://github.com/slaksjsjjjjjj-dev/multimodelsss)"


def utc_run(date_text: str | None, cycle_text: str | None) -> datetime:
    # Global products (especially ECMWF Open Data) are not available at the
    # instant a cycle starts. Select the most recent cycle at least nine hours
    # old, rather than asking providers for files that are still being built.
    reference = datetime.now(timezone.utc) - timedelta(hours=9)
    if date_text:
        day = datetime.strptime(date_text, "%Y%m%d").date()
    else:
        day = reference.date()
    if cycle_text:
        cycle = int(cycle_text)
    else:
        cycle = (reference.hour // 6) * 6
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

    # Herbie expects a timezone-naive UTC datetime. Passing aware UTC caused
    # "can't compare tz-naive and tz-aware datetimes" before any GRIB download.
    herbie_date = run.astimezone(timezone.utc).replace(tzinfo=None) if run.tzinfo else run
    args = {
        "date": herbie_date,
        "model": model,
        "product": product,
        "fxx": lead,
        "save_dir": str(save_dir),
        **kwargs,
    }
    return Herbie(**args)


def download_herbie_file(model: str, product: str, run: datetime, lead: int,
                         raw_dir: Path, **kwargs) -> list[Path]:
    """Download one Herbie product with explicit errors and transient retries."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    delays = (2, 5, 10, 20)
    last_error: Exception | None = None

    for attempt in range(1, len(delays) + 1):
        before = set(raw_dir.rglob("*"))
        try:
            h = make_herbie(model, product, run, lead, raw_dir, **kwargs)
            # Herbie defaults to errors="warn", which can make a missing or
            # failed transfer look like an empty successful result. Raise it.
            result = h.download(errors="raise", verbose=False)
            files: list[Path] = []

            if result:
                try:
                    candidate = Path(result)
                    if candidate.is_file() and candidate.stat().st_size > 0:
                        files.append(candidate)
                except (TypeError, OSError):
                    pass

            if not files:
                after = set(raw_dir.rglob("*"))
                files = [
                    candidate for candidate in after - before
                    if candidate.is_file()
                    and candidate.suffix.lower() in {".grib2", ".grb2", ".grb"}
                    and candidate.stat().st_size > 0
                ]

            files = sorted(set(files))
            if files:
                return files
            raise RuntimeError(
                f"Herbie terminou sem retornar GRIB2 válido para "
                f"{model}/{product} F{lead:03d}."
            )
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            permanent_missing = any(token in message for token in (
                "404", "not found", "no data found", "no matching file",
                "does not exist", "not available for this forecast hour",
            ))
            if permanent_missing or attempt == len(delays):
                raise RuntimeError(
                    f"{model}/{product} F{lead:03d} falhou após {attempt} tentativa(s): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            wait = delays[attempt - 1]
            print(
                f"[Herbie] {model}/{product} F{lead:03d}: tentativa "
                f"{attempt}/{len(delays)} falhou ({type(exc).__name__}: {exc}); "
                f"nova tentativa em {wait}s.",
                flush=True,
            )
            time.sleep(wait)

    raise RuntimeError(f"Download Herbie falhou: {last_error}") from last_error

def _ecmwf_step_end(step: int | str) -> int:
    if isinstance(step, int):
        return step
    if "-" in str(step):
        return int(str(step).split("-", 1)[1])
    return int(step)


def _ecmwf_range_steps(start: int, stop: int, increment: int) -> list[int]:
    return list(range(start, stop + 1, increment))


def _ecmwf_tasks(model_key: str, run: datetime) -> list[tuple[str, str, list[int | str], str]]:
    """Return the documented stream/type/step combinations for this cycle."""
    hres_00_12 = (
        _ecmwf_range_steps(0, 144, 3)
        + _ecmwf_range_steps(150, 240, 6)
    )
    hres_06_18 = _ecmwf_range_steps(0, 90, 3)
    ens_00_12 = (
        _ecmwf_range_steps(0, 144, 3)
        + _ecmwf_range_steps(150, 360, 6)
    )
    ens_06_18 = _ecmwf_range_steps(0, 144, 3)
    aifs_steps = _ecmwf_range_steps(0, 360, 6)
    ep_instant = _ecmwf_range_steps(12, 360, 12)
    ep_daily = [f"{12 * i}-{12 * i + 24}" for i in range(29)]
    ep_averaged = ["120-168", "168-240", "120-240"]

    if model_key == "ecmwf_ifs":
        # Cycle 50r1 onward: 06/18 use oper/wave too; scda/scwv are retired.
        return [
            ("oper", "fc", hres_00_12 if run.hour in (0, 12) else hres_06_18, "atmosphere"),
            ("wave", "fc", ens_00_12 if run.hour in (0, 12) else _ecmwf_range_steps(0, 144, 3), "wave"),
        ]

    if model_key == "ecmwf_eps":
        ensemble_steps = ens_00_12 if run.hour in (0, 12) else ens_06_18
        tasks = [
            ("enfo", "pf", ensemble_steps, "atmosphere-members"),
            ("waef", "pf", ensemble_steps, "wave-members"),
        ]
        if run.hour in (0, 12):
            tasks.extend([
                ("enfo", "em", ens_00_12, "atmosphere-mean"),
                ("enfo", "es", ens_00_12, "atmosphere-spread"),
                ("enfo", "ep", ep_instant, "atmosphere-probabilities-instant"),
                ("enfo", "ep", ep_daily, "atmosphere-probabilities-daily"),
                ("waef", "ep", ep_instant, "wave-probabilities-instant"),
            ])
        return tasks

    if model_key == "ecmwf_aifs":
        return [
            ("oper", "fc", aifs_steps, "atmosphere"),
            ("wave", "fc", aifs_steps, "wave"),
        ]

    if model_key == "ecmwf_aifs_ens":
        return [
            ("enfo", "cf", aifs_steps, "atmosphere-control"),
            ("enfo", "pf", aifs_steps, "atmosphere-members"),
            ("enfo", "em", aifs_steps, "atmosphere-mean"),
            ("enfo", "es", aifs_steps, "atmosphere-spread"),
            ("waef", "cf", aifs_steps, "wave-control"),
            ("waef", "pf", aifs_steps, "wave-members"),
            ("enfo", "ep", ep_instant, "atmosphere-probabilities-instant"),
            ("enfo", "ep", ep_daily, "atmosphere-probabilities-daily"),
            ("enfo", "ep", ep_averaged, "atmosphere-probabilities-averaged"),
            ("waef", "ep", ep_instant, "wave-probabilities-instant"),
            ("waef", "ep", ep_averaged, "wave-probabilities-averaged"),
        ]

    raise ValueError(f"Modelo ECMWF desconhecido: {model_key}")


def collect_ecmwf(model_key: str, run: datetime, raw_dir: Path,
                   requested: list[int],
                   process_file: Callable[[Path], None] | None = None
                   ) -> tuple[list[Path], list[int], list[str]]:
    """Download every available field in each documented ECMWF product stream.

    The requests do not specify param/levelist/number, so every published
    parameter, level and (for PF products) ensemble member is returned.
    """
    from ecmwf.opendata import Client

    upstream_model = {
        "ecmwf_ifs": "ifs",
        "ecmwf_eps": "ifs",
        "ecmwf_aifs": "aifs-single",
        "ecmwf_aifs_ens": "aifs-ens",
    }[model_key]
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

    for stream, product_type, steps, task_label in _ecmwf_tasks(model_key, run):
        if not steps:
            continue
        # Full forecast files can be very large; process one forecast lead at a
        # time. EP probability files are much smaller, so keep their step bundles
        # together to reduce request count.
        step_groups = [steps] if "probabilities" in task_label else [[step] for step in steps]
        for step_group in step_groups:
            step_label = (
                f"{step_group[0]}-to-{step_group[-1]}-bundle"
                if len(step_group) > 1 else str(step_group[0])
            )
            safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_label)
            target = raw_dir / safe_label / f"{stream}-{product_type}-{step_label}.grib2"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.is_file() or target.stat().st_size == 0:
                    client.retrieve(
                        date=run_date,
                        time=run.hour,
                        stream=stream,
                        type=product_type,
                        step=step_group if len(step_group) > 1 else step_group[0],
                        target=str(target),
                    )
                if not target.is_file() or target.stat().st_size == 0:
                    raise RuntimeError("ECMWF API did not create a non-empty GRIB2 file.")
                if process_file:
                    process_file(target)
                else:
                    downloaded.append(target)
                found_hours.update(_ecmwf_step_end(step) for step in step_group)
            except Exception as exc:
                errors.append(
                    f"{stream}/{product_type}/{task_label}/{step_label}: "
                    f"{type(exc).__name__}: {exc}"
                )
                target.unlink(missing_ok=True)
            time.sleep(0.15)

    return sorted(set(downloaded)), sorted(found_hours), errors



def collect_herbie(model_key: str, run: datetime, raw_dir: Path, requested: list[int],
                   process_file: Callable[[Path], None] | None = None,
                   checkpoint: Callable[[int], None] | None = None
                   ) -> tuple[list[Path], list[int], list[str]]:
    """Collect NOAA models with bounded parallel downloads and durable progress."""
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
    output_root = raw_dir.parent.parent
    model_output = output_root / model_key
    model_output.mkdir(parents=True, exist_ok=True)
    progress_path = model_output / "collection-progress.json"

    downloaded: list[Path] = []
    found_hours: set[int] = set()
    completed_hours: set[int] = set()
    missing_hours: set[int] = set()
    errors: list[str] = []

    def write_progress() -> None:
        payload = {
            "schema": "sideral-global-nwp-progress-v1",
            "model_key": model_key,
            "run_utc": run.isoformat(),
            "status": "collecting",
            "requested_forecast_hours": len(requested),
            "completed_forecast_hours": sorted(completed_hours),
            "available_forecast_hours": sorted(found_hours),
            "missing_forecast_hours": sorted(missing_hours),
            "completed_count": len(completed_hours),
            "errors": errors[:250],
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        temp_path = progress_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(progress_path)

    def collect_one_lead(lead: int) -> tuple[int, list[Path], list[str]]:
        lead_files: list[Path] = []
        lead_errors: list[str] = []
        for source_model, product, kwargs in products:
            options = dict(kwargs)
            dest = (
                raw_dir / f"f{lead:03d}" / source_model / product
                / "_".join(f"{key}-{value}" for key, value in sorted(options.items()))
            )
            try:
                found = download_herbie_file(
                    source_model, product, run, lead, dest, **options
                )
                if found:
                    lead_files.extend(found)
                else:
                    lead_errors.append(
                        f"F{lead:03d} {source_model}/{product}: fonte não retornou arquivo."
                    )
            except Exception as exc:
                lead_errors.append(
                    f"F{lead:03d} {source_model}/{product}: "
                    f"{type(exc).__name__}: {exc}"
                )
        return lead, sorted(set(lead_files)), lead_errors

    write_progress()
    # Download a few forecast leads concurrently; process GRIB files in the
    # main thread to avoid concurrent appends to the same regional NDJSON files.
    # GFS has the longest hourly series, so bounded concurrency is particularly
    # important there. Each lead/product writes to its own destination folder.
    max_workers = 4 if model_key == "gfs" else 3
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_lead = {
            executor.submit(collect_one_lead, lead): lead for lead in requested
        }
        for future in as_completed(future_to_lead):
            lead = future_to_lead[future]
            try:
                returned_lead, lead_files, lead_errors = future.result()
                if returned_lead != lead:
                    raise RuntimeError("O worker retornou uma validade diferente da solicitada.")
            except Exception as exc:
                lead_files = []
                lead_errors = [
                    f"F{lead:03d}: erro inesperado no worker: "
                    f"{type(exc).__name__}: {exc}"
                ]

            errors.extend(lead_errors)
            if lead_files:
                found_hours.add(lead)
                for grib_path in lead_files:
                    if process_file:
                        try:
                            process_file(grib_path)
                        except Exception as exc:
                            errors.append(
                                f"F{lead:03d} {grib_path.name}: processamento GRIB falhou: "
                                f"{type(exc).__name__}: {exc}"
                            )
                    else:
                        downloaded.append(grib_path)
                # Persist the complete F-hour immediately. If the runner is
                # later cancelled, already completed JSON files remain uploadable.
                if checkpoint:
                    try:
                        checkpoint(lead)
                    except Exception as exc:
                        errors.append(
                            f"F{lead:03d}: checkpoint JSON falhou: "
                            f"{type(exc).__name__}: {exc}"
                        )
            else:
                missing_hours.add(lead)

            completed_hours.add(lead)
            write_progress()
            print(
                f"[{model_key}] progresso {len(completed_hours)}/{len(requested)} "
                f"validade(s); última F{lead:03d}; "
                f"{len(found_hours)} com arquivo e {len(missing_hours)} sem arquivo.",
                flush=True,
            )

    final_progress = json.loads(progress_path.read_text(encoding="utf-8"))
    final_progress["status"] = "download-finished"
    final_progress["errors"] = errors[:250]
    temp_path = progress_path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(final_progress, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_path.replace(progress_path)

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
    # GDPS and GEPS both publish on the current Datamart /today tree.
    # Both issue runs at 00Z/12Z; 06Z/18Z consumers use the latest prior run.
    source_run = run.replace(hour=0 if run.hour < 12 else 12)
    cycle = f"{source_run.hour:02d}"
    if model_key == "cmc_gdps":
        product_types = ("model_gdps/15km",)
    elif model_key == "cmc_geps":
        # raw = every member; products = ensemble probabilities/statistics.
        product_types = (
            "ensemble/geps/grib2/raw",
            "ensemble/geps/grib2/products",
        )
    else:
        raise ValueError(f"Modelo ECCC desconhecido: {model_key}")

    downloaded: list[Path] = []
    found_hours: set[int] = set()
    errors: list[str] = []
    for lead in requested:
        lead_downloaded = False
        for product_path in product_types:
            base = (
                f"https://dd.weather.gc.ca/today/{product_path}/"
                f"{cycle}/{lead:03d}/"
            )
            try:
                links = list_grib_links(base)
                if not links:
                    errors.append(
                        f"{product_path}/F{lead:03d}: nenhum GRIB2 publicado neste ciclo."
                    )
                    continue
                lead_dir = raw_dir / product_path.rsplit("/", 1)[-1] / f"f{lead:03d}"
                lead_dir.mkdir(parents=True, exist_ok=True)

                def download_one(url: str) -> Path | None:
                    name = url.rsplit("/", 1)[-1]
                    target = lead_dir / name
                    if ensure_request(url, target, timeout=(20, 240)):
                        return target
                    return None

                # Download each forecast lead concurrently to avoid crawling
                # thousands of Datamart files serially. Decoding and writes to
                # the regional spool remain serial to keep the JSON intact.
                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = {
                        executor.submit(download_one, url): url for url in links
                    }
                    for future in as_completed(futures):
                        url = futures[future]
                        name = url.rsplit("/", 1)[-1]
                        try:
                            target = future.result()
                            if target is None:
                                continue
                            lead_downloaded = True
                            if process_file:
                                process_file(target)
                            else:
                                downloaded.append(target)
                        except Exception as exc:
                            errors.append(
                                f"{product_path}/F{lead:03d} {name}: "
                                f"{type(exc).__name__}: {exc}"
                            )
            except Exception as exc:
                errors.append(
                    f"{product_path}/F{lead:03d}: {type(exc).__name__}: {exc}"
                )
        if lead_downloaded:
            found_hours.add(lead)
        else:
            errors.append(f"F{lead:03d}: nenhum produto ECCC disponível.")
        time.sleep(0.05)

    return sorted(set(downloaded)), sorted(found_hours), errors



def collect_dwd(model_key: str, raw_dir: Path,
                process_file: Callable[[Path], None] | None = None
                ) -> tuple[list[Path], list[int], list[str]]:
    """Collect every DWD field on its native ICON mesh.

    ecCodes exposes a latitude/longitude coordinate for every unstructured
    ICON point, so the regionalizer can clip the native mesh directly. No
    CDO remapping or interpolation is required for the regional JSON archive.
    """
    upstream = {
        "icon_dwd": "icon",
        "icon_ai": "aicon",
    }[model_key]
    try:
        from dwdopen import DWD
        raw_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        errors: list[str] = []
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
                            else str(
                                getattr(level_type, "alias", None)
                                or getattr(level_type, "code", level_type)
                            )
                        )
                        safe_parameter = re.sub(r"[^A-Za-z0-9_.-]+", "-", parameter)
                        safe_level = re.sub(r"[^A-Za-z0-9_.-]+", "-", level_suffix)
                        target = (
                            raw_dir / safe_parameter / safe_level
                            / f"{safe_parameter}-{safe_level}-all-members-all-steps.grib2"
                        )
                        try:
                            options = {"parameters": parameter, "steps": "all"}
                            # ICON/AICON parameters can exist on multiple vertical
                            # coordinates. Select each coordinate explicitly to avoid
                            # dwdopen raising AmbiguousSelectionError.
                            if level_type is not None:
                                options["level_type"] = level_type
                                options["levels"] = "all"
                            query = model.select(**options)
                            plan = query.resolve(run=source_run, require="partial")
                            if not plan.assets:
                                errors.append(
                                    f"{parameter}/{level_suffix}: nenhum asset disponível."
                                )
                                continue
                            if getattr(plan, "missing", None):
                                errors.append(
                                    f"{parameter}/{level_suffix}: "
                                    f"{len(plan.missing)} combinações de nível/membro/validade ausentes."
                                )
                            plan.download(str(target))
                            if target.is_file() and target.stat().st_size > 0:
                                if process_file:
                                    process_file(target)
                                else:
                                    downloaded.append(target)
                            else:
                                errors.append(
                                    f"{parameter}/{level_suffix}: download não gerou GRIB2."
                                )
                        except Exception as exc:
                            errors.append(
                                f"{parameter}/{level_suffix}: {type(exc).__name__}: {exc}"
                            )
                        finally:
                            if process_file:
                                target.unlink(missing_ok=True)
                except Exception as exc:
                    errors.append(f"{parameter}: {type(exc).__name__}: {exc}")
                print(f"[{model_key}] parâmetro {parameter}: concluído.")

        if process_file:
            return [], [], errors
        if not downloaded:
            raise RuntimeError("DWD não forneceu GRIB2 para as variáveis/níveis solicitados.")
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
                    source_key = source.stem.split("-", 1)[0]
                    short_name_value = message_value(gid, "shortName", "")
                    readable_name_value = message_value(gid, "name", "")
                    if not short_name_value or str(short_name_value).lower() == "unknown":
                        short_name_value = source_key
                    if not readable_name_value or str(readable_name_value).lower() == "unknown":
                        readable_name_value = source_key
                    meta = {
                        "short_name": str(short_name_value),
                        "name": str(readable_name_value),
                        "units": str(message_value(gid, "units", "")),
                        "parameter_id": message_value(gid, "paramId"),
                        "grid_type": str(message_value(gid, "gridType", "unknown")),
                        "grid_ni": message_value(gid, "Ni"),
                        "grid_nj": message_value(gid, "Nj"),
                        "grid_points": message_value(gid, "numberOfDataPoints"),
                        "i_scans_negatively": message_value(gid, "iScansNegatively"),
                        "j_scans_positively": message_value(gid, "jScansPositively"),
                        "j_points_consecutive": message_value(gid, "jPointsAreConsecutive"),
                        "alternative_row_scanning": message_value(gid, "alternativeRowScanning"),
                        "level_type": str(message_value(gid, "typeOfLevel", "")),
                        "level": message_value(gid, "level"),
                        "step_type": str(message_value(gid, "stepType", "")),
                        "step_range": str(message_value(gid, "stepRange", "")),
                        "member": message_value(gid, "number", message_value(gid, "perturbationNumber")),
                        "ensemble_forecast_type": message_value(gid, "typeOfEnsembleForecast"),
                        "statistical_process": message_value(gid, "typeOfStatisticalProcessing"),
                        "percentile": message_value(gid, "percentileValue"),
                        "probability": message_value(gid, "probability"),
                        "lower_limit": message_value(gid, "lowerLimit"),
                        "upper_limit": message_value(gid, "upperLimit"),
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
                        grid_signature = {
                            "grid_type": meta["grid_type"],
                            "grid_ni": meta["grid_ni"],
                            "grid_nj": meta["grid_nj"],
                            "grid_points": meta["grid_points"],
                            "i_scans_negatively": meta["i_scans_negatively"],
                            "j_scans_positively": meta["j_scans_positively"],
                            "j_points_consecutive": meta["j_points_consecutive"],
                            "alternative_row_scanning": meta["alternative_row_scanning"],
                        }
                        signature = hashlib.sha1(
                            selected_lats.tobytes()
                            + selected_lons.tobytes()
                            + json.dumps(grid_signature, sort_keys=True).encode("utf-8")
                        ).hexdigest()[:16]
                        grid_path = grids_dir / f"{signature}.json"
                        if not grid_path.exists():
                            grid_path.write_text(
                                safe_json_line({
                                    **grid_signature,
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
                            meta["level_type"], str(meta["level"]), meta["grid_type"],
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
                 "level_type": row[3], "level": row[4], "grid_type": row[5]}
                for row in sorted(rows)
            ]
        }
        for region_id, rows in field_inventory.items()
    }
    stats["_errors"] = {"items": errors[:500]}
    return stats


def finalize_spool(spool_root: Path, output_root: Path, model_key: str,
                   run: datetime, catalog_spec: dict, stats: dict,
                   only_leads: set[int] | None = None) -> list[dict]:
    """Write one individually compressed JSON file for each available F-hour."""
    written: list[dict] = []
    inventory = stats.get("_inventory", {})
    requested_hours = stats.get("_requested_hours", [])

    for region in REGIONS:
        region_id = region["id"]
        leads = stats.get(region_id, {})
        if only_leads is not None:
            leads = {
                int(lead): entry for lead, entry in leads.items()
                if int(lead) in only_leads
            }
        region_root = output_root / model_key / region_id
        forecast_dir = region_root / "forecast"
        forecast_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = region_root / "manifest.json"
        validity: list[dict] = []

        for lead in sorted(leads):
            entry = leads[lead]
            field_dir = spool_root / region_id / f"f{lead:03d}"
            grids: dict[str, dict] = {}
            grids_dir = field_dir / "grids"
            if grids_dir.exists():
                for grid_path in sorted(grids_dir.glob("*.json")):
                    grids[grid_path.stem] = json.loads(
                        grid_path.read_text(encoding="utf-8")
                    )

            fields_path = field_dir / "fields.ndjson"
            if not fields_path.is_file() or fields_path.stat().st_size == 0:
                continue

            # Exactly one JSON document per available forecast hour, containing
            # every field/level/member collected for this model and region.
            with fields_path.open("r", encoding="utf-8") as field_source:
                total_fields = sum(1 for line in field_source if line.strip())
            if total_fields == 0:
                continue

            file_name = f"F{lead:03d}.json.gz"
            out_path = forecast_dir / file_name
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
                "field_count": total_fields,
                "parts": 1,
            }

            with fields_path.open("r", encoding="utf-8") as source:
                with gzip.open(
                    out_path, "wt", encoding="utf-8", compresslevel=6
                ) as compressed:
                    compressed.write("{")
                    compressed.write('"metadata":')
                    compressed.write(json.dumps(
                        header, ensure_ascii=False, separators=(",", ":")
                    ))
                    compressed.write(',"grids":')
                    compressed.write(json.dumps(
                        grids, ensure_ascii=False, separators=(",", ":")
                    ))
                    compressed.write(',"fields":[')
                    first_field = True
                    for line in source:
                        line = line.strip()
                        if not line:
                            continue
                        if not first_field:
                            compressed.write(",")
                        compressed.write(line)
                        first_field = False
                    compressed.write("]}")

            validity.append({
                "forecast_hour": lead,
                "valid_time": entry.get("valid_time"),
                "file": f"forecast/{file_name}",
                "field_count": total_fields,
            })

        previous_manifest = {}
        if only_leads is not None and manifest_path.is_file():
            previous_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            validity_by_hour = {
                int(item["forecast_hour"]): item
                for item in previous_manifest.get("validities", [])
            }
            validity_by_hour.update({
                int(item["forecast_hour"]): item for item in validity
            })
            validity = [
                validity_by_hour[lead] for lead in sorted(validity_by_hour)
            ]

        if not validity:
            continue

        actual_hours = sorted({item["forecast_hour"] for item in validity})
        missing_hours = sorted(set(requested_hours) - set(actual_hours))
        current_variables = inventory.get(region_id, {}).get("variables", [])
        if previous_manifest:
            variable_map = {
                (
                    row.get("short_name"), row.get("name"), row.get("units"),
                    row.get("level_type"), str(row.get("level")), row.get("grid_type"),
                ): row
                for row in previous_manifest.get("variables", [])
            }
            for row in current_variables:
                variable_map[(
                    row.get("short_name"), row.get("name"), row.get("units"),
                    row.get("level_type"), str(row.get("level")), row.get("grid_type"),
                )] = row
            current_variables = list(variable_map.values())

        manifest = {
            "schema": "sideral-global-nwp-region-manifest-v1",
            "model_key": model_key,
            "model": catalog_spec["label"],
            "provider": catalog_spec["provider"],
            "source": catalog_spec.get("source"),
            "resolution": catalog_spec.get("resolution"),
            "run_utc": next(
                (
                    entry.get("run_utc")
                    for entry in leads.values()
                    if entry.get("run_utc")
                ),
                run.isoformat(),
            ),
            "cycle_utc": next(
                (
                    str(entry.get("run_utc"))[11:13]
                    for entry in leads.values()
                    if entry.get("run_utc")
                ),
                f"{run.hour:02d}",
            ),
            "region": region,
            "requested_forecast_hours": requested_hours,
            "forecast_hours": actual_hours,
            "missing_forecast_hours": missing_hours,
            "validities": validity,
            "variables": current_variables,
            "variable_level_count": len(current_variables),
            "field_count": sum(item["field_count"] for item in validity),
            "data_format": "um JSON por validade, compactado individualmente em gzip (.json.gz); não é ZIP",
            "collection_status": "in-progress" if only_leads is not None else "finalizing",
            "attribution": {
                "ECMWF": "Contains ECMWF data. © ECMWF. Licensed under CC-BY-4.0 where applicable."
            } if catalog_spec["provider"] == "ECMWF" else None,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append({
            "region": region_id,
            "forecast_hours": len(validity),
            "field_count": manifest["field_count"],
            "variables": manifest["variable_level_count"],
            "missing_forecast_hours": len(missing_hours),
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
                row.get("grid_type"),
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
        "_requested_hours": requested,
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

    def checkpoint_lead(lead: int) -> None:
        finalize_spool(
            spool_root, output_root, model_key, run, spec, stats,
            only_leads={lead},
        )

    try:
        if spec["adapter"] == "ecmwf-opendata":
            _, discovered_hours, errors = collect_ecmwf(
                model_key, run, raw_dir, requested, process_file
            )
        elif spec["adapter"] == "herbie":
            _, discovered_hours, errors = collect_herbie(
                model_key, run, raw_dir, requested, process_file, checkpoint_lead
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
            "requested_forecast_hours": requested,
            "source_hours": available_hours or discovered_hours,
            "missing_forecast_hours": sorted(set(requested) - set(available_hours)),
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
