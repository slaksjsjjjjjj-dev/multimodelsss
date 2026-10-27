from __future__ import annotations

__all__ = ["generate_output", "parse_hours"]

import json
from pathlib import Path

TRANSPARENT_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x02\x00\x01\xe5\x27\xd8\xcf"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def parse_hours(raw_hours: str | None) -> list[int]:
    if not raw_hours:
        return [0, 6, 12, 18, 24]
    values = []
    for item in raw_hours.split(","):
        text = (item or "").strip()
        if text:
            values.append(int(text))
    return values or [0, 6, 12, 18, 24]


def generate_output(output_dir: str | Path, hours: list[int] | None = None, run_name: str = "run") -> Path:
    base_dir = Path(output_dir)
    run_dir = base_dir / run_name
    forecast_dir = run_dir / "forecast"
    forecast_dir.mkdir(parents=True, exist_ok=True)

    selected_hours = hours or [0, 6, 12, 18, 24]
    manifest = {
        "runName": run_name,
        "productForecastHours": [str(hour) for hour in selected_hours],
        "forecastProducts": [],
    }

    for lead in selected_hours:
        json_path = forecast_dir / f"f{lead:03d}.json"
        png_path = forecast_dir / f"f{lead:03d}.png"

        json_path.write_text(
            json.dumps({
                "forecastHour": lead,
                "runName": run_name,
                "status": "ok",
                "model": "stub"
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        png_path.write_bytes(TRANSPARENT_PNG)

        manifest["forecastProducts"].append({
            "forecastHour": str(lead),
            "png": str(png_path),
            "json": str(json_path),
        })

    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_dir
