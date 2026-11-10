from __future__ import annotations

import argparse
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
# Modo enxuto: as regiões do Brasil e da Argentina estão contidas em
# south_america e podem ser recortadas pelo cliente. Para voltar a publicá-las,
# remova o filtro abaixo.
REGIONS = [
    region for region in REGION_CATALOG["regions"]
    if not region["id"].startswith(("brazil_", "argentina_"))
]
CATALOG_BY_KEY = {item["key"]: item for item in CATALOG["models"]}
USER_AGENT = "Sideral-Global-NWP/1.0 (+https://github.com/slaksjsjjjjjj-dev/multimodelsss)"
MAX_FRAME_PART_RAW_BYTES = 32 * 1024 * 1024


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_path.replace(path)



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
        # NOAA AIGFS operational files are published every 6 hours.
        return list(range(0, 385, 6))
    if model_key == "gefs_ai":
        # NOAA AIGEFS AI products use 6-hour forecast intervals.
        return list(range(0, 385, 6))
    if model_key == "hgefs":
        # NOAA HGEFS mean/spread products are published every 6 hours.
        return list(range(0, 241, 6))
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


def is_valid_grib2(path: Path) -> bool:
    """Fast structural check: GRIB magic at start and 7777 terminator at EOF."""
    try:
        if not path.is_file() or path.stat().st_size < 20:
            return False
        with path.open("rb") as stream:
            magic = stream.read(4)
            if magic == b"GRIB":
                stream.seek(-4, os.SEEK_END)
                return stream.read(4) == b"7777"
            if magic[:2] == b"\x1f\x8b":
                with gzip.open(path, "rb") as decoded:
                    if decoded.read(4) != b"GRIB":
                        return False
                    decoded.seek(-4, os.SEEK_END)
                    return decoded.read(4) == b"7777"
    except (OSError, EOFError, gzip.BadGzipFile):
        return False
    return False


