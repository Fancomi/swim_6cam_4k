import os
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "scripts" / "run_bench.sh"


def bench(*arguments, **keywords):
    """Run scripts/run_bench.sh through bash, whatever the platform.

    Handing the .sh straight to CreateProcess fails on Windows with WinError 193
    ("not a valid Win32 application") — the shebang means nothing to the loader
    there. Naming the interpreter is also what the executable bit cannot do: git
    tracks mode 100755, but NTFS has no exec bit for os.access to read.
    """
    return subprocess.run(["bash", str(BENCH), *arguments], cwd=ROOT, text=True,
                          capture_output=True, **keywords)


def is_executable(path):
    """True when git records the file as mode 755.

    Not os.access(X_OK): on Windows that answers "does it exist". The mode in
    git's index is what actually ships, and it is the same answer on every
    platform."""
    entry = subprocess.check_output(
        ["git", "ls-files", "-s", "--", str(path.relative_to(ROOT).as_posix())],
        cwd=ROOT, text=True)
    return entry.split(" ", 1)[0] == "100755"


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
            "from tests.python.test_summarize_benchmarks import record_for\n"
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
        result = bench("matrix", "--list-cells", check=True)
        cells = result.stdout.splitlines()
        self.assertEqual(len(cells), 48)
        self.assertEqual(len(set(cells)), 48)
        self.assertIn("decode-only,1,paced", cells)
        self.assertIn("full,6,unpaced", cells)
        self.assertTrue(is_executable(BENCH))

    def test_soak_help_exposes_safety_defaults(self):
        result = bench("soak", "--help", check=True)
        self.assertIn("600", result.stdout)
        self.assertIn("29.0", result.stdout)
        self.assertIn("67108864", result.stdout)
        self.assertIn("33554432", result.stdout)
        self.assertTrue(is_executable(BENCH))

    def test_external_executable_is_preflighted_and_latest_stays_inside_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config, executable, log = self.make_fake_run(directory)
            output = directory / "external results with spaces"
            environment = dict(os.environ, FAKE_EXEC_LOG=str(log))
            bench("matrix", "--quick", "--config", str(config),
                  "--executable", str(executable), "--output-dir", str(output),
                  env=environment, check=True)
            self.assertEqual(len(log.read_text().splitlines()), 49)
            manifest = json.loads((output / "manifest.json").read_text())
            expected_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            self.assertEqual(manifest["git_sha"], expected_sha)
            self.assertEqual(manifest["build_type"], "Release")
            self.assertEqual(manifest["executable_sha256"], hashlib.sha256(executable.read_bytes()).hexdigest())
            # `latest` must NOT follow a scratch --output-dir: the directory is
            # about to be deleted, and a dangling symlink there breaks the
            # documented `summarize outputs/benchmarks/latest/results.jsonl`.
            latest = ROOT / "outputs" / "benchmarks" / "latest"
            if latest.exists() or latest.is_symlink():
                self.assertNotEqual(latest.resolve(), output.resolve())

    def test_mismatched_external_executable_stops_after_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config, executable, log = self.make_fake_run(directory)
            environment = dict(os.environ, FAKE_EXEC_LOG=str(log), FAKE_GIT_SHA="c" * 40)
            result = bench("matrix", "--quick", "--config", str(config),
                           "--executable", str(executable),
                           "--output-dir", str(directory / "mismatch"),
                           env=environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log.read_text().splitlines(), ["render-only"])
