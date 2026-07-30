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
    "source_count",
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
    "camera_received",
    "camera_decoded",
    "camera_published",
    "mailbox_overwrites",
    "frame_reuses",
    "received",
    "decoded",
    "published",
    "overwritten",
    "reused",
    "malformed",
    "reconnects",
    "render_submissions",
    "render_completions",
    "render_drops",
    "render_active_ns",
    "render_first_submit_ns",
    "render_last_completion_ns",
    "render_completion_interval_ns",
    "preview_submissions",
    "preview_completions",
    "preview_drops",
    "preview_presents",
    "encode_submissions",
    "encode_completions",
    "encode_bytes",
    "encode_drops",
    "encode_rejected_frames",
    "encode_callback_errors",
    "encode_first_submit_ns",
    "encode_last_completion_ns",
    "encode_using_hardware",
    "encode_drain_timeouts",
    "encode_codec",
    "pool_exhaustion",
    "decoded_pixel_host_copies",
    "application_owned_frame_allocations",
    "render_inflight_capacity",
    "render_inflight_in_use",
    "render_inflight_high_water",
    "render_inflight_pool_misses",
    "render_output_capacity",
    "render_output_in_use",
    "render_output_high_water",
    "render_output_pool_misses",
    "decode_surface_pool_capacity",
    "decode_surface_pool_in_use",
    "decode_surface_pool_high_water",
    "decode_surface_pool_misses",
    "decode_ticket_pool_capacity",
    "decode_ticket_pool_in_use",
    "decode_ticket_pool_high_water",
    "decode_ticket_pool_misses",
    "encode_input_capacity",
    "encode_input_in_use",
    "encode_input_high_water",
    "encode_input_pool_misses",
    "native_texture_wrappers",
    "native_command_buffers",
    "native_decode_tickets",
    "native_callback_wrappers",
    "native_wrapper_creations",
    "sources_healthy",
    "output_width",
    "output_height",
    "requested_stage",
    "requested_pacing",
    "requested_stream_count",
    "requested_preview",
    "requested_encode",
    "resolved_active_sources",
    "resolved_create_renderer",
    "resolved_synthetic_inputs",
    "resolved_preview",
    "resolved_encode",
    "resolved_graph",
    "resolved_config",
    "fingerprints_verified",
    "asset_sha256",
    "source_sha256",
    "machine",
    "rss_bytes",
    "gpu_allocated_bytes",
}

NONNEGATIVE_INTEGER_FIELDS = {
    "stream_count", "received", "decoded", "published", "overwritten",
    "reused", "malformed", "reconnects", "render_submissions",
    "render_completions", "render_drops", "render_active_ns",
    "render_first_submit_ns", "render_last_completion_ns",
    "render_completion_interval_ns", "render_inflight_capacity",
    "render_inflight_in_use", "render_inflight_high_water",
    "render_inflight_pool_misses", "render_output_capacity",
    "render_output_in_use", "render_output_high_water",
    "render_output_pool_misses", "preview_submissions",
    "preview_completions", "preview_drops", "preview_presents",
    "encode_submissions", "encode_completions", "encode_bytes",
    "encode_drops", "encode_rejected_frames", "encode_callback_errors",
    "encode_first_submit_ns", "encode_last_completion_ns",
    "encode_input_capacity", "encode_input_in_use", "encode_input_high_water",
    "encode_input_pool_misses", "encode_drain_timeouts", "pool_exhaustion",
    "decoded_pixel_host_copies", "native_texture_wrappers",
    "native_command_buffers", "native_decode_tickets",
    "native_callback_wrappers", "application_owned_frame_allocations",
    "sources_healthy", "output_width", "output_height", "source_count",
    "requested_stream_count", "resolved_active_sources", "rss_bytes",
    "gpu_allocated_bytes",
}

