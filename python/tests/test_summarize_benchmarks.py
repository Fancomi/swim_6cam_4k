import json
import tempfile
import unittest
from pathlib import Path

from python.validation.summarize_benchmarks import (
    MatrixValidationError,
    load_records,
    summarize_records,
    validate_soak_records,
    validate_cell_records,
    validate_matrix,
)


STAGES = (
    "decode-only",
    "render-only",
    "decode-render",
    "decode-render-preview",
    "decode-render-encode",
    "full",
)
COUNTS = (1, 2, 4, 6)
PACINGS = ("paced", "unpaced")


def graph_for(stage: str, stream_count: int) -> dict:
    return {
        "active_sources": 0 if stage == "render-only" else stream_count,
        "create_renderer": stage != "decode-only",
        "synthetic_inputs": stage == "render-only",
        "preview": stage in ("decode-render-preview", "full"),
        "encode": stage in ("decode-render-encode", "full"),
    }


def record_for(
    stage: str,
    stream_count: int,
    pacing: str,
    *,
    final: bool,
    elapsed_s: float = 15.0,
    throughput: float = 30.0,
) -> dict:
    graph = graph_for(stage, stream_count)
    received = 0 if stage == "render-only" else 450 * stream_count
    rendered = 0 if stage == "decode-only" else 450
    previewed = rendered if graph["preview"] else 0
    encoded = rendered if graph["encode"] else 0
    camera_received = [450 if i < graph["active_sources"] else 0 for i in range(6)]
    return {
        "schema": 1,
        "run_id": "run-fixed",
        "final": final,
        "backend": "metal",
        "mode": "realtime" if pacing == "paced" else "benchmark",
        "stage": stage,
        "pacing": pacing,
        "build_type": "Release",
        "compiler": "AppleClang 18.0.0",
        "git_sha": "a" * 40,
        "stream_count": stream_count,
        "elapsed_s": elapsed_s,
        "render_fps": throughput if rendered else 0.0,
        "preview_fps": throughput if previewed else 0.0,
        "encode_fps": throughput if encoded else 0.0,
        "gpu_render_ms_p50": 2,
        "gpu_render_ms_p95": 3,
        "frame_age_ms_p50": [1] * 6,
        "frame_age_ms_p95": [2] * 6,
        "frame_age_ms_p99": [3] * 6,
        "snapshot_age_spread_ms_p99": 1,
        "camera_received": camera_received,
        "camera_decoded": camera_received,
        "camera_published": camera_received,
        "mailbox_overwrites": [0] * 6,
        "frame_reuses": [0] * 6,
        "received": received,
        "decoded": received,
        "published": received,
        "overwritten": 0,
        "reused": 0,
        "malformed": 0,
        "reconnects": 0,
        "render_submissions": rendered,
        "render_completions": rendered,
        "render_drops": 0,
        "render_active_ns": 1_000_000 if rendered else 0,
        "render_first_submit_ns": 1 if rendered else 0,
        "render_last_completion_ns": 2 if rendered else 0,
        "render_completion_interval_ns": 1 if rendered else 0,
        "preview_submissions": previewed,
        "preview_completions": previewed,
        "preview_drops": 0,
        "preview_presents": previewed,
        "encode_submissions": encoded,
        "encode_completions": encoded,
        "encode_bytes": encoded * 100,
        "encode_drops": 0,
        "encode_rejected_frames": 0,
        "encode_callback_errors": 0,
        "encode_first_submit_ns": 1 if encoded else 0,
        "encode_last_completion_ns": 2 if encoded else 0,
        "encode_using_hardware": bool(graph["encode"]),
        "encode_drain_timeouts": 0,
        "encode_codec": "hevc",
        "pool_exhaustion": 0,
        "decoded_pixel_host_copies": 0,
        "application_owned_frame_allocations": 0,
        "render_inflight_capacity": 3 if graph["create_renderer"] else 0,
        "render_inflight_in_use": 0,
        "render_inflight_high_water": 2 if rendered else 0,
        "render_inflight_pool_misses": 0,
        "render_output_capacity": 4 if graph["create_renderer"] else 0,
        "render_output_in_use": 0,
        "render_output_high_water": 2 if rendered else 0,
        "render_output_pool_misses": 0,
        "decode_surface_pool_capacity": [8] * 6,
        "decode_surface_pool_in_use": [0] * 6,
        "decode_surface_pool_high_water": [4 if i < graph["active_sources"] else 0 for i in range(6)],
        "decode_surface_pool_misses": [0] * 6,
        "decode_ticket_pool_capacity": [16] * 6,
        "decode_ticket_pool_in_use": [0] * 6,
        "decode_ticket_pool_high_water": [4 if i < graph["active_sources"] else 0 for i in range(6)],
        "decode_ticket_pool_misses": [0] * 6,
        "encode_input_capacity": 2,
        "encode_input_in_use": 0,
        "encode_input_high_water": 2 if graph["encode"] else 0,
        "encode_input_pool_misses": 0,
        "native_texture_wrappers": 1,
        "native_command_buffers": 1,
        "native_decode_tickets": 1,
        "native_callback_wrappers": 1,
        "native_wrapper_creations": {
            "cv_metal_texture": 1,
            "metal_command_buffer": 1,
            "videotoolbox_ticket": 1,
        },
        "sources_healthy": graph["active_sources"],
        "output_width": 5002,
        "output_height": 2102,
        "requested_stage": stage,
        "requested_pacing": "realtime" if pacing == "paced" else "benchmark",
        "requested_stream_count": stream_count,
        "requested_preview": True,
        "requested_encode": True,
        "resolved_active_sources": graph["active_sources"],
        "resolved_create_renderer": graph["create_renderer"],
        "resolved_synthetic_inputs": graph["synthetic_inputs"],
        "resolved_preview": graph["preview"],
        "resolved_encode": graph["encode"],
        "resolved_graph": graph,
        "resolved_config": {
            "fps_num": 30000,
            "fps_den": 1001,
            "decode_surface_pool": 8,
            "decode_ticket_pool": 16,
            "render_inflight": 3,
            "output_pool": 4,
        },
        "fingerprints_verified": True,
        "asset_sha256": "b" * 64,
        "source_sha256": [f"{i:x}" * 64 for i in range(1, 7)],
        "machine": {"hostname": "test", "os": "macOS", "arch": "arm64"},
        "rss_bytes": 1_000_000,
        "gpu_allocated_bytes": 2_000_000,
    }


