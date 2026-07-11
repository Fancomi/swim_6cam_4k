"""Validate and summarize schema-1 Metal benchmark JSONL records."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence


STAGES = (
    "decode-only",
    "render-only",
    "decode-render",
    "decode-render-preview",
    "decode-render-encode",
    "full",
)
STREAM_COUNTS = (1, 2, 4, 6)
PACINGS = ("paced", "unpaced")
EXPECTED_CELLS = {
    (stage, stream_count, pacing)
    for stage in STAGES
    for stream_count in STREAM_COUNTS
    for pacing in PACINGS
}
HEX_40 = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
HEX_64 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)

REQUIRED_FIELDS = {
    "schema",
    "run_id",
    "final",
    "backend",
    "mode",
    "stage",
    "pacing",
    "build_type",
    "compiler",
    "git_sha",
    "stream_count",
    "elapsed_s",
    "render_fps",
    "preview_fps",
    "encode_fps",
    "gpu_render_ms_p50",
    "gpu_render_ms_p95",
    "frame_age_ms_p50",
    "frame_age_ms_p95",
    "frame_age_ms_p99",
    "snapshot_age_spread_ms_p99",
    "received",
    "decoded",
    "published",
    "render_submissions",
    "render_completions",
    "preview_submissions",
    "preview_completions",
    "preview_presents",
    "encode_submissions",
    "encode_completions",
    "encode_callback_errors",
    "encode_using_hardware",
    "encode_drain_timeouts",
    "decoded_pixel_host_copies",
    "application_owned_frame_allocations",
    "render_inflight_capacity",
    "render_inflight_high_water",
    "render_output_capacity",
    "render_output_high_water",
    "decode_surface_pool_capacity",
    "decode_surface_pool_high_water",
    "decode_ticket_pool_capacity",
    "decode_ticket_pool_high_water",
    "encode_input_capacity",
    "encode_input_high_water",
    "resolved_graph",
    "fingerprints_verified",
    "asset_sha256",
    "source_sha256",
    "machine",
    "rss_bytes",
    "gpu_allocated_bytes",
}


class MatrixValidationError(ValueError):
    """Raised when records cannot form a trustworthy benchmark matrix."""


@dataclass(frozen=True)
class SummaryRow:
    stage: str
    stream_count: int
    pacing: str
    throughput_fps: float
    interval_fps_p50: float
    interval_fps_p95: float
    gpu_render_ms_p50: float
    gpu_render_ms_p95: float
    frame_age_ms_p95: float
    rss_peak_bytes: int
    gpu_allocated_peak_bytes: int
    bottleneck_rank: int = 0
    incremental_fps_cost: float | None = None
    incremental_gpu_ms_p95: float | None = None


def load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise MatrixValidationError(f"cannot read {path}: {error}") from error
    if not lines:
        raise MatrixValidationError(f"{path} contains no JSONL records")
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise MatrixValidationError(f"line {line_number}: blank JSONL records are forbidden")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise MatrixValidationError(f"line {line_number}: invalid JSON: {error.msg}") from error
        if not isinstance(record, dict):
            raise MatrixValidationError(f"line {line_number}: record must be a JSON object")
        records.append(record)
    return records


def _cell(record: dict) -> tuple[str, int, str]:
    return (record["stage"], record["stream_count"], record["pacing"])


def _expected_graph(stage: str, stream_count: int) -> dict:
    return {
        "active_sources": 0 if stage == "render-only" else stream_count,
        "create_renderer": stage != "decode-only",
        "synthetic_inputs": stage == "render-only",
        "preview": stage in ("decode-render-preview", "full"),
        "encode": stage in ("decode-render-encode", "full"),
    }


def _require_nonnegative_number(record: dict, field: str, context: str) -> float:
    value = record[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise MatrixValidationError(f"{context}: {field} must be a finite nonnegative number")
    return float(value)


def _validate_capacity(record: dict, capacity: str, high_water: str, context: str) -> None:
    capacities = record[capacity]
    high_waters = record[high_water]
    if isinstance(capacities, list) or isinstance(high_waters, list):
        if not isinstance(capacities, list) or not isinstance(high_waters, list) or len(capacities) != 6 or len(high_waters) != 6:
            raise MatrixValidationError(f"{context}: {capacity}/{high_water} must be six-element arrays")
        pairs = zip(capacities, high_waters)
    else:
        pairs = ((capacities, high_waters),)
    for cap, used in pairs:
        if isinstance(cap, bool) or isinstance(used, bool) or not isinstance(cap, int) or not isinstance(used, int):
            raise MatrixValidationError(f"{context}: {capacity}/{high_water} must contain integers")
        if cap < 0 or used < 0 or used > cap:
            raise MatrixValidationError(f"{context}: {high_water} exceeds {capacity}")


def _validate_record(record: dict, index: int) -> None:
    context = f"record {index}"
    missing = sorted(REQUIRED_FIELDS - record.keys())
    if missing:
        raise MatrixValidationError(f"{context}: missing required field {missing[0]}")
    if record["schema"] != 1:
        raise MatrixValidationError(f"{context}: schema must be 1")
    if record["backend"] != "metal":
        raise MatrixValidationError(f"{context}: backend must be metal")
    if not isinstance(record["final"], bool):
        raise MatrixValidationError(f"{context}: final must be boolean")
    if record["stage"] not in STAGES:
        raise MatrixValidationError(f"{context}: unknown stage {record['stage']!r}")
    if record["stream_count"] not in STREAM_COUNTS:
        raise MatrixValidationError(f"{context}: invalid stream_count")
    if record["pacing"] not in PACINGS:
        raise MatrixValidationError(f"{context}: invalid pacing")
    expected_mode = "realtime" if record["pacing"] == "paced" else "benchmark"
    if record["mode"] != expected_mode:
        raise MatrixValidationError(f"{context}: mode/pacing mismatch")
    if not isinstance(record["run_id"], str) or not record["run_id"]:
        raise MatrixValidationError(f"{context}: run_id must be nonempty")
    if not isinstance(record["git_sha"], str) or HEX_40.fullmatch(record["git_sha"]) is None:
        raise MatrixValidationError(f"{context}: git_sha must be 40 hexadecimal digits")
    if not isinstance(record["build_type"], str) or not record["build_type"]:
        raise MatrixValidationError(f"{context}: build_type must be nonempty")
    if not isinstance(record["compiler"], str) or not record["compiler"]:
        raise MatrixValidationError(f"{context}: compiler must be nonempty")
    for field in (
        "elapsed_s", "render_fps", "preview_fps", "encode_fps",
        "gpu_render_ms_p50", "gpu_render_ms_p95", "rss_bytes",
        "gpu_allocated_bytes", "decoded_pixel_host_copies",
        "application_owned_frame_allocations", "encode_callback_errors",
        "encode_drain_timeouts",
    ):
        _require_nonnegative_number(record, field, context)
    for field in ("frame_age_ms_p50", "frame_age_ms_p95", "frame_age_ms_p99"):
        values = record[field]
        if not isinstance(values, list) or len(values) != 6:
            raise MatrixValidationError(f"{context}: {field} must be a six-element array")
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise MatrixValidationError(f"{context}: {field} contains an invalid value")
    if record["decoded_pixel_host_copies"] != 0:
        raise MatrixValidationError(f"{context}: decoded_pixel_host_copies must be zero")
    if record["application_owned_frame_allocations"] != 0:
        raise MatrixValidationError(f"{context}: application_owned_frame_allocations must be zero")
    if record["encode_callback_errors"] != 0:
        raise MatrixValidationError(f"{context}: encode_callback_errors must be zero")
    if record["encode_drain_timeouts"] != 0:
        raise MatrixValidationError(f"{context}: encode_drain_timeouts must be zero")
    for capacity, high_water in (
        ("render_inflight_capacity", "render_inflight_high_water"),
        ("render_output_capacity", "render_output_high_water"),
        ("decode_surface_pool_capacity", "decode_surface_pool_high_water"),
        ("decode_ticket_pool_capacity", "decode_ticket_pool_high_water"),
        ("encode_input_capacity", "encode_input_high_water"),
    ):
        _validate_capacity(record, capacity, high_water, context)
    expected_graph = _expected_graph(record["stage"], record["stream_count"])
    if record["resolved_graph"] != expected_graph:
        raise MatrixValidationError(f"{context}: resolved_graph violates stage invariant")
    if not isinstance(record["fingerprints_verified"], bool):
        raise MatrixValidationError(f"{context}: fingerprints_verified must be boolean")
    if not isinstance(record["asset_sha256"], str) or HEX_64.fullmatch(record["asset_sha256"]) is None:
        raise MatrixValidationError(f"{context}: asset_sha256 must be 64 hexadecimal digits")
    source_hashes = record["source_sha256"]
    if not isinstance(source_hashes, list) or len(source_hashes) != 6 or any(
        not isinstance(value, str) or HEX_64.fullmatch(value) is None for value in source_hashes
    ):
        raise MatrixValidationError(f"{context}: source_sha256 must contain six SHA-256 values")
    machine = record["machine"]
    if not isinstance(machine, dict) or any(not machine.get(key) for key in ("hostname", "os", "arch")):
        raise MatrixValidationError(f"{context}: machine identity is incomplete")


def _validate_final_stage_work(record: dict) -> None:
    context = f"final cell {_cell(record)}"
    stage = record["stage"]
    graph = record["resolved_graph"]
    for field in (
        "received", "decoded", "published", "render_submissions",
        "render_completions", "preview_submissions", "preview_completions",
        "preview_presents", "encode_submissions", "encode_completions",
    ):
        _require_nonnegative_number(record, field, context)
    if stage == "decode-only":
        if record["decoded"] <= 0:
            raise MatrixValidationError(f"{context}: decode-only performed no decode work")
        if any(record[field] != 0 for field in ("render_submissions", "preview_submissions", "encode_submissions")):
            raise MatrixValidationError(f"{context}: decode-only executed an output stage")
    elif stage == "render-only":
        if any(record[field] != 0 for field in ("received", "decoded", "published")):
            raise MatrixValidationError(f"{context}: render-only executed source work")
        if record["render_completions"] <= 0:
            raise MatrixValidationError(f"{context}: render-only performed no GPU render work")
    else:
        if record["decoded"] <= 0 or record["render_completions"] <= 0:
            raise MatrixValidationError(f"{context}: decode/render work is missing")
    if graph["preview"]:
        if record["preview_submissions"] <= 0 or record["preview_completions"] <= 0 or record["preview_presents"] <= 0:
            raise MatrixValidationError(f"{context}: preview GPU present work is missing")
    elif any(record[field] != 0 for field in ("preview_submissions", "preview_completions", "preview_presents")):
        raise MatrixValidationError(f"{context}: unexpected preview work")
    if graph["encode"]:
        if record["encode_submissions"] <= 0 or record["encode_completions"] <= 0:
            raise MatrixValidationError(f"{context}: encode work is missing")
        if record["encode_using_hardware"] is not True:
            raise MatrixValidationError(f"{context}: encode_using_hardware must be true")
    elif record["encode_submissions"] != 0 or record["encode_completions"] != 0:
        raise MatrixValidationError(f"{context}: unexpected encode work")


def validate_cell_records(
    records: list[dict], expected_cell: tuple[str, int, str] | None = None
) -> tuple[str, int, str]:
    if not records:
        raise MatrixValidationError("cell contains no records")
    for index, record in enumerate(records, 1):
        _validate_record(record, index)
    cells = {_cell(record) for record in records}
    if len(cells) != 1:
        raise MatrixValidationError(f"cell file contains mixed cells: {sorted(cells)}")
    cell = next(iter(cells))
    if expected_cell is not None and cell != expected_cell:
        raise MatrixValidationError(f"cell identity mismatch: expected {expected_cell}, got {cell}")
    finals = [record for record in records if record["final"]]
    if len(finals) != 1:
        raise MatrixValidationError(f"cell {cell} must contain exactly one final record")
    _validate_final_stage_work(finals[0])
    return cell


def validate_matrix(records: list[dict], publishable: bool) -> None:
    if not records:
        raise MatrixValidationError("matrix contains no records")
    for index, record in enumerate(records, 1):
        _validate_record(record, index)

    identity_fields = (
        "schema", "run_id", "backend", "build_type", "compiler", "git_sha",
        "asset_sha256", "source_sha256", "machine",
    )
    baseline = records[0]
    for field in identity_fields:
        if any(record[field] != baseline[field] for record in records[1:]):
            raise MatrixValidationError(f"mixed {field} identity in matrix")

    records_by_cell: dict[tuple[str, int, str], list[dict]] = {}
    for record in records:
        records_by_cell.setdefault(_cell(record), []).append(record)
    unexpected = set(records_by_cell) - EXPECTED_CELLS
    if unexpected:
        raise MatrixValidationError(f"unexpected cells: {sorted(unexpected)}")
    missing = EXPECTED_CELLS - set(records_by_cell)
    if missing:
        raise MatrixValidationError(f"missing final cells: {sorted(missing)}")
    for cell, cell_records in records_by_cell.items():
        finals = [record for record in cell_records if record["final"]]
        if len(finals) > 1:
            raise MatrixValidationError(f"duplicate final cell {cell}")
        if not finals:
            raise MatrixValidationError(f"missing final cells: {cell}")
        _validate_final_stage_work(finals[0])

    if publishable:
        if baseline["build_type"] != "Release":
            raise MatrixValidationError("publishable build_type must be Release")
        if not all(record["fingerprints_verified"] is True for record in records):
            raise MatrixValidationError("publishable fingerprints_verified must be true")
        for cell, cell_records in records_by_cell.items():
            final = next(record for record in cell_records if record["final"])
            if final["elapsed_s"] < 15.0:
                raise MatrixValidationError(f"publishable elapsed_s below 15 seconds for {cell}")
            if not any(not record["final"] for record in cell_records):
                raise MatrixValidationError(f"publishable cell {cell} has no interval telemetry")


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[index])


def _throughput(record: dict) -> float:
    stage = record["stage"]
    if stage == "decode-only":
        elapsed = float(record["elapsed_s"])
        return 0.0 if elapsed <= 0 else float(record["decoded"]) / elapsed / record["stream_count"]
    candidates = [float(record["render_fps"])]
    if record["resolved_graph"]["preview"]:
        candidates.append(float(record["preview_fps"]))
    if record["resolved_graph"]["encode"]:
        candidates.append(float(record["encode_fps"]))
    return min(candidates)


def summarize_records(records: list[dict]) -> list[SummaryRow]:
    groups: dict[tuple[str, int, str], list[dict]] = {}
    for record in records:
        groups.setdefault(_cell(record), []).append(record)
    rows: list[SummaryRow] = []
    for (stage, stream_count, pacing), cell_records in groups.items():
        finals = [record for record in cell_records if record["final"]]
        if len(finals) != 1:
            raise MatrixValidationError(f"cell {(stage, stream_count, pacing)} lacks one final record")
        final = finals[0]
        intervals = [record for record in cell_records if not record["final"]]
        interval_fps = [_throughput(record) for record in intervals]
        age_values = [float(value) for value in final["frame_age_ms_p95"][:stream_count]]
        rows.append(
            SummaryRow(
                stage=stage,
                stream_count=stream_count,
                pacing=pacing,
                throughput_fps=_throughput(final),
                interval_fps_p50=_percentile(interval_fps, 0.50),
                interval_fps_p95=_percentile(interval_fps, 0.95),
                gpu_render_ms_p50=float(final["gpu_render_ms_p50"]),
                gpu_render_ms_p95=float(final["gpu_render_ms_p95"]),
                frame_age_ms_p95=max(age_values, default=0.0),
                rss_peak_bytes=max(int(record["rss_bytes"]) for record in cell_records),
                gpu_allocated_peak_bytes=max(int(record["gpu_allocated_bytes"]) for record in cell_records),
            )
        )

    by_axis: dict[tuple[int, str], list[SummaryRow]] = {}
    for row in rows:
        by_axis.setdefault((row.stream_count, row.pacing), []).append(row)
    ranked: list[SummaryRow] = []
    for axis_rows in by_axis.values():
        ordered = sorted(axis_rows, key=lambda row: (row.throughput_fps, STAGES.index(row.stage)))
        rank = {row.stage: index + 1 for index, row in enumerate(ordered)}
        baseline = next((row for row in axis_rows if row.stage == "decode-render"), None)
        for row in axis_rows:
            fps_cost = None
            latency_cost = None
            if baseline is not None and row.stage in ("decode-render-preview", "decode-render-encode"):
                fps_cost = baseline.throughput_fps - row.throughput_fps
                latency_cost = row.gpu_render_ms_p95 - baseline.gpu_render_ms_p95
            ranked.append(
                replace(
                    row,
                    bottleneck_rank=rank[row.stage],
                    incremental_fps_cost=fps_cost,
                    incremental_gpu_ms_p95=latency_cost,
                )
            )
    return sorted(ranked, key=lambda row: (row.stream_count, PACINGS.index(row.pacing), STAGES.index(row.stage)))


def write_csv(rows: list[SummaryRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(SummaryRow.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: getattr(row, field) for field in fieldnames})


def write_markdown(rows: list[SummaryRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Metal benchmark summary",
        "",
        "Final records define headline throughput; interval records define stability percentiles.",
        "",
        "| Streams | Pacing | Stage | Throughput FPS | Interval p50/p95 | GPU p50/p95 ms | Bottleneck rank |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.stream_count} | {row.pacing} | {row.stage} | {row.throughput_fps:.3f} | "
            f"{row.interval_fps_p50:.3f}/{row.interval_fps_p95:.3f} | "
            f"{row.gpu_render_ms_p50:.3f}/{row.gpu_render_ms_p95:.3f} | {row.bottleneck_rank} |"
        )
    lines.extend(["", "## Preview and encode incremental costs", ""])
    lines.append("| Streams | Pacing | Consumer | FPS cost vs decode-render | GPU p95 cost ms |")
    lines.append("| ---: | --- | --- | ---: | ---: |")
    for row in rows:
        if row.incremental_fps_cost is not None:
            consumer = "preview" if row.stage == "decode-render-preview" else "encode"
            lines.append(
                f"| {row.stream_count} | {row.pacing} | {consumer} | "
                f"{row.incremental_fps_cost:.3f} | {row.incremental_gpu_ms_p95:.3f} |"
            )
    lines.extend(["", "## Lowest-throughput stages", ""])
    for (stream_count, pacing) in ((count, pacing) for count in STREAM_COUNTS for pacing in PACINGS):
        candidates = [row for row in rows if row.stream_count == stream_count and row.pacing == pacing]
        if candidates:
            bottleneck = min(candidates, key=lambda row: row.throughput_fps)
            lines.append(f"- {stream_count} streams, {pacing}: `{bottleneck.stage}` at {bottleneck.throughput_fps:.3f} FPS")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--publishable", action="store_true")
    parser.add_argument("--cell-only", action="store_true")
    parser.add_argument("--expected-stage", choices=STAGES)
    parser.add_argument("--expected-stream-count", type=int, choices=STREAM_COUNTS)
    parser.add_argument("--expected-pacing", choices=PACINGS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        records = load_records(args.results)
        if args.cell_only:
            expected_parts = (args.expected_stage, args.expected_stream_count, args.expected_pacing)
            if any(value is None for value in expected_parts):
                raise MatrixValidationError("--cell-only requires all three --expected-* options")
            validate_cell_records(records, expected_parts)
            return 0
        validate_matrix(records, publishable=args.publishable)
        rows = summarize_records(records)
        csv_path = args.csv or args.results.with_name("summary.csv")
        markdown_path = args.markdown or args.results.with_name("summary.md")
        write_csv(rows, csv_path)
        write_markdown(rows, markdown_path)
        print(f"validated {len(rows)} cells; CSV={csv_path}; Markdown={markdown_path}")
        return 0
    except MatrixValidationError as error:
        print(f"benchmark validation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