# One entry per lane the run actually drives, not per array slot.
PER_LANE_INTEGER_ARRAY_FIELDS = {
    "camera_received", "camera_decoded", "camera_published",
    "mailbox_overwrites", "frame_reuses", "decode_surface_pool_capacity",
    "decode_surface_pool_in_use", "decode_surface_pool_high_water",
    "decode_surface_pool_misses", "decode_ticket_pool_capacity",
    "decode_ticket_pool_in_use", "decode_ticket_pool_high_water",
    "decode_ticket_pool_misses",
}

IDENTITY_FIELDS = (
    "schema", "run_id", "backend", "build_type", "compiler", "git_sha",
    "asset_sha256", "source_sha256", "machine",
)

DEFAULT_MAX_RSS_SLOPE_BYTES_PER_MINUTE = 64 * 1024 * 1024
DEFAULT_MAX_GPU_SLOPE_BYTES_PER_MINUTE = 32 * 1024 * 1024


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


@dataclass(frozen=True)
class SoakSummary:
    interval_count: int
    post_warmup_interval_count: int
    minimum_post_warmup_fps: float
    rss_slope_bytes_per_minute: float
    gpu_slope_bytes_per_minute: float


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


def _require_nonnegative_integer(record: dict, field: str, context: str) -> int:
    value = record[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MatrixValidationError(f"{context}: {field} must be a nonnegative integer")
    return value


def _lane_count(record: dict, context: str) -> int:
    """How many entries a per-lane array must carry in this record.

    The runtime emits one entry per lane it actually drives (`resolved_active_sources`),
    not one per slot of the fixed-capacity array — a render-only cell drives none
    and reports empty arrays. Hard-coding six here silently passed only while the
    pool was the one layout; the 16-plane line then failed validation on arrays
    that were correct."""
    value = record["resolved_active_sources"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MatrixValidationError(
            f"{context}: resolved_active_sources must be a nonnegative integer")
    return value


def _require_lane_integer_array(record: dict, field: str, context: str,
                                lanes: int) -> list[int]:
    values = record[field]
    if not isinstance(values, list) or len(values) != lanes:
        raise MatrixValidationError(
            f"{context}: {field} must have one entry per active lane ({lanes})")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise MatrixValidationError(f"{context}: {field} must contain nonnegative integers")
    return values


def _validate_capacity(record: dict, capacity: str, high_water: str, context: str,
                       lanes: int) -> None:
    capacities = record[capacity]
    high_waters = record[high_water]
    if isinstance(capacities, list) or isinstance(high_waters, list):
        if (not isinstance(capacities, list) or not isinstance(high_waters, list)
                or len(capacities) != lanes or len(high_waters) != lanes):
            raise MatrixValidationError(
                f"{context}: {capacity}/{high_water} must have one entry per "
                f"active lane ({lanes})")
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
    if isinstance(record["schema"], bool) or not isinstance(record["schema"], int) or record["schema"] != 1:
        raise MatrixValidationError(f"{context}: schema must be 1")
    if not isinstance(record["backend"], str) or record["backend"] != "metal":
        raise MatrixValidationError(f"{context}: backend must be metal")
    if not isinstance(record["final"], bool):
        raise MatrixValidationError(f"{context}: final must be boolean")
    if not isinstance(record["stage"], str) or record["stage"] not in STAGES:
        raise MatrixValidationError(f"{context}: unknown stage {record['stage']!r}")
    if isinstance(record["stream_count"], bool) or not isinstance(record["stream_count"], int) or record["stream_count"] not in STREAM_COUNTS:
        raise MatrixValidationError(f"{context}: invalid stream_count")
    if not isinstance(record["pacing"], str) or record["pacing"] not in PACINGS:
        raise MatrixValidationError(f"{context}: invalid pacing")
    expected_mode = "realtime" if record["pacing"] == "paced" else "benchmark"
    if not isinstance(record["mode"], str) or record["mode"] != expected_mode:
        raise MatrixValidationError(f"{context}: mode/pacing mismatch")
    if not isinstance(record["run_id"], str) or not record["run_id"]:
        raise MatrixValidationError(f"{context}: run_id must be nonempty")
    if not isinstance(record["git_sha"], str) or HEX_40.fullmatch(record["git_sha"]) is None:
        raise MatrixValidationError(f"{context}: git_sha must be 40 hexadecimal digits")
    if not isinstance(record["build_type"], str) or not record["build_type"]:
        raise MatrixValidationError(f"{context}: build_type must be nonempty")
    if not isinstance(record["compiler"], str) or not record["compiler"]:
        raise MatrixValidationError(f"{context}: compiler must be nonempty")
    if not isinstance(record["encode_using_hardware"], bool):
        raise MatrixValidationError(f"{context}: encode_using_hardware must be boolean")
    if not isinstance(record["encode_codec"], str) or record["encode_codec"] != "hevc":
        raise MatrixValidationError(f"{context}: encode_codec must be hevc")
    for field in NONNEGATIVE_INTEGER_FIELDS:
        _require_nonnegative_integer(record, field, context)
    for field in (
        "elapsed_s", "render_fps", "preview_fps", "encode_fps",
        "gpu_render_ms_p50", "gpu_render_ms_p95",
        "snapshot_age_spread_ms_p99",
    ):
        _require_nonnegative_number(record, field, context)
    lanes = _lane_count(record, context)
    for field in ("frame_age_ms_p50", "frame_age_ms_p95", "frame_age_ms_p99"):
        values = record[field]
        if not isinstance(values, list) or len(values) != lanes:
            raise MatrixValidationError(
                f"{context}: {field} must have one entry per active lane ({lanes})")
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise MatrixValidationError(f"{context}: {field} contains an invalid value")
    for field in PER_LANE_INTEGER_ARRAY_FIELDS:
        _require_lane_integer_array(record, field, context, lanes)
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
        ("render_inflight_capacity", "render_inflight_in_use"),
        ("render_output_capacity", "render_output_high_water"),
        ("render_output_capacity", "render_output_in_use"),
        ("decode_surface_pool_capacity", "decode_surface_pool_high_water"),
        ("decode_surface_pool_capacity", "decode_surface_pool_in_use"),
        ("decode_ticket_pool_capacity", "decode_ticket_pool_high_water"),
        ("decode_ticket_pool_capacity", "decode_ticket_pool_in_use"),
        ("encode_input_capacity", "encode_input_high_water"),
        ("encode_input_capacity", "encode_input_in_use"),
    ):
        _validate_capacity(record, capacity, high_water, context, lanes)
    expected_graph = _expected_graph(record["stage"], record["stream_count"])
    graph = record["resolved_graph"]
    if not isinstance(graph, dict) or set(graph) != set(expected_graph):
        raise MatrixValidationError(f"{context}: resolved_graph has invalid fields")
    if isinstance(graph["active_sources"], bool) or not isinstance(graph["active_sources"], int):
        raise MatrixValidationError(f"{context}: resolved_graph active_sources must be integer")
    if any(not isinstance(graph[field], bool) for field in ("create_renderer", "synthetic_inputs", "preview", "encode")):
        raise MatrixValidationError(f"{context}: resolved_graph flags must be boolean")
    if graph != expected_graph:
        raise MatrixValidationError(f"{context}: resolved_graph violates stage invariant")
    resolved_config = record["resolved_config"]
    expected_config_fields = {
        "fps_num", "fps_den", "decode_surface_pool", "decode_ticket_pool",
        "render_inflight", "output_pool",
    }
    if not isinstance(resolved_config, dict) or set(resolved_config) != expected_config_fields:
        raise MatrixValidationError(f"{context}: resolved_config has invalid fields")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in resolved_config.values()):
        raise MatrixValidationError(f"{context}: resolved_config values must be positive integers")
    if resolved_config["fps_num"] != 30000 or resolved_config["fps_den"] != 1001:
        raise MatrixValidationError(f"{context}: resolved_config cadence must be 30000/1001")
    if graph["create_renderer"]:
        if resolved_config["render_inflight"] != record["render_inflight_capacity"] or resolved_config["output_pool"] != record["render_output_capacity"]:
            raise MatrixValidationError(f"{context}: resolved_config capacity mismatch")
    elif record["render_inflight_capacity"] != 0 or record["render_output_capacity"] != 0:
        raise MatrixValidationError(f"{context}: renderer capacities must be zero when renderer is absent")
    if record["output_width"] != 5002:
        raise MatrixValidationError(f"{context}: output_width must be 5002")
    if record["output_height"] != 2102:
        raise MatrixValidationError(f"{context}: output_height must be 2102")
    if record["requested_stage"] != record["stage"] or not isinstance(record["requested_stage"], str):
        raise MatrixValidationError(f"{context}: requested_stage mismatch")
    if record["requested_pacing"] != record["mode"] or not isinstance(record["requested_pacing"], str):
        raise MatrixValidationError(f"{context}: requested_pacing mismatch")
    if record["requested_stream_count"] != record["stream_count"]:
        raise MatrixValidationError(f"{context}: requested_stream_count mismatch")
    if not isinstance(record["requested_preview"], bool) or not isinstance(record["requested_encode"], bool):
        raise MatrixValidationError(f"{context}: requested preview/encode flags must be boolean")
    flat_resolved = {
        "active_sources": record["resolved_active_sources"],
        "create_renderer": record["resolved_create_renderer"],
        "synthetic_inputs": record["resolved_synthetic_inputs"],
        "preview": record["resolved_preview"],
        "encode": record["resolved_encode"],
    }
    if isinstance(flat_resolved["active_sources"], bool) or not isinstance(flat_resolved["active_sources"], int):
        raise MatrixValidationError(f"{context}: resolved_active_sources must be integer")
    if any(not isinstance(flat_resolved[field], bool) for field in ("create_renderer", "synthetic_inputs", "preview", "encode")):
        raise MatrixValidationError(f"{context}: resolved flat flags must be boolean")
    if flat_resolved != graph:
        raise MatrixValidationError(f"{context}: resolved flat fields mismatch resolved_graph")
    if not isinstance(record["fingerprints_verified"], bool):
        raise MatrixValidationError(f"{context}: fingerprints_verified must be boolean")
    if not isinstance(record["asset_sha256"], str) or HEX_64.fullmatch(record["asset_sha256"]) is None:
        raise MatrixValidationError(f"{context}: asset_sha256 must be 64 hexadecimal digits")
    # One hash per declared lane, which for a partial-stream cell is more than
    # the lanes it drives: the fingerprint identifies the inputs, not the run.
    declared = _require_nonnegative_integer(record, "source_count", context)
    source_hashes = record["source_sha256"]
    if not isinstance(source_hashes, list) or len(source_hashes) != declared or any(
        not isinstance(value, str) or HEX_64.fullmatch(value) is None for value in source_hashes
    ):
        raise MatrixValidationError(
            f"{context}: source_sha256 must carry one SHA-256 per declared "
            f"source ({declared})")
    machine = record["machine"]
    if not isinstance(machine, dict) or set(machine) != {"hostname", "os", "arch"} or any(
        not isinstance(machine[key], str) or not machine[key] for key in machine
    ):
        raise MatrixValidationError(f"{context}: machine identity is incomplete")
    wrappers = record["native_wrapper_creations"]
    wrapper_fields = {"cv_metal_texture", "metal_command_buffer", "videotoolbox_ticket"}
    if not isinstance(wrappers, dict) or set(wrappers) != wrapper_fields or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in wrappers.values()
    ):
        raise MatrixValidationError(f"{context}: native_wrapper_creations is invalid")
    if wrappers != {
        "cv_metal_texture": record["native_texture_wrappers"],
        "metal_command_buffer": record["native_command_buffers"],
        "videotoolbox_ticket": record["native_decode_tickets"],
    }:
        raise MatrixValidationError(f"{context}: native_wrapper_creations mismatch")


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
        if any(record[field] != 0 for field in (
            "render_submissions", "render_completions", "preview_submissions",
            "preview_completions", "preview_presents", "encode_submissions",
            "encode_completions",
        )):
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
    elif any(record[field] != 0 for field in (
        "encode_submissions", "encode_completions", "encode_bytes",
        "encode_drops", "encode_rejected_frames", "encode_callback_errors",
        "encode_drain_timeouts",
    )) or record["encode_using_hardware"] is not False:
        raise MatrixValidationError(f"{context}: unexpected encode work")