def complete_matrix(*, include_intervals: bool = True) -> list[dict]:
    records = []
    for stage in STAGES:
        for count in COUNTS:
            for pacing in PACINGS:
                if include_intervals:
                    records.extend(
                        [
                            record_for(stage, count, pacing, final=False, elapsed_s=1.0, throughput=28.0),
                            record_for(stage, count, pacing, final=False, elapsed_s=1.0, throughput=30.0),
                            record_for(stage, count, pacing, final=False, elapsed_s=1.0, throughput=32.0),
                        ]
                    )
                records.append(record_for(stage, count, pacing, final=True))
    return records


class LoadRecordsTest(unittest.TestCase):
    def test_load_records_rejects_blank_and_non_object_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            path.write_text('{}\n\n[]\n', encoding="utf-8")
            with self.assertRaisesRegex(MatrixValidationError, "line 2"):
                load_records(path)

    def test_load_records_reports_invalid_json_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            path.write_text('{"schema":1}\n{broken}\n', encoding="utf-8")
            with self.assertRaisesRegex(MatrixValidationError, "line 2"):
                load_records(path)


class ValidateMatrixTest(unittest.TestCase):
    def test_accepts_complete_identity_safe_publishable_matrix(self):
        validate_matrix(complete_matrix(), publishable=True)

    def test_rejects_missing_required_field(self):
        records = complete_matrix()
        del records[-1]["git_sha"]
        with self.assertRaisesRegex(MatrixValidationError, "git_sha"):
            validate_matrix(records, publishable=True)

    def test_rejects_schema_build_and_run_identity_mismatches(self):
        for field, value in (("schema", 2), ("git_sha", "c" * 40), ("run_id", "other"), ("build_type", "Debug")):
            with self.subTest(field=field):
                records = complete_matrix()
                records[-1][field] = value
                with self.assertRaisesRegex(MatrixValidationError, field):
                    validate_matrix(records, publishable=True)

    def test_rejects_duplicate_and_missing_final_cells(self):
        records = complete_matrix()
        records.append(dict(records[-1]))
        with self.assertRaisesRegex(MatrixValidationError, "duplicate final cell"):
            validate_matrix(records, publishable=True)

        records = complete_matrix()
        records.pop()
        with self.assertRaisesRegex(MatrixValidationError, "missing final cells"):
            validate_matrix(records, publishable=True)

    def test_rejects_host_copy_or_hot_path_allocation(self):
        for field in ("decoded_pixel_host_copies", "application_owned_frame_allocations"):
            with self.subTest(field=field):
                records = complete_matrix()
                records[-1][field] = 1
                with self.assertRaisesRegex(MatrixValidationError, field):
                    validate_matrix(records, publishable=True)

    def test_rejects_stage_graph_and_work_invariants(self):
        corruptions = (
            ("decode-only", "render_submissions", 1),
            ("render-only", "decoded", 1),
            ("decode-render-preview", "preview_presents", 0),
            ("decode-render-encode", "encode_using_hardware", False),
            ("full", "preview_completions", 0),
        )
        for stage, field, value in corruptions:
            with self.subTest(stage=stage, field=field):
                records = complete_matrix()
                target = next(r for r in records if r["final"] and r["stage"] == stage)
                target[field] = value
                with self.assertRaises(MatrixValidationError):
                    validate_matrix(records, publishable=True)

    def test_publishable_requires_release_verified_hashes_and_fifteen_seconds(self):
        for field, value in (("build_type", "Debug"), ("fingerprints_verified", False), ("elapsed_s", 14.999)):
            with self.subTest(field=field):
                records = complete_matrix()
                for record in records:
                    record[field] = value
                with self.assertRaisesRegex(MatrixValidationError, field):
                    validate_matrix(records, publishable=True)

    def test_rejects_booleans_strings_and_nonfinite_values_in_numeric_fields(self):
        corruptions = (
            ("schema", True),
            ("stream_count", True),
            ("received", 1.5),
            ("snapshot_age_spread_ms_p99", "1"),
            ("gpu_render_ms_p95", float("nan")),
            ("frame_age_ms_p95", [2, 2, 2, 2, 2, float("inf")]),
        )
        for field, value in corruptions:
            with self.subTest(field=field):
                record = record_for("full", 1, "paced", final=True)
                record[field] = value
                with self.assertRaisesRegex(MatrixValidationError, field):
                    validate_cell_records([record])

    def test_rejects_wrong_fixed_geometry_codec_cadence_and_nested_graph_types(self):
        corruptions = (
            ("output_width", 5001),
            ("output_height", 2101),
            ("encode_codec", "h264"),
            ("resolved_config", {"fps_num": 30, "fps_den": 1, "decode_surface_pool": 8, "decode_ticket_pool": 16, "render_inflight": 3, "output_pool": 4}),
            ("resolved_graph", {"active_sources": True, "create_renderer": True, "synthetic_inputs": False, "preview": True, "encode": True}),
        )
        for field, value in corruptions:
            with self.subTest(field=field):
                record = record_for("full", 1, "paced", final=True)
                record[field] = value
                with self.assertRaisesRegex(MatrixValidationError, field):
                    validate_cell_records([record])

    def test_decode_only_accepts_absent_renderer_capacity(self):
        record = record_for("decode-only", 1, "paced", final=True)
        for field in (
            "render_inflight_capacity", "render_inflight_in_use",
            "render_inflight_high_water", "render_output_capacity",
            "render_output_in_use", "render_output_high_water",
        ):
            record[field] = 0
        validate_cell_records([record])

    def test_publishable_rejects_any_malformed_input(self):
        records = complete_matrix()
        records[-1]["malformed"] = 1
        with self.assertRaisesRegex(MatrixValidationError, "malformed"):
            validate_matrix(records, publishable=True)


