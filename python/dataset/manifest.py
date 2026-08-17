"""Which files each generation of the calibration data consists of.

The file list is DERIVED from the line registries, never typed here: every path
comes from ``python.stitch.profiles`` or ``python.fbx_overlay.profiles``, so a new
line teaches this module about itself. What a profile cannot know — the sha256 and
size a file should have — is the manifest's job, and the manifest is generated
from a known-good tree rather than maintained by hand.

A generation is a property of the LINE, not of a directory: a rebuilt FBX of the
same cameras is a new line (``pool`` -> ``pool2``), and both revisions' files sit
side by side under ``inputs/<area>/models/``.
"""
import hashlib

from python.common.paths import INPUTS, PROJECT_ROOT
from python.fbx_overlay import profiles as overlay_profiles
from python.stitch import profiles as stitch_profiles


MANIFEST = PROJECT_ROOT / "docs" / "data-manifest.tsv"
GENERATIONS = {
    "v1": ("water_entry", "underwater", "pool", "overhead"),
    "v2": ("water_entry2", "underwater2", "pool2", "overhead2"),
}
# The FBX SDK writes .mayaSwatches/ caches beside any model it opens, and macOS
# leaves .DS_Store: both appear among the textures but belong to whoever browsed
# the directory, not to the dataset.
LOCAL_DEBRIS = (".DS_Store",)


def line_paths(line):
    """The inputs paths one line declares, exactly as its profile records them.

    A stitch line names its FBX, its texture directory, and possibly a lane
    schematic. An overlay line names one FBX per sub-camera plus the .fbm
    directory beside it, and possibly an explicit base image.
    """
    profile = stitch_profiles.PROFILES.get(line)
    if profile is not None:
        paths = {profile.fbx, profile.tex_dir}
        if profile.label_line is not None:
            paths.add(profile.label_line)
        return paths
    profile = overlay_profiles.get(line)
    paths = set()
    for camera in profile.cameras:
        paths.update(camera.fbxs)
        paths.update(fbx.with_suffix(".fbm") for fbx in camera.fbxs)
        if camera.base_image_path is not None:
            paths.add(camera.base_image_path)
    return paths


def line_files(line):
    """Every file of `line` present on disk, texture directories expanded."""
    files = set()
    for path in line_paths(line):
        if path.is_dir():
            files |= {child for child in path.rglob("*")
                      if child.is_file() and child.name not in LOCAL_DEBRIS
                      and ".mayaSwatches" not in child.parts}
        elif path.is_file():
            files.add(path)
    return files


def generation_lines(generation=None):
    """[(generation, line)] for one generation or, by default, all of them."""
    names = GENERATIONS if generation is None else {generation: GENERATIONS[generation]}
    return [(gen, line) for gen, lines in names.items() for line in lines]


def digest(path):
    """sha256 of `path`, read in 1MB blocks so a 62MB FBX never lands in memory."""
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def scan(generation=None):
    """[(gen, line, sha256, size, relative_path)] for what is on disk now.

    Sorted, and deduplicated by (line, path) so a file two lines share — the lane
    schematic belongs to both overhead generations — is recorded once per line.
    """
    rows = []
    for gen, line in generation_lines(generation):
        for path in sorted(line_files(line)):
            rows.append((gen, line, digest(path), path.stat().st_size,
                         path.relative_to(INPUTS).as_posix()))
    return rows


def load(path=MANIFEST):
    """Manifest rows as [(gen, line, sha256, size, relative_path)].

    Comment lines (``#``) carry the header and the regeneration note; blank lines
    are tolerated so the file stays editable by hand in a pinch.
    """
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        gen, line, sha, size, relative = raw.split("\t")
        rows.append((gen, line, sha, int(size), relative))
    return rows


HEADER = (
    "# 标定输入清单。inputs/ 不在 git 里（见 docs/DATA.md），这份表是搬运后的验收依据。\n"
    "# 路径由 profiles 导出，不手写：加一条线后跑 scripts/check_inputs.sh --write 重新生成。\n"
    "# gen\tline\tsha256\tbytes\tpath（相对 inputs/）\n"
)


def dump(rows):
    """The manifest file's exact text for `rows`."""
    return HEADER + "".join("\t".join(str(field) for field in row) + "\n"
                            for row in rows)