def _validate_uniform_identity(
    records: list[dict], *, require_release: bool, require_verified: bool
) -> None:
    baseline = records[0]
    for field in IDENTITY_FIELDS:
        if any(record[field] != baseline[field] for record in records[1:]):
            raise MatrixValidationError(f"mixed {field} identity in records")
    if require_release and baseline["build_type"] != "Release":
        raise MatrixValidationError("build_type must be Release")
    if require_verified and not all(record["fingerprints_verified"] is True for record in records):
        raise MatrixValidationError("fingerprints_verified must be true")


def validate_cell_records(
    records: list[dict], expected_cell: tuple[str, int, str] | None = None
) -> tuple[str, int, str]:
    if not records:
        raise MatrixValidationError("cell contains no records")
    for index, record in enumerate(records, 1):
        _validate_record(record, index)
    _validate_uniform_identity(records, require_release=False, require_verified=False)
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

    baseline = records[0]
    _validate_uniform_identity(
        records, require_release=publishable, require_verified=publishable
    )

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
        if any(record["malformed"] != 0 for record in records):
            raise MatrixValidationError("publishable malformed input count must be zero")
        for cell, cell_records in records_by_cell.items():
            final = next(record for record in cell_records if record["final"])
            if final["elapsed_s"] < 15.0:
                raise MatrixValidationError(f"publishable elapsed_s below 15 seconds for {cell}")
            if not any(not record["final"] for record in cell_records):
                raise MatrixValidationError(f"publishable cell {cell} has no interval telemetry")