class SummarizeRecordsTest(unittest.TestCase):
    def test_groups_cells_and_aggregates_interval_p50_p95(self):
        records = complete_matrix()
        rows = summarize_records(records)
        self.assertEqual(len(rows), 48)
        row = next(r for r in rows if (r.stage, r.stream_count, r.pacing) == ("decode-render", 1, "paced"))
        self.assertEqual(row.interval_fps_p50, 30.0)
        self.assertEqual(row.interval_fps_p95, 32.0)
        self.assertEqual(row.throughput_fps, 30.0)

    def test_ranks_bottleneck_and_reports_preview_encode_costs(self):
        records = complete_matrix()
        finals = {
            r["stage"]: r
            for r in records
            if r["final"] and r["stream_count"] == 6 and r["pacing"] == "paced"
        }
        finals["decode-render"]["render_fps"] = 35.0
        finals["decode-render-preview"]["render_fps"] = 31.0
        finals["decode-render-preview"]["preview_fps"] = 31.0
        finals["decode-render-preview"]["gpu_render_ms_p95"] = 5
        finals["decode-render-encode"]["render_fps"] = 29.0
        finals["decode-render-encode"]["encode_fps"] = 29.0
        finals["decode-render-encode"]["gpu_render_ms_p95"] = 7
        finals["full"]["render_fps"] = 27.0
        finals["full"]["preview_fps"] = 27.0
        finals["full"]["encode_fps"] = 27.0

        rows = summarize_records(records)
        preview = next(r for r in rows if (r.stage, r.stream_count, r.pacing) == ("decode-render-preview", 6, "paced"))
        encode = next(r for r in rows if (r.stage, r.stream_count, r.pacing) == ("decode-render-encode", 6, "paced"))
        full = next(r for r in rows if (r.stage, r.stream_count, r.pacing) == ("full", 6, "paced"))
        self.assertEqual(preview.incremental_fps_cost, 4.0)
        self.assertEqual(preview.incremental_gpu_ms_p95, 2.0)
        self.assertEqual(encode.incremental_fps_cost, 6.0)
        self.assertEqual(encode.incremental_gpu_ms_p95, 4.0)
        self.assertEqual(full.bottleneck_rank, 1)