def ensure_request(url: str, destination: Path, timeout: tuple[int, int] = (20, 180)) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if is_valid_grib2(destination):
            return True
        destination.unlink(missing_ok=True)

    temp = destination.with_suffix(destination.suffix + ".part")
    temp.unlink(missing_ok=True)
    with requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True
    ) as response:
        if response.status_code == 404:
            return False
        response.raise_for_status()
        with temp.open("wb") as stream:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    stream.write(chunk)

    if not is_valid_grib2(temp):
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Download GRIB2 incompleto ou inválido: {url}")
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
                    if is_valid_grib2(candidate):
                        files.append(candidate)
                except (TypeError, OSError):
                    pass

            if not files:
                after = set(raw_dir.rglob("*"))
                files = [
                    candidate for candidate in after - before
                    if candidate.is_file()
                    and candidate.suffix.lower() in {".grib2", ".grb2", ".grb"}
                    and is_valid_grib2(candidate)
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


# NOMADS recommends spacing automated GRIB-filter requests by 10 seconds.
# A single shared gate applies that limit across all GFS worker threads.
_NOMADS_REQUEST_LOCK = threading.Lock()
_NOMADS_NEXT_REQUEST_AT = 0.0


def _wait_for_nomads_slot() -> None:
    global _NOMADS_NEXT_REQUEST_AT
    with _NOMADS_REQUEST_LOCK:
        delay = _NOMADS_NEXT_REQUEST_AT - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _NOMADS_NEXT_REQUEST_AT = time.monotonic() + 10.0


def download_gfs_regional(
    product: str, run: datetime, lead: int, raw_dir: Path
) -> Path:
    """Download the complete GFS parameter/level set, clipped to SIDERAL's AOI."""
    scripts = {
        "pgrb2.0p25": "filter_gfs_0p25.pl",
        "pgrb2b.0p25": "filter_gfs_0p25b.pl",
    }
    try:
        script = scripts[product]
    except KeyError as exc:
        raise ValueError(f"Produto GFS desconhecido: {product}") from exc

    target_dir = (
        raw_dir / f"f{lead:03d}" / "gfs" / product
        / f"gfs.{run:%Y%m%d}" / f"{run.hour:02d}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"gfs.t{run.hour:02d}z.{product}.f{lead:03d}.grib2"
    if target.is_file() and target.stat().st_size > 0:
        return target

    url = f"https://nomads.ncep.noaa.gov/cgi-bin/{script}"
    params = {
        "file": f"gfs.t{run.hour:02d}z.{product}.f{lead:03d}",
        "all_var": "on",
        "all_lev": "on",
        "subregion": "",
        # One continuous 120E–330E box includes the South Pacific, South
        # America and the western South Pacific edge used by SIDERAL.
        "leftlon": "120",
        "rightlon": "330",
        "toplat": "15",
        "bottomlat": "-60",
        "dir": f"/gfs.{run:%Y%m%d}/{run.hour:02d}/atmos/",
    }

    delays = (15, 30, 60)
    last_error: Exception | None = None
    for attempt in range(1, len(delays) + 1):
        _wait_for_nomads_slot()
        temp = target.with_suffix(target.suffix + ".part")
        try:
            with requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=(30, 900),
                stream=True,
            ) as response:
                if response.status_code == 404:
                    raise FileNotFoundError(
                        f"NOMADS não publicou {product} F{lead:03d} "
                        f"para {run:%Y-%m-%d %H}Z."
                    )
                response.raise_for_status()
                with temp.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            output.write(chunk)

            with temp.open("rb") as downloaded:
                magic = downloaded.read(4)
            if magic != b"GRIB" or not is_valid_grib2(temp):
                try:
                    detail = temp.read_text(encoding="utf-8", errors="replace")[:400]
                except OSError:
                    detail = repr(magic)
                raise RuntimeError(
                    f"NOMADS não retornou GRIB para {product} F{lead:03d}: "
                    f"{detail!r}"
                )
            if temp.stat().st_size < 16:
                raise RuntimeError(f"GRIB truncado para {product} F{lead:03d}.")
            temp.replace(target)
            return target
        except FileNotFoundError:
            temp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            last_error = exc
            temp.unlink(missing_ok=True)
            if attempt == len(delays):
                break
            wait = delays[attempt - 1]
            print(
                f"[GFS/NOMADS] {product} F{lead:03d}: tentativa "
                f"{attempt}/{len(delays)} falhou ({type(exc).__name__}: {exc}); "
                f"nova tentativa em {wait}s.",
                flush=True,
            )
            time.sleep(wait)

    raise RuntimeError(
        f"NOMADS falhou em {product} F{lead:03d}: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error

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
        # Modo enxuto: sem membros individuais (pf). Só média (em),
        # espalhamento (es) e probabilidades (ep). Os 50 membros multiplicavam
        # o volume e o tempo de coleta e interrompiam o job.
        ensemble_steps = ens_00_12 if run.hour in (0, 12) else ens_06_18
        tasks = [
            ("enfo", "em", ensemble_steps, "atmosphere-mean"),
            ("enfo", "es", ensemble_steps, "atmosphere-spread"),
        ]
        if run.hour in (0, 12):
            tasks.extend([
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
        # Modo enxuto: sem controle (cf) nem membros (pf).
        return [
            ("enfo", "em", aifs_steps, "atmosphere-mean"),
            ("enfo", "es", aifs_steps, "atmosphere-spread"),
            ("enfo", "ep", ep_instant, "atmosphere-probabilities-instant"),
            ("enfo", "ep", ep_daily, "atmosphere-probabilities-daily"),
            ("enfo", "ep", ep_averaged, "atmosphere-probabilities-averaged"),
            ("waef", "ep", ep_instant, "wave-probabilities-instant"),
            ("waef", "ep", ep_averaged, "wave-probabilities-averaged"),
        ]

    raise ValueError(f"Modelo ECMWF desconhecido: {model_key}")


def collect_ecmwf(
    model_key: str,
    run: datetime,
    raw_dir: Path,
    requested: list[int],
    process_file: Callable[[Path], None] | None = None,
    checkpoint: Callable[[int], None] | None = None,
) -> tuple[list[Path], list[int], list[str]]:
    """Download ECMWF streams with retries, integrity checks and checkpoints."""
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
    model_output = raw_dir.parent.parent / model_key
    model_output.mkdir(parents=True, exist_ok=True)
    progress_path = model_output / "collection-progress.json"

    downloaded: list[Path] = []
    found_hours: set[int] = set()
    completed_hours: set[int] = set()
    errors: list[str] = []
    run_date = run.strftime("%Y%m%d")

    def write_progress(status: str = "collecting") -> None:
        available = []
        for lead in sorted(completed_hours):
            if any(
                (
                    model_output / region["id"] / "forecast"
                    / f"F{lead:03d}.json.gz"
                ).is_file()
                or any(
                    (
                        model_output / region["id"] / "forecast"
                    ).glob(f"F{lead:03d}-part*.json.gz")
                )
                for region in REGIONS
            ):
                available.append(lead)
        payload = {
            "schema": "sideral-global-nwp-progress-v1",
            "model_key": model_key,
            "run_utc": run.isoformat(),
            "status": status,
            "requested_forecast_hours": len(requested),
            "completed_forecast_hours": sorted(completed_hours),
            "available_forecast_hours": available,
            "missing_forecast_hours": sorted(
                completed_hours - set(available)
            ),
            "completed_count": len(completed_hours),
            "errors": errors[-250:],
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        temp_path = progress_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(progress_path)

    def finish_group(step_group: list[int | str], success: bool) -> None:
        leads = sorted({_ecmwf_step_end(step) for step in step_group})
        completed_hours.update(leads)
        if success:
            found_hours.update(leads)
            if checkpoint:
                for lead in leads:
                    try:
                        checkpoint(lead)
                    except Exception as exc:
                        errors.append(
                            f"F{lead:03d}: checkpoint ECMWF falhou: "
                            f"{type(exc).__name__}: {exc}"
                        )
        write_progress()

    write_progress()

    for stream, product_type, steps, task_label in _ecmwf_tasks(model_key, run):
        if not steps:
            continue
        if "probabilities" in task_label:
            # Avoid a single multi-hundred-hour bundle; short chunks reduce
            # interrupted ECMWF transfers and WrongLengthError frequency.
            step_groups = [
                steps[index:index + 6]
                for index in range(0, len(steps), 6)
            ]
        else:
            step_groups = [[step] for step in steps]

        for step_group in step_groups:
            step_label = (
                f"{step_group[0]}-to-{step_group[-1]}-bundle"
                if len(step_group) > 1 else str(step_group[0])
            )
            safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_label)
            target = raw_dir / safe_label / f"{stream}-{product_type}-{step_label}.grib2"
            target.parent.mkdir(parents=True, exist_ok=True)

            last_error: Exception | None = None
            valid_download = False
            for attempt, wait in enumerate((0, 5, 15), start=1):
                try:
                    if not is_valid_grib2(target):
                        target.unlink(missing_ok=True)
                        client.retrieve(
                            date=run_date,
                            time=run.hour,
                            stream=stream,
                            type=product_type,
                            step=step_group if len(step_group) > 1 else step_group[0],
                            target=str(target),
                        )
                    if not is_valid_grib2(target):
                        raise RuntimeError(
                            "ECMWF não produziu GRIB2 íntegro (assinatura/terminador inválidos)."
                        )
                    valid_download = True
                    break
                except Exception as exc:
                    last_error = exc
                    target.unlink(missing_ok=True)
                    if attempt < 3:
                        print(
                            f"[{model_key}] {stream}/{product_type}/{step_label}: "
                            f"tentativa {attempt}/3 falhou ({type(exc).__name__}: {exc}); "
                            f"nova tentativa em {wait}s.",
                            flush=True,
                        )
                        time.sleep(wait)

            if not valid_download:
                errors.append(
                    f"{stream}/{product_type}/{task_label}/{step_label}: "
                    f"{type(last_error).__name__}: {last_error}"
                )
                finish_group(step_group, False)
                time.sleep(0.15)
                continue

            success = False
            try:
                if process_file:
                    process_file(target)
                else:
                    downloaded.append(target)
                success = True
            except Exception as exc:
                errors.append(
                    f"{stream}/{product_type}/{task_label}/{step_label}: "
                    f"processamento {type(exc).__name__}: {exc}"
                )
                target.unlink(missing_ok=True)
            finally:
                finish_group(step_group, success)
            time.sleep(0.15)

    write_progress("download-finished")
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
                if model_key == "gfs":
                    found = [
                        download_gfs_regional(product, run, lead, raw_dir)
                    ]
                else:
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
    max_workers = 1 if model_key == "gfs" else 3
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


def eccc_base_urls(product_path: str, source_run: datetime, cycle: str,
                   lead: int) -> list[str]:
    """Diretórios do Datamart para uma validade, do melhor candidato ao pior.

    /today/ sempre significa o dia UTC atual, então só vale quando a rodada é
    de hoje. Os diretórios datados (YYYYMMDD/WXO-DD) ficam disponíveis por cerca
    de 30 dias e funcionam também para rodadas manuais ou atrasadas.
    """
    day = source_run.strftime("%Y%m%d")
    suffix = f"{product_path}/{cycle}/{lead:03d}/"
    bases = [f"https://dd.weather.gc.ca/{day}/WXO-DD/{suffix}"]
    if day == datetime.now(timezone.utc).strftime("%Y%m%d"):
        bases.append(f"https://dd.weather.gc.ca/today/{suffix}")
    return bases


def collect_eccc(model_key: str, run: datetime, raw_dir: Path, requested: list[int],
                 process_file: Callable[[Path], None] | None = None,
                 checkpoint: Callable[[int], None] | None = None
                 ) -> tuple[list[Path], list[int], list[str]]:
    # GDPS and GEPS both publish on the MSC Datamart. Both issue runs at
    # 00Z/12Z; 06Z/18Z consumers use the latest prior run. The URL uses the
    # dated tree of the run itself, with /today/ only as a same-day fallback.
    source_run = run.replace(hour=0 if run.hour < 12 else 12)
    cycle = f"{source_run.hour:02d}"
    if model_key == "cmc_gdps":
        product_types = ("model_gdps/15km",)
    elif model_key == "cmc_geps":
        # Modo enxuto: só products (probabilidades/estatísticas do ensemble);
        # os arquivos raw de todos os membros não são coletados.
        product_types = ("ensemble/geps/grib2/products",)
    else:
        raise ValueError(f"Modelo ECCC desconhecido: {model_key}")

    downloaded: list[Path] = []
    found_hours: set[int] = set()
    completed_hours: set[int] = set()
    missing_hours: set[int] = set()
    errors: list[str] = []
    model_output = raw_dir.parent.parent / model_key
    model_output.mkdir(parents=True, exist_ok=True)
    progress_path = model_output / "collection-progress.json"

    def write_progress(status: str = "collecting") -> None:
        payload = {
            "schema": "sideral-global-nwp-progress-v1",
            "model_key": model_key,
            "run_utc": run.isoformat(),
            "status": status,
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

    write_progress()

    def download_one_lead(lead: int) -> tuple[int, list[Path], list[str]]:
        lead_files: list[Path] = []
        lead_errors: list[str] = []
        for product_path in product_types:
            links: list[str] = []
            try:
                for candidate in eccc_base_urls(product_path, source_run, cycle, lead):
                    links = list_grib_links(candidate)
                    if links:
                        break
                if not links:
                    lead_errors.append(
                        f"{product_path}/F{lead:03d}: nenhum GRIB2 publicado neste ciclo."
                    )
                    continue

                lead_dir = raw_dir / product_path.rsplit("/", 1)[-1] / f"f{lead:03d}"
                lead_dir.mkdir(parents=True, exist_ok=True)

                def download_one(url: str) -> Path | None:
                    name = url.rsplit("/", 1)[-1]
                    target = lead_dir / name
                    last_error: Exception | None = None
                    for attempt, wait in enumerate((5, 15, 30), start=1):
                        try:
                            if ensure_request(url, target, timeout=(20, 300)):
                                return target
                            return None
                        except Exception as exc:
                            last_error = exc
                            if attempt < 3:
                                time.sleep(wait)
                    raise RuntimeError(
                        f"download falhou após 3 tentativas: "
                        f"{type(last_error).__name__}: {last_error}"
                    ) from last_error

                # Variáveis/arquivos de uma validade são baixados juntos.
                with ThreadPoolExecutor(max_workers=1 if model_key == "cmc_gdps" else 4) as executor:
                    futures = {
                        executor.submit(download_one, url): url for url in links
                    }
                    for future in as_completed(futures):
                        url = futures[future]
                        name = url.rsplit("/", 1)[-1]
                        try:
                            target = future.result()
                            if target is not None:
                                lead_files.append(target)
                        except Exception as exc:
                            lead_errors.append(
                                f"{product_path}/F{lead:03d} {name}: "
                                f"{type(exc).__name__}: {exc}"
                            )
            except Exception as exc:
                lead_errors.append(
                    f"{product_path}/F{lead:03d}: {type(exc).__name__}: {exc}"
                )

        return lead, sorted(set(lead_files)), lead_errors

    completed = 0
    # GDPS/NOAA files are very large. Download only one GDPS or GFS lead
    # at a time so temporary GRIB files cannot fill the runner disk.
    with ThreadPoolExecutor(max_workers=1 if model_key == "cmc_gdps" else 2) as executor:
        future_to_lead = {
            executor.submit(download_one_lead, lead): lead for lead in requested
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
                # GRIB decoding and writes to the F-hour spool stay serial.
                for target in lead_files:
                    if process_file:
                        try:
                            process_file(target)
                        except Exception as exc:
                            errors.append(
                                f"F{lead:03d} {target.name}: processamento GRIB falhou: "
                                f"{type(exc).__name__}: {exc}"
                            )
                    else:
                        downloaded.append(target)

                # Write a complete per-validity JSON as soon as that lead's
                # downloaded fields have been processed.
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
                errors.append(f"F{lead:03d}: nenhum produto ECCC disponível.")

            completed_hours.add(lead)
            completed += 1
            write_progress()
            print(
                f"[{model_key}] progresso {completed}/{len(requested)} validade(s); "
                f"última F{lead:03d}; {len(found_hours)} com arquivos.",
                flush=True,
            )

    write_progress("download-finished")
    return sorted(set(downloaded)), sorted(found_hours), errors



def dwd_run_hour(source_run) -> int:
    reference_time = getattr(source_run, "reference_time", None)
    if reference_time is None or not hasattr(reference_time, "hour"):
        raise RuntimeError(
            "DWD retornou uma rodada sem reference_time.hour; "
            f"tipo recebido: {type(source_run).__name__}"
        )
    return int(reference_time.hour)


def regrid_icon_grib(source: Path) -> Path:
    """Regrid native triangular DWD ICON GRIB to SIDERAL's regional lat/lon grid."""
    grid_root = Path(os.environ.get("ICON_REGRID_DIR", ".cache/icon-regrid"))
    target_candidates = list(grid_root.rglob("target_grid_world_025.txt"))
    weight_candidates = list(grid_root.rglob("weights_icogl2world_025.nc"))
    if not target_candidates or not weight_candidates:
        raise RuntimeError(
            "Pesos oficiais do DWD não encontrados. "
            "Esperados target_grid_world_025.txt e weights_icogl2world_025.nc "
            f"em {grid_root}."
        )
    if shutil.which("cdo") is None:
        raise RuntimeError("CDO não está instalado para regrade da malha ICON.")

    target_grid = target_candidates[0]
    weights = weight_candidates[0]
    output = source.with_name(source.stem + ".sideral-latlon.grib2")
    # The DWD global target grid uses the normal -180..180 longitude
    # convention. Regrid the complete global field here; regionalize() below
    # clips the required SIDERAL boxes after normalizing longitudes.
    command = [
        "cdo", "-f", "grb2",
        f"remap,{target_grid},{weights}",
        str(source),
        str(output),
    ]
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "OMP_NUM_THREADS": "4"},
    )
    if result.returncode != 0:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "CDO não conseguiu regrar o GRIB nativo ICON. "
            + result.stdout[-5000:]
        )
    if not output.is_file() or output.stat().st_size < 16:
        output.unlink(missing_ok=True)
        raise RuntimeError("CDO terminou sem produzir um GRIB regional válido.")
    with output.open("rb") as stream:
        if stream.read(4) != b"GRIB":
            output.unlink(missing_ok=True)
            raise RuntimeError("A saída CDO não possui assinatura GRIB2.")
    return output

def collect_dwd(
    model_key: str,
    raw_dir: Path,
    requested: list[int],
    process_file: Callable[[Path], None] | None = None,
    checkpoint: Callable[[int], None] | None = None,
) -> tuple[list[Path], list[int], list[str]]:
    """Collect ICON/AICON in bounded forecast-step batches with checkpoints."""
    upstream = {"icon_dwd": "icon", "icon_ai": "aicon"}[model_key]
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
            parameters = list(model.parameters())
            if not parameters:
                raise RuntimeError(f"DWD não informou parâmetros para {upstream}.")
            available_runs = model.runs()
            if not available_runs:
                raise RuntimeError(f"DWD não publicou uma rodada para {upstream}.")
            source_run = available_runs[-1]
            max_step = 180 if dwd_run_hour(source_run) in (0, 12) else 120
            # ICON Global outputs hourly through F078 and every 3 hours
            # afterwards. Keep each download small enough to survive runner
            # timeouts and publish completed forecast hours incrementally.
            forecast_steps = list(range(0, 79)) + list(
                range(81, max_step + 1, 3)
            )
            # stats["_requested_hours"] shares this list, so each checkpoint
            # manifest records the complete DWD forecast schedule as it runs.
            requested[:] = forecast_steps
            step_batches = [
                forecast_steps[index:index + 6]
                for index in range(0, len(forecast_steps), 6)
            ]
            parameter_levels: list[tuple[str, object | None, str]] = []
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
                        parameter_levels.append(
                            (parameter, level_type, level_suffix)
                        )
                except Exception as exc:
                    errors.append(
                        f"{parameter}: {type(exc).__name__}: {exc}"
                    )

            print(
                f"[{model_key}] rodada DWD {source_run}; "
                f"{len(parameter_levels)} combinações parâmetro/nível; "
                f"{len(forecast_steps)} validades em {len(step_batches)} lotes.",
                flush=True,
            )
            model_output = raw_dir.parent.parent / model_key
            model_output.mkdir(parents=True, exist_ok=True)
            progress_path = model_output / "collection-progress.json"
            completed_steps: set[int] = set()

            def write_dwd_progress(status: str = "collecting") -> None:
                available_steps = []
                for lead in sorted(completed_steps):
                    if any(
                        (
                            model_output / region["id"] / "forecast"
                            / f"F{lead:03d}.json.gz"
                        ).is_file()
                        or any(
                            (
                                model_output / region["id"] / "forecast"
                            ).glob(f"F{lead:03d}-part*.json.gz")
                        )
                        for region in REGIONS
                    ):
                        available_steps.append(lead)
                payload = {
                    "schema": "sideral-global-nwp-progress-v1",
                    "model_key": model_key,
                    "run_utc": str(source_run),
                    "status": status,
                    "requested_forecast_hours": len(forecast_steps),
                    "completed_forecast_hours": sorted(completed_steps),
                    "available_forecast_hours": available_steps,
                    "missing_forecast_hours": sorted(
                        completed_steps - set(available_steps)
                    ),
                    "completed_count": len(completed_steps),
                    "errors": errors[-250:],
                    "updated_utc": datetime.now(timezone.utc).isoformat(),
                }
                temp_progress = progress_path.with_suffix(".tmp")
                temp_progress.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temp_progress.replace(progress_path)

            write_dwd_progress()

            for batch_index, step_batch in enumerate(step_batches, start=1):
                batch_label = f"f{step_batch[0]:03d}-f{step_batch[-1]:03d}"
                print(
                    f"[{model_key}] lote {batch_index}/{len(step_batches)} "
                    f"{batch_label}: iniciando.",
                    flush=True,
                )
                for parameter, level_type, level_suffix in parameter_levels:
                    safe_parameter = re.sub(
                        r"[^A-Za-z0-9_.-]+", "-", parameter
                    )
                    safe_level = re.sub(
                        r"[^A-Za-z0-9_.-]+", "-", level_suffix
                    )
                    target = (
                        raw_dir / safe_parameter / safe_level / batch_label
                        / f"{safe_parameter}-{safe_level}-{batch_label}.grib2"
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        options = {
                            "parameters": parameter,
                            "steps": [f"{step}h" for step in step_batch],
                        }
                        if level_type is not None:
                            options["level_type"] = level_type
                            options["levels"] = "all"
                        query = model.select(**options)
                        plan = query.resolve(run=source_run, require="partial")
                        if not plan.assets:
                            continue
                        if getattr(plan, "missing", None):
                            errors.append(
                                f"{parameter}/{level_suffix}/{batch_label}: "
                                f"{len(plan.missing)} combinações ausentes."
                            )
                        plan.download(str(target))
                        if not target.is_file() or target.stat().st_size == 0:
                            errors.append(
                                f"{parameter}/{level_suffix}/{batch_label}: "
                                "download não gerou GRIB2."
                            )
                            continue
                        if process_file:
                            regridded = regrid_icon_grib(target)
                            try:
                                process_file(regridded)
                            finally:
                                regridded.unlink(missing_ok=True)
                        else:
                            downloaded.append(target)
                    except Exception as exc:
                        errors.append(
                            f"{parameter}/{level_suffix}/{batch_label}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    finally:
                        if process_file:
                            target.unlink(missing_ok=True)

                if checkpoint:
                    for lead in step_batch:
                        try:
                            checkpoint(lead)
                        except Exception as exc:
                            errors.append(
                                f"F{lead:03d}: checkpoint ICON falhou: "
                                f"{type(exc).__name__}: {exc}"
                            )
                completed_steps.update(step_batch)
                write_dwd_progress()
                print(
                    f"[{model_key}] lote {batch_index}/{len(step_batches)} "
                    f"{batch_label}: concluído; {len(errors)} ocorrência(s).",
                    flush=True,
                )

            write_dwd_progress("download-finished")

        if process_file:
            return [], forecast_steps, errors
        if not downloaded:
            raise RuntimeError(
                "DWD não forneceu GRIB2 para as variáveis/níveis solicitados."
            )
        return sorted(set(downloaded)), forecast_steps, errors
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
                requested_hours: list[int], max_hour: int,
                forecast_hour_override: int | None = None) -> dict[str, dict[int, dict]]:
    from eccodes import (
        codes_grib_new_from_file, codes_get_array, codes_get_values, codes_release
    )
    spool_root.mkdir(parents=True, exist_ok=True)
    stats: dict[str, dict[int, dict]] = defaultdict(dict)
    field_inventory: dict[str, set[tuple]] = defaultdict(set)
    requested = set(requested_hours)
    errors: list[str] = []
    diagnostics = {
        "grib_files_opened": 0,
        "grib_messages_read": 0,
        "messages_with_forecast_hour": 0,
        "messages_rejected_by_hour": 0,
        "messages_without_coordinates": 0,
        "messages_outside_regions": 0,
        "messages_written_to_regions": 0,
        "coordinate_grid_cache_hits": 0,
        "coordinate_grids_cached": 0,
    }
    # Coordinate arrays and regional point indices are identical for every
    # field on the same grid. Compute them once per grid, not once per field.
    coordinate_cache: dict[tuple, dict] = {}
    coordinate_point_counts: dict[tuple, int] = {}

    for source in raw_files:
        # Do not filter by filename extension here. NOAA/Herbie GFS objects
        # are published on AWS with names ending in f000/f003/... and have no
        # .grib2 suffix. Herbie still downloads them as valid GRIB2 files.
        # Identify the payload by its GRIB magic instead of its filename.
        try:
            with source.open("rb") as probe:
                magic = probe.read(4)
            if magic == b"GRIB":
                stream = source.open("rb")
            elif magic[:2] == b"\x1f\x8b":
                stream = gzip.open(source, "rb")
                if stream.read(4) != b"GRIB":
                    stream.close()
                    errors.append(
                        f"{source.name}: arquivo gzip não contém payload GRIB2."
                    )
                    continue
                stream.seek(0)
            else:
                errors.append(
                    f"{source.name}: arquivo ignorado; assinatura inicial "
                    f"{magic!r} não é GRIB2 nem gzip de GRIB2."
                )
                continue
        except OSError as exc:
            errors.append(f"{source.name}: {exc}")
            continue

        diagnostics["grib_files_opened"] += 1
        with stream:
            while True:
                gid = codes_grib_new_from_file(stream)
                if gid is None:
                    break
                try:
                    diagnostics["grib_messages_read"] += 1
                    lead, run_utc, valid_utc = lead_from_message(gid)
                    # NOAA/Herbie stores the requested F-hour in the path itself.
                    # Some GRIB products expose non-standard time metadata; the
                    # downloaded forecast filename is the authoritative lead for
                    # these files, so do not silently discard every GFS message.
                    if forecast_hour_override is not None:
                        lead = forecast_hour_override
                    if lead is None or lead < 0 or lead > max_hour:
                        diagnostics["messages_rejected_by_hour"] += 1
                        continue
                    diagnostics["messages_with_forecast_hour"] += 1
                    if requested and lead not in requested and model_key not in {"icon_dwd", "icon_ai"}:
                        diagnostics["messages_rejected_by_hour"] += 1
                        continue
                    grid_key_names = (
                        "gridType", "numberOfDataPoints", "Ni", "Nj",
                        "latitudeOfFirstGridPointInDegrees",
                        "longitudeOfFirstGridPointInDegrees",
                        "latitudeOfLastGridPointInDegrees",
                        "longitudeOfLastGridPointInDegrees",
                        "iDirectionIncrementInDegrees",
                        "jDirectionIncrementInDegrees",
                        "iScansNegatively", "jScansPositively",
                        "jPointsAreConsecutive", "alternativeRowScanning",
                    )
                    grid_key = tuple(message_value(gid, key) for key in grid_key_names)
                    grid_regions = coordinate_cache.get(grid_key)
                    if grid_regions is None:
                        lats = np.asarray(
                            codes_get_array(gid, "latitudes"), dtype=np.float64
                        )
                        lons = np.asarray(
                            codes_get_array(gid, "longitudes"), dtype=np.float64
                        )
                        if lats.size == 0 or lats.size != lons.size:
                            diagnostics["messages_without_coordinates"] += 1
                            continue
                        lons = ((lons + 180.0) % 360.0) - 180.0
                        grid_signature = {
                            "grid_type": str(message_value(gid, "gridType", "unknown")),
                            "grid_ni": message_value(gid, "Ni"),
                            "grid_nj": message_value(gid, "Nj"),
                            "grid_points": message_value(gid, "numberOfDataPoints"),
                            "i_scans_negatively": message_value(gid, "iScansNegatively"),
                            "j_scans_positively": message_value(gid, "jScansPositively"),
                            "j_points_consecutive": message_value(gid, "jPointsAreConsecutive"),
                            "alternative_row_scanning": message_value(gid, "alternativeRowScanning"),
                        }
                        grid_regions = {}
                        for region in REGIONS:
                            west, south, east, north = [
                                float(x) for x in region["bounds"]
                            ]
                            indices = np.flatnonzero(
                                (lats >= south) & (lats <= north) &
                                (lons >= west) & (lons <= east)
                            )
                            if indices.size == 0:
                                continue
                            selected_lats = np.round(lats[indices], 4)
                            selected_lons = np.round(lons[indices], 4)
                            signature = hashlib.sha1(
                                selected_lats.tobytes()
                                + selected_lons.tobytes()
                                + json.dumps(
                                    grid_signature, sort_keys=True
                                ).encode("utf-8")
                            ).hexdigest()[:16]
                            grid_regions[region["id"]] = (
                                indices, selected_lats, selected_lons,
                                signature, grid_signature,
                            )
                        coordinate_cache[grid_key] = grid_regions
                        coordinate_point_counts[grid_key] = int(lats.size)
                        diagnostics["coordinate_grids_cached"] += 1
                    else:
                        diagnostics["coordinate_grid_cache_hits"] += 1

                    vals = np.asarray(codes_get_values(gid), dtype=np.float64)
                    expected_points = int(message_value(gid, "numberOfDataPoints", vals.size))
                    if (
                        vals.size != expected_points
                        or vals.size != coordinate_point_counts.get(grid_key, vals.size)
                    ):
                        diagnostics["messages_without_coordinates"] += 1
                        continue
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
                        cached_geometry = grid_regions.get(region["id"])
                        if cached_geometry is None:
                            continue
                        (
                            indices, selected_lats, selected_lons,
                            signature, grid_signature,
                        ) = cached_geometry
                        diagnostics["messages_written_to_regions"] += 1
                        region_id = region["id"]
                        field_dir = spool_root / region_id / f"f{lead:03d}"
                        grids_dir = field_dir / "grids"
                        grids_dir.mkdir(parents=True, exist_ok=True)
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
    stats["_diagnostics"] = diagnostics
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
        previous_manifest = {}
        if manifest_path.is_file():
            try:
                previous_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                previous_manifest = {}
        validity_by_hour = {
            int(item["forecast_hour"]): item
            for item in previous_manifest.get("validities", [])
            if "forecast_hour" in item
        }

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

            # GitHub rejects individual Git blobs above 100 MB. Split large
            # forecast frames by uncompressed NDJSON size, keeping every part
            # as a complete, independently valid gzip-compressed JSON document.
            max_part_raw_bytes = MAX_FRAME_PART_RAW_BYTES
            for stale_part in forecast_dir.glob(f"F{lead:03d}*.json.gz"):
                stale_part.unlink(missing_ok=True)

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
            }

            part_paths: list[Path] = []
            part_index = 0
            raw_bytes_in_part = 0
            fields_in_part = 0
            compressed = None

            def open_part(index: int):
                name = (
                    f"F{lead:03d}.json.gz" if index == 1
                    else f"F{lead:03d}-part{index:03d}.json.gz"
                )
                path = forecast_dir / name
                stream = gzip.open(
                    path, "wt", encoding="utf-8", compresslevel=6
                )
                part_header = {**header, "part_index": index}
                stream.write("{")
                stream.write('"metadata":')
                stream.write(json.dumps(
                    part_header, ensure_ascii=False, separators=(",", ":")
                ))
                stream.write(',"grids":')
                stream.write(json.dumps(
                    grids, ensure_ascii=False, separators=(",", ":")
                ))
                stream.write(',"fields":[')
                return path, stream

            with fields_path.open("r", encoding="utf-8") as source:
                for line in source:
                    line = line.strip()
                    if not line:
                        continue
                    encoded_size = len(line.encode("utf-8")) + 1
                    if compressed is None or (
                        fields_in_part > 0
                        and raw_bytes_in_part + encoded_size > max_part_raw_bytes
                    ):
                        if compressed is not None:
                            compressed.write("]}")
                            compressed.close()
                        part_index += 1
                        path, compressed = open_part(part_index)
                        part_paths.append(path)
                        raw_bytes_in_part = 0
                        fields_in_part = 0
                    if fields_in_part:
                        compressed.write(",")
                    compressed.write(line)
                    raw_bytes_in_part += encoded_size
                    fields_in_part += 1

            if compressed is not None:
                compressed.write("]}")
                compressed.close()

            if not part_paths:
                continue

            part_names = [path.name for path in part_paths]
            validity_by_hour[lead] = {
                "forecast_hour": lead,
                "valid_time": entry.get("valid_time"),
                "file": f"forecast/{part_names[0]}",
                "files": [f"forecast/{name}" for name in part_names],
                "part_count": len(part_names),
                "field_count": total_fields,
            }

        # Previous checkpoints stay in the manifest even after their raw NDJSON
        # spool has been deleted to reclaim disk space.
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
            "data_format": "JSON gzip independente por validade; validades grandes são divididas em FNNN.json.gz e FNNN-partNNN.json.gz, todos listados em validities[].files.",
            "collection_status": "in-progress" if only_leads is not None else "finalizing",
            "attribution": {
                "ECMWF": "Contains ECMWF data. © ECMWF. Licensed under CC-BY-4.0 where applicable."
            } if catalog_spec["provider"] == "ECMWF" else None,
        }
        write_json_atomic(manifest_path, manifest)
        # The regional gzip frames now contain the grids and all fields for
        # these forecast hours. Delete the much larger plain NDJSON spool;
        # the manifest preserves prior checkpoint entries for finalization.
        for lead in leads:
            shutil.rmtree(spool_root / region_id / f"f{int(lead):03d}", ignore_errors=True)
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
    write_json_atomic(path, {
        "schema": "sideral-global-nwp-model-status-v1",
        "model_key": model_key,
        "status": status,
        "run_utc": run.isoformat(),
        "message": message,
        "errors": (errors or [])[:250],
    })


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

    diagnostic_totals = destination.setdefault("_diagnostics", {})
    for key, value in source.get("_diagnostics", {}).items():
        diagnostic_totals[key] = diagnostic_totals.get(key, 0) + int(value)

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
            lead_match = next(
                (
                    re.fullmatch(r"f(\d{3})", part, flags=re.IGNORECASE)
                    for part in path.parts
                    if re.fullmatch(r"f(\d{3})", part, flags=re.IGNORECASE)
                ),
                None,
            )
            lead_override = int(lead_match.group(1)) if lead_match else None
            piece = regionalize(
                [path], spool_root, model_key, requested, max_hour,
                forecast_hour_override=lead_override,
            )
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
                model_key, run, raw_dir, requested, process_file, checkpoint_lead
            )
        elif spec["adapter"] in {"herbie", "nomads-filter"}:
            # collect_herbie routes the GFS key through the NOMADS filter
            # downloader; AI/ensemble models continue to use Herbie.
            _, discovered_hours, errors = collect_herbie(
                model_key, run, raw_dir, requested, process_file, checkpoint_lead
            )
        elif spec["adapter"] == "dwdopen":
            _, discovered_hours, errors = collect_dwd(
                model_key, raw_dir, requested, process_file, checkpoint_lead
            )
            if discovered_hours:
                stats["_requested_hours"] = discovered_hours
        elif spec["adapter"] == "datamart":
            if model_key not in {"cmc_gdps", "cmc_geps"}:
                raise RuntimeError("Adaptador Datamart desconhecido.")
            _, discovered_hours, errors = collect_eccc(
                model_key, run, raw_dir, requested, process_file, checkpoint_lead
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
            diagnostic = stats.get("_diagnostics", {})
            regional_errors = stats.get("_errors", {}).get("items", [])
            message = (
                "Coleta encerrada sem campos regionais convertidos. "
                f"Diagnóstico GRIB: {json.dumps(diagnostic, ensure_ascii=False)}. "
                f"Erros de leitura: {regional_errors[:10]}"
            )
            write_model_status(
                model_output / "model_status.json", model_key, "failed", run,
                message, errors + regional_errors[:50],
            )
            print(f"[{model_key}] {message}", file=sys.stderr, flush=True)
            return 0

        written = finalize_spool(spool_root, output_root, model_key, run, spec, stats)
        if not written:
            message = "Coleta encerrada, mas não foi possível gravar JSONs regionais."
            write_model_status(
                model_output / "model_status.json", model_key, "failed", run,
                message, errors,
            )
            print(f"[{model_key}] {message}", file=sys.stderr, flush=True)
            return 0
        regional_errors = stats.get("_errors", {}).get("items", [])
        all_errors = errors + regional_errors
        for item in written:
            manifest_path = output_root / model_key / item["region"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["collection_status"] = "complete" if not all_errors else "partial"
            manifest["collection_errors_count"] = len(all_errors)
            write_json_atomic(manifest_path, manifest)
        summary = {
            "run_utc": run.isoformat(),
            "processed_grib_files": len(processed_files),
            "processed_file_examples": processed_files[:30],
            "regions": written,
            "requested_forecast_hours": requested,
            "source_hours": available_hours or discovered_hours,
            "missing_forecast_hours": sorted(set(requested) - set(available_hours)),
            "regionalizer_diagnostics": stats.get("_diagnostics", {}),
            "regionalizer_errors": stats.get("_errors", {}).get("items", []),
            "download_errors": errors[:250],
        }
        write_json_atomic(model_output / "collection-summary.json", summary)
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
        failure = f"{type(exc).__name__}: {exc}"
        partial_errors = errors + [failure]
        available_hours = sorted({
            int(lead)
            for region_id, lead_map in stats.items()
            if not region_id.startswith("_")
            for lead in lead_map
        })
        if available_hours:
            try:
                # A late network/processing exception must not discard fields
                # already converted by earlier forecast hours.
                written = finalize_spool(
                    spool_root, output_root, model_key, run, spec, stats
                )
                if written:
                    for item in written:
                        manifest_path = (
                            output_root / model_key / item["region"] / "manifest.json"
                        )
                        manifest = json.loads(
                            manifest_path.read_text(encoding="utf-8")
                        )
                        manifest["collection_status"] = "partial"
                        manifest["collection_errors_count"] = len(partial_errors)
                        write_json_atomic(manifest_path, manifest)
                    summary = {
                        "run_utc": run.isoformat(),
                        "processed_grib_files": len(processed_files),
                        "processed_file_examples": processed_files[:30],
                        "regions": written,
                        "requested_forecast_hours": requested,
                        "source_hours": available_hours,
                        "missing_forecast_hours": sorted(
                            set(requested) - set(available_hours)
                        ),
                        "regionalizer_diagnostics": stats.get("_diagnostics", {}),
                        "regionalizer_errors": stats.get("_errors", {}).get("items", []),
                        "download_errors": partial_errors[:250],
                    }
                    write_json_atomic(
                        model_output / "collection-summary.json", summary
                    )
                    total_fields = sum(item["field_count"] for item in written)
                    write_model_status(
                        model_output / "model_status.json", model_key, "partial", run,
                        f"Coleta interrompida após salvar {len(available_hours)} "
                        f"validades; {len(written)} regiões e {total_fields} campos "
                        "foram preservados.",
                        partial_errors,
                    )
                    print(
                        f"[{model_key}] PARCIAL: {failure}; "
                        f"{len(available_hours)} validade(s) preservada(s).",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 0
            except Exception as finalize_exc:
                partial_errors.append(
                    f"Falha ao finalizar dados parciais: "
                    f"{type(finalize_exc).__name__}: {finalize_exc}"
                )

        write_model_status(
            model_output / "model_status.json", model_key, "failed", run,
            failure, partial_errors
        )
        print(f"[{model_key}] FALHA: {failure}", file=sys.stderr, flush=True)
        return 1
    finally:
        shutil.rmtree(spool_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