def _linear_slope_per_minute(times_s: Sequence[float], values: Sequence[float]) -> float:
    if len(times_s) != len(values):
        raise MatrixValidationError("slope time/value sample counts differ")
    if len(values) < 2:
        return 0.0
    mean_x = sum(times_s) / len(times_s)
    mean_y = sum(values) / len(values)
    numerator = sum((time_s - mean_x) * (value - mean_y) for time_s, value in zip(times_s, values))
    denominator = sum((time_s - mean_x) ** 2 for time_s in times_s)
    return 0.0 if denominator == 0 else numerator / denominator * 60.0


def validate_soak_records(
    records: list[dict],
    *,
    warmup_seconds: int,
    min_fps: float,
    max_rss_slope_bytes_per_minute: float = DEFAULT_MAX_RSS_SLOPE_BYTES_PER_MINUTE,
    max_gpu_slope_bytes_per_minute: float = DEFAULT_MAX_GPU_SLOPE_BYTES_PER_MINUTE,
) -> SoakSummary:
    if warmup_seconds < 0:
        raise MatrixValidationError("warmup_seconds must be nonnegative")
    if not math.isfinite(min_fps) or min_fps <= 0:
        raise MatrixValidationError("min_fps must be positive")
    for name, value in (
        ("max_rss_slope_bytes_per_minute", max_rss_slope_bytes_per_minute),
        ("max_gpu_slope_bytes_per_minute", max_gpu_slope_bytes_per_minute),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise MatrixValidationError(f"{name} must be a finite nonnegative number")
    validate_cell_records(records, ("full", 6, "paced"))
    _validate_uniform_identity(records, require_release=True, require_verified=True)
    intervals = [record for record in records if not record["final"]]
    cumulative_s = 0.0
    timed_intervals: list[tuple[float, dict]] = []
    for record in intervals:
        duration_s = float(record["elapsed_s"])
        if duration_s <= 0:
            raise MatrixValidationError("soak interval elapsed_s must be positive")
        cumulative_s += duration_s
        if cumulative_s > warmup_seconds:
            timed_intervals.append((cumulative_s, record))
    if not timed_intervals:
        raise MatrixValidationError("soak has no post-warmup interval telemetry")
    times_s = [time_s for time_s, _ in timed_intervals]
    post_warmup = [record for _, record in timed_intervals]
    post_warmup_fps = [_throughput(record) for record in post_warmup]
    low_streak = 0
    for fps in post_warmup_fps:
        low_streak = low_streak + 1 if fps < min_fps else 0
        if low_streak >= 5:
            raise MatrixValidationError(
                f"sustained FPS below {min_fps:.3f} for five consecutive intervals"
            )
    rss_slope = _linear_slope_per_minute(
        times_s, [float(record["rss_bytes"]) for record in post_warmup]
    )
    gpu_slope = _linear_slope_per_minute(
        times_s, [float(record["gpu_allocated_bytes"]) for record in post_warmup]
    )
    if rss_slope > max_rss_slope_bytes_per_minute:
        raise MatrixValidationError(
            f"RSS slope {rss_slope:.3f} exceeds {max_rss_slope_bytes_per_minute:.3f} bytes/minute"
        )
    if gpu_slope > max_gpu_slope_bytes_per_minute:
        raise MatrixValidationError(
            f"GPU allocation slope {gpu_slope:.3f} exceeds {max_gpu_slope_bytes_per_minute:.3f} bytes/minute"
        )
    return SoakSummary(
        interval_count=len(intervals),
        post_warmup_interval_count=len(post_warmup),
        minimum_post_warmup_fps=min(post_warmup_fps),
        rss_slope_bytes_per_minute=rss_slope,
        gpu_slope_bytes_per_minute=gpu_slope,
    )


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
        # The array already holds exactly the driven lanes, so no slice: slicing
        # by stream_count silently dropped nothing for the pool and would drop
        # real lanes for a wider layout.
        age_values = [float(value) for value in final["frame_age_ms_p95"]]
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
    parser.add_argument("--soak-only", action="store_true")
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--min-fps", type=float, default=29.0)
    parser.add_argument("--max-rss-slope-bytes-per-minute", type=float)
    parser.add_argument("--max-gpu-slope-bytes-per-minute", type=float)
    parser.add_argument("--expected-stage", choices=STAGES)
    parser.add_argument("--expected-stream-count", type=int, choices=STREAM_COUNTS)
    parser.add_argument("--expected-pacing", choices=PACINGS)
    parser.add_argument("--expected-git-sha")
    parser.add_argument("--expected-build-type")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        records = load_records(args.results)
        if args.soak_only:
            summary = validate_soak_records(
                records,
                warmup_seconds=args.warmup_seconds,
                min_fps=args.min_fps,
                max_rss_slope_bytes_per_minute=args.max_rss_slope_bytes_per_minute,
                max_gpu_slope_bytes_per_minute=args.max_gpu_slope_bytes_per_minute,
            )
            print(json.dumps(summary.__dict__, sort_keys=True))
            return 0
        if args.cell_only:
            expected_parts = (args.expected_stage, args.expected_stream_count, args.expected_pacing)
            if any(value is None for value in expected_parts):
                raise MatrixValidationError("--cell-only requires all three --expected-* options")
            validate_cell_records(records, expected_parts)
            if (args.expected_git_sha is None) != (args.expected_build_type is None):
                raise MatrixValidationError(
                    "--expected-git-sha and --expected-build-type must be supplied together"
                )
            if args.expected_git_sha is not None:
                if HEX_40.fullmatch(args.expected_git_sha) is None:
                    raise MatrixValidationError("--expected-git-sha must be 40 hexadecimal digits")
                if any(record["git_sha"] != args.expected_git_sha for record in records):
                    raise MatrixValidationError("git_sha does not match expected executable identity")
                if any(record["build_type"] != args.expected_build_type for record in records):
                    raise MatrixValidationError("build_type does not match expected executable identity")
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