class ValidateSoakTest(unittest.TestCase):
    def test_reports_resource_slopes_and_accepts_healthy_full_soak(self):
        records = [
            record_for("full", 6, "paced", final=False, elapsed_s=1.0)
            for _ in range(40)
        ]
        for index, record in enumerate(records):
            record["rss_bytes"] += index * 1024
            record["gpu_allocated_bytes"] += index * 2048
        records.append(record_for("full", 6, "paced", final=True, elapsed_s=600.0))
        summary = validate_soak_records(records, warmup_seconds=30, min_fps=29.0)
        self.assertGreater(summary.rss_slope_bytes_per_minute, 0)
        self.assertGreater(summary.gpu_slope_bytes_per_minute, 0)

    def test_rejects_five_consecutive_post_warmup_low_fps_intervals(self):
        records = [
            record_for("full", 6, "paced", final=False, elapsed_s=1.0)
            for _ in range(35)
        ]
        for record in records[-5:]:
            record["render_fps"] = 28.0
            record["preview_fps"] = 28.0
            record["encode_fps"] = 28.0
        records.append(record_for("full", 6, "paced", final=True, elapsed_s=600.0))
        with self.assertRaisesRegex(MatrixValidationError, "sustained FPS"):
            validate_soak_records(records, warmup_seconds=30, min_fps=29.0)

    def test_uses_cumulative_interval_elapsed_time_for_slopes(self):
        durations = (0.5, 2.0, 0.5, 2.0, 1.0, 3.0)
        records = []
        cumulative = 0.0
        for duration in durations:
            cumulative += duration
            record = record_for("full", 6, "paced", final=False, elapsed_s=duration)
            record["rss_bytes"] = int(1_000_000 + cumulative * 1_000)
            record["gpu_allocated_bytes"] = int(2_000_000 + cumulative * 2_000)
            records.append(record)
        records.append(record_for("full", 6, "paced", final=True, elapsed_s=cumulative))
        summary = validate_soak_records(
            records,
            warmup_seconds=0,
            min_fps=29.0,
            max_rss_slope_bytes_per_minute=100_000,
            max_gpu_slope_bytes_per_minute=200_000,
        )
        self.assertAlmostEqual(summary.rss_slope_bytes_per_minute, 60_000, delta=1)
        self.assertAlmostEqual(summary.gpu_slope_bytes_per_minute, 120_000, delta=1)

    def test_default_limits_reject_sustained_memory_growth(self):
        records = []
        for index in range(40):
            record = record_for("full", 6, "paced", final=False, elapsed_s=1.0)
            record["rss_bytes"] += index * 2_000_000
            record["gpu_allocated_bytes"] += index * 2_000_000
            records.append(record)
        records.append(record_for("full", 6, "paced", final=True, elapsed_s=40.0))
        with self.assertRaisesRegex(MatrixValidationError, "slope"):
            validate_soak_records(records, warmup_seconds=5, min_fps=29.0)

    def test_rejects_debug_unverified_and_mixed_soak_identity(self):
        for field, value in (
            ("build_type", "Debug"),
            ("fingerprints_verified", False),
            ("run_id", "mixed-run"),
        ):
            with self.subTest(field=field):
                records = [
                    record_for("full", 6, "paced", final=False, elapsed_s=1.0)
                    for _ in range(8)
                ]
                records.append(record_for("full", 6, "paced", final=True, elapsed_s=8.0))
                records[3][field] = value
                with self.assertRaisesRegex(MatrixValidationError, field):
                    validate_soak_records(records, warmup_seconds=1, min_fps=29.0)

    def test_rejects_nonfinite_slope_limits(self):
        records = [
            record_for("full", 6, "paced", final=False, elapsed_s=1.0)
            for _ in range(8)
        ]
        records.append(record_for("full", 6, "paced", final=True, elapsed_s=8.0))
        with self.assertRaisesRegex(MatrixValidationError, "max_rss"):
            validate_soak_records(
                records,
                warmup_seconds=1,
                min_fps=29.0,
                max_rss_slope_bytes_per_minute=float("inf"),
            )


if __name__ == "__main__":
    unittest.main()
