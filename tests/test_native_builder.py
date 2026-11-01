"""Exercise source pinning and runtime isolation without a native toolchain."""

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The native builder is Linux-only"
)


@pytest.fixture
def builder(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "tools" / "build_renode_native.py"
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("native_builder_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo, source, output = (tmp_path / name for name in ("repo", "Cores", "output"))
    (repo / "renode" / "patches").mkdir(parents=True)
    source.mkdir()
    (source / "tracked.c").write_bytes(b"pinned tracked source")
    (source / "other.c").write_bytes(b"pinned remaining source")
    digest = hashlib.sha256((source / "tracked.c").read_bytes()).hexdigest()
    manifest = {
        "renode_commit": "test-revision",
        "original_sha256": {"tracked.c": digest},
        "patched_sha256": {"tracked.c": digest},
        "source_tree_sha256": module.tree_hash(source),
        "patched_tree_sha256": module.tree_hash(source),
        "patches": ["noop.patch"],
    }
    (repo / "renode" / "sources.json").write_text(json.dumps(manifest))
    (repo / "renode" / "patches" / "noop.patch").touch()
    module.ROOT = repo
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    state = SimpleNamespace(builds=0, corrupt_patch=False)

    def command(arguments, **options):
        if arguments[0] == "patch":
            if state.corrupt_patch:
                (Path(options["cwd"]) / "other.c").write_bytes(b"unpinned patch change")
        elif "--build" in arguments:
            state.builds += 1
            artifact = state.build / "tlib" / "translate-arm-m-le.so"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(f"compiled by {state.compiler}".encode())
        else:
            state.build = Path(arguments[arguments.index("-B") + 1])
            state.compiler = next(
                (
                    arg.split("=", 1)[1]
                    for arg in arguments
                    if arg.startswith("-DCMAKE_C_COMPILER=")
                ),
                os.environ["CC"],
            )

    def installation(name):
        installed = tmp_path / name
        native = installed / "platform-lib" / "linux-x64" / "translate-arm-m-le.so"
        native.parent.mkdir(parents=True)
        native.write_bytes(f"original native {name}".encode())
        (installed / "Infrastructure.dll").write_bytes(f"assembly {name}".encode())
        (installed / "Common.dll").write_bytes(f"support {name}".encode())
        return installed

    def run(installed):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "builder",
                "--renode",
                str(installed),
                "--source",
                str(source),
                "--output",
                str(output),
            ],
        )
        module.main()

    monkeypatch.setenv("CC", "/compilers/clang")
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/compilers/{name}")
    monkeypatch.setattr(module.subprocess, "run", command)
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda args, **kwargs: f"{args[0]} version 1.0\n",
    )
    return SimpleNamespace(
        module=module,
        source=source,
        output=output,
        state=state,
        installation=installation,
        run=run,
    )


@pytest.mark.parametrize("stage", ["copy", "patch"])
def test_unpinned_source_change_is_rejected_before_build(builder, monkeypatch, stage):
    if stage == "copy":
        copytree = builder.module.shutil.copytree

        def corrupt_copy(source, destination, **options):
            result = copytree(source, destination, **options)
            (Path(destination) / "other.c").write_bytes(b"unpinned copied change")
            return result

        monkeypatch.setattr(builder.module.shutil, "copytree", corrupt_copy)
    else:
        builder.state.corrupt_patch = True
    with pytest.raises(ValueError):
        builder.run(builder.installation("installed"))
    assert builder.state.builds == 0
    assert not (builder.output / "runtime").exists()
    assert (builder.source / "other.c").read_bytes() == b"pinned remaining source"


def test_old_runtime_symlinks_cannot_modify_installed_renode(builder):
    installed = builder.installation("installed")
    original = {
        path: path.read_bytes() for path in installed.rglob("*") if path.is_file()
    }
    runtime = builder.output / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "platform-lib").symlink_to(
        installed / "platform-lib", target_is_directory=True
    )
    builder.run(installed)
    assert all(path.read_bytes() == contents for path, contents in original.items())
    assert (
        (runtime / "platform-lib" / "linux-x64" / "translate-arm-m-le.so")
        .read_bytes()
        .startswith(b"compiled by ")
    )


def test_rebuild_refreshes_runtime_and_reports_actual_compiler(builder):
    first, second = builder.installation("first"), builder.installation("second")
    builder.run(first)
    builder.run(second)
    runtime = builder.output / "runtime"
    assert (runtime / "Common.dll").read_bytes() == b"support second"
    assert (runtime / "Infrastructure.dll").read_bytes() == b"assembly second"
    metadata = json.loads((builder.output / "build.json").read_text())
    assert (
        runtime / "platform-lib" / "linux-x64" / "translate-arm-m-le.so"
    ).read_bytes() == (f"compiled by {metadata['compiler_path']}".encode())
    assert metadata["compiler"].startswith(metadata["compiler_path"])
