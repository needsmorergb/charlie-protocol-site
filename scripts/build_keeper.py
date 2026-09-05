"""Assemble the production keeper folder from the indexer package.

    python scripts/build_keeper.py            # write ./charlie-keeper
    python scripts/build_keeper.py --check    # exit 1 if ./charlie-keeper has drifted from the source

The folder is a BUILD OUTPUT: the `indexer` package copied verbatim (so the
keeper runs against exactly the code the tests ran against), the operator
files from `scripts/keeper_template/`, and a `MANIFEST.json` naming every
file, its sha256 and the commit it came from. Nothing in the folder is
hand-written there; edit the source and rebuild. `--check` is what a test
runs so a committed copy cannot quietly fall behind.

Standard library only, like the thing it packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "keeper_template"
DEFAULT_OUT = ROOT / "charlie-keeper"
MANIFEST = "MANIFEST.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_files() -> dict[str, Path]:
    """Relative destination path -> source path."""
    files: dict[str, Path] = {}
    for src in sorted((ROOT / "indexer").glob("*.py")):
        files[f"indexer/{src.name}"] = src
    for src in sorted(TEMPLATE.iterdir()):
        if src.is_file():
            files[src.name] = src
    return files


def _commit() -> dict:
    """The commit the source files came from, and whether any of them
    differed from it when the build ran. A manifest naming a commit whose
    files are not the ones copied would be the drift this script exists to
    prevent, so the flag is recorded rather than the commit being trusted."""
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        paths = [str(p.relative_to(ROOT)) for p in _source_files().values()]
        status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", *paths], capture_output=True, text=True, check=True).stdout
        return {"commit": sha or None, "source_dirty": bool(status.strip())}
    except Exception:  # noqa: BLE001
        return {"commit": None, "source_dirty": None}


def build(out: Path) -> dict:
    if out.exists():
        shutil.rmtree(out)
    (out / "indexer").mkdir(parents=True)
    hashes = {}
    for rel, src in _source_files().items():
        dst = out / rel
        shutil.copyfile(src, dst)
        hashes[rel] = _sha256(dst)
    manifest = {
        "package": "charlie-keeper",
        "built_from": {"repository": "needsmorergb/charlie-protocol-site", **_commit()},
        "built_at": int(time.time()),
        "python": ">=3.11, standard library only",
        "files": hashes,
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def check(out: Path) -> list[str]:
    """Every way the committed folder can differ from what a build would
    produce now: a source file changed, a file added, a file removed, a
    manifest hash that does not match the file beside it."""
    problems = []
    if not out.exists():
        return [f"{out} does not exist; run the build"]
    expected = {rel: _sha256(src) for rel, src in _source_files().items()}
    manifest_path = out / MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"files": {}}
    for rel, digest in expected.items():
        dst = out / rel
        if not dst.exists():
            problems.append(f"missing: {rel}")
        elif _sha256(dst) != digest:
            problems.append(f"stale: {rel} differs from its source")
        if manifest["files"].get(rel) != digest:
            problems.append(f"manifest: {rel} hash is not the source's")
    present = {str(p.relative_to(out)).replace("\\", "/") for p in out.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    for extra in sorted(present - set(expected) - {MANIFEST}):
        problems.append(f"unexpected file in the build output: {extra}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.check:
        problems = check(out)
        for line in problems:
            print(line)
        print("up to date" if not problems else f"{len(problems)} problem(s): rebuild with `python scripts/build_keeper.py`")
        return 1 if problems else 0
    manifest = build(out)
    origin = manifest["built_from"]
    note = " (source files differ from that commit)" if origin.get("source_dirty") else ""
    print(f"built {out} from {origin.get('commit') or 'an unknown commit'}{note}: {len(manifest['files'])} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
