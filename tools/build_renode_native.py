"""Build the ARM backend with native DWT reads without changing the installed Renode."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tree_hash(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if path.is_file() and ".git" not in relative.parts:
            digest.update(
                str(relative).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest()
            )
    return digest.hexdigest()


def verify(directory, hashes):
    for name, expected in hashes.items():
        path = directory / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Source does not match the pinned Renode 1.17.0 revision: {path}")


def copy_atomic(source, destination):
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_directory(installed, destination, native):
    destination.mkdir(parents=True)
    for entry in installed.iterdir():
        target = destination / entry.name
        if entry.name == "platform-lib":
            for item in entry.rglob("*"):
                link = destination / item.relative_to(installed)
                if item.is_dir():
                    link.mkdir(parents=True, exist_ok=True)
                elif not link.exists():
                    link.symlink_to(item)
        elif entry.name == "Infrastructure.dll":
            # PlatformFileLoader searches beside this assembly. A private copy
            # anchors native lookup in the generated runtime on all CLR loaders.
            copy_atomic(entry, target)
        elif not target.exists():
            target.symlink_to(entry)
    target = destination / "platform-lib/linux-x64/translate-arm-m-le.so"
    copy_atomic(native, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--renode", default=os.environ.get("PYRENODE_PATH"), type=Path)
    parser.add_argument("--source", type=Path, help="Renode's Emulator/Cores source directory")
    parser.add_argument("--output", type=Path, default=ROOT / ".renode-native")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        parser.error("This build currently supports Linux x86-64")
    if args.renode is None:
        parser.error("Set PYRENODE_PATH or pass --renode with the installed Renode directory")
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    installed = args.renode.resolve()
    source = (args.source or installed / "src/Infrastructure/src/Emulator/Cores").resolve()
    output = args.output.resolve()
    if output == installed or installed in output.parents or output in installed.parents:
        parser.error("The output directory must be separate from the installed Renode")
    if output == source or source in output.parents:
        parser.error("The output directory must be separate from the Cores source tree")
    manifest = json.loads((ROOT / "renode/sources.json").read_text())
    verify(source, manifest["original_sha256"])
    if tree_hash(source) != manifest["source_tree_sha256"]:
        raise ValueError("Cores source tree does not match the pinned Renode revision")
    for command in ("cmake", "ninja", "gcc", "patch"):
        if shutil.which(command) is None:
            parser.error(f"Missing build dependency: {command}")

    compiler = shutil.which("gcc")
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / "runtime"
    if runtime.is_symlink() or (runtime.exists() and not runtime.is_dir()):
        raise ValueError("The generated runtime must be a real directory")
    # Always build in a fresh private tree: no mutable source/build cache can
    # supply unverified files or redirect writes through descendant symlinks.
    with tempfile.TemporaryDirectory(prefix=".build-", dir=output) as temporary:
        workspace = Path(temporary)
        working, build = workspace / "source", workspace / "build"
        shutil.copytree(source, working)
        if tree_hash(working) != manifest["source_tree_sha256"]:
            raise ValueError("Copied source tree does not match the pinned revision")
        # Nix installs immutable source modes; only the private copy is writable.
        for path in [working, *working.rglob("*")]:
            path.chmod(path.stat().st_mode | 0o200)
        for patch in manifest["patches"]:
            subprocess.run(
                [
                    "patch",
                    "--batch",
                    "--forward",
                    "--no-backup-if-mismatch",
                    "-p1",
                    "-i",
                    str(ROOT / "renode/patches" / patch),
                ],
                cwd=working,
                check=True,
            )
        verify(working, manifest["patched_sha256"])
        if tree_hash(working) != manifest["patched_tree_sha256"]:
            raise ValueError("Patched source tree does not match the manifest")
        subprocess.run(
            [
                "cmake",
                "-S",
                str(working),
                "-B",
                str(build),
                "-G",
                "Ninja",
                "-DCMAKE_C_COMPILER=" + compiler,
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_C_FLAGS_RELEASE=-O2 -DNDEBUG",
                "-DTARGET_ARCH=arm-m",
                "-DTARGET_WORD_SIZE=32",
                "-DHOST_ARCH=i386",
                "-DHOST_WORD_SIZE=64",
                "-DTLIB_COMMIT_SHA=renode-ab721d88",
            ],
            check=True,
        )
        subprocess.run(["cmake", "--build", str(build), "--parallel", str(args.jobs)], check=True)
        native = build / "tlib/translate-arm-m-le.so"
        staged = workspace / "runtime"
        runtime_directory(installed, staged, native)
        metadata = {
            "renode_commit": manifest["renode_commit"],
            "installed_renode": str(installed),
            "runtime": str(runtime),
            "native_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
            "compiler_path": compiler,
            "compiler": subprocess.check_output([compiler, "--version"], text=True).splitlines()[0],
            "c_flags_release": "-O2 -DNDEBUG",
            "nix_hardening": os.environ.get("NIX_HARDENING_ENABLE"),
        }
        previous = workspace / "previous-runtime"
        if runtime.exists():
            runtime.rename(previous)
        try:
            staged.rename(runtime)
        except OSError:
            if previous.exists():
                previous.rename(runtime)
            raise
        with tempfile.NamedTemporaryFile(mode="w", dir=output, delete=False) as handle:
            handle.write(json.dumps(metadata, indent=2) + "\n")
            metadata_path = Path(handle.name)
        os.replace(metadata_path, output / "build.json")
    print(json.dumps(metadata))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"build_renode_native: {error}", file=sys.stderr)
        raise SystemExit(1)
