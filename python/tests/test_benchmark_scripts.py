import os
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class BenchmarkScriptsTest(unittest.TestCase):
    def make_fake_run(self, directory: Path) -> tuple[Path, Path, Path]:
        inputs = directory / "inputs"
        inputs.mkdir()
        asset = inputs / "asset.swasset"
        asset.write_bytes(b"asset")
        cameras = ("cam3", "cam2", "cam1", "cam4", "cam5", "cam6")
        sources = []
        for camera in cameras:
            source = inputs / f"{camera}.mp4"
            source.write_bytes(camera.encode())
            sources.append(source)
        config = directory / "runtime.conf"
        config.write_text(
            "asset=" + str(asset) + "\n" + "".join(
                f"source.{camera}={source}\n" for camera, source in zip(cameras, sources)
            ),
            encoding="utf-8",
        )
        log = directory / "invocations.log"
        executable = directory / "fake swim realtime.py"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from python.tests.test_summarize_benchmarks import record_for\n"
            "args = sys.argv[1:]\n"
            "opts = {a.split('=', 1)[0]: a.split('=', 1)[1] for a in args if a.startswith('--') and '=' in a}\n"
            "manifest = {}\n"
            "for line in Path(opts['--benchmark-manifest']).read_text().splitlines():\n"
            "    key, value = line.split('=', 1); manifest[key] = value\n"
            "stage = opts['--stage']; count = int(opts['--stream-count'])\n"
            "pacing = 'paced' if opts['--mode'] == 'realtime' else 'unpaced'\n"
            "record = record_for(stage, count, pacing, final=True, elapsed_s=float(opts['--duration-seconds']))\n"
            "record['run_id'] = manifest['run_id']; record['asset_sha256'] = manifest['asset_sha256']\n"
            "record['source_sha256'] = [manifest[f'source.{camera}_sha256'] for camera in ('cam3','cam2','cam1','cam4','cam5','cam6')]\n"
            "record['git_sha'] = os.environ.get('FAKE_GIT_SHA') or subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()\n"
            "record['build_type'] = os.environ.get('FAKE_BUILD_TYPE', 'Release')\n"
            "Path(opts['--metrics']).write_text(json.dumps(record) + '\\n')\n"
            "with open(os.environ['FAKE_EXEC_LOG'], 'a') as output: output.write(stage + '\\n')\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return config, executable, log

    def test_matrix_runner_lists_each_required_cell_once(self):
        script = ROOT / "scripts" / "run_metal_benchmarks.sh"
        result = subprocess.run(
            [str(script), "--list-cells"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        cells = result.stdout.splitlines()
        self.assertEqual(len(cells), 48)
        self.assertEqual(len(set(cells)), 48)
        self.assertIn("decode-only,1,paced", cells)
        self.assertIn("full,6,unpaced", cells)
        self.assertTrue(os.access(script, os.X_OK))

    def test_soak_help_exposes_safety_defaults(self):
        script = ROOT / "scripts" / "run_metal_soak.sh"
        result = subprocess.run(
            [str(script), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("600", result.stdout)
        self.assertIn("29.0", result.stdout)
        self.assertIn("67108864", result.stdout)
        self.assertIn("33554432", result.stdout)
        self.assertTrue(os.access(script, os.X_OK))

    def test_external_executable_is_preflighted_and_latest_targets_custom_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config, executable, log = self.make_fake_run(directory)
            output = directory / "external results with spaces"
            environment = dict(os.environ, FAKE_EXEC_LOG=str(log))
            subprocess.run(
                [
                    str(ROOT / "scripts" / "run_metal_benchmarks.sh"),
                    "--quick", "--config", str(config), "--executable", str(executable),
                    "--output-dir", str(output),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(len(log.read_text().splitlines()), 49)
            manifest = json.loads((output / "manifest.json").read_text())
            expected_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            self.assertEqual(manifest["git_sha"], expected_sha)
            self.assertEqual(manifest["build_type"], "Release")
            self.assertEqual(manifest["executable_sha256"], hashlib.sha256(executable.read_bytes()).hexdigest())
            self.assertEqual((ROOT / "benchmarks" / "latest").resolve(), output.resolve())

    def test_mismatched_external_executable_stops_after_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config, executable, log = self.make_fake_run(directory)
            environment = dict(os.environ, FAKE_EXEC_LOG=str(log), FAKE_GIT_SHA="c" * 40)
            result = subprocess.run(
                [
                    str(ROOT / "scripts" / "run_metal_benchmarks.sh"),
                    "--quick", "--config", str(config), "--executable", str(executable),
                    "--output-dir", str(directory / "mismatch"),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log.read_text().splitlines(), ["render-only"])
