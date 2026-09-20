from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest



def _load_script(name: str):
    path = Path(__file__).resolve().parents[1] / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


patch_spectator_app = _load_script("patch_spectator_app")
patch_v2 = _load_script("patch_v2")


@pytest.mark.skipif(os.name == "nt", reason="Windows exposes limited POSIX mode bits")
@pytest.mark.parametrize("script", [patch_v2, patch_spectator_app])
def test_atomic_write_preserves_permissions(tmp_path, script):
    app_path = tmp_path / "app.py"
    _write(app_path, "original\n")
    os.chmod(app_path, 0o640)
    original_mode = stat.S_IMODE(app_path.stat().st_mode)

    script._atomic_write_utf8(app_path, "patched\n")

    assert stat.S_IMODE(app_path.stat().st_mode) == original_mode


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def test_patch_v2_is_idempotent_and_rejects_missing_anchor(tmp_path):
    app_path = tmp_path / "src" / "cai" / "api" / "app.py"
    replacement_path = tmp_path / "new_send_message_stream.py"
    _write(
        app_path,
        """def create_app():
    @app.post('/messages')
    async def send_message_stream(session_id: str):
        return 1

    async def sibling():
        return 2
""",
    )
    _write(
        replacement_path,
        """    async def send_message_stream(session_id: str):
        return 3
""",
    )

    assert patch_v2.main(
        ["--app-path", str(app_path), "--new-function-path", str(replacement_path)]
    ) == 0
    first = app_path.read_text(encoding="utf-8")
    assert first.count("async def send_message_stream") == 1
    assert "return 3" in first

    assert patch_v2.main(
        ["--app-path", str(app_path), "--new-function-path", str(replacement_path)]
    ) == 0
    assert app_path.read_text(encoding="utf-8") == first

    invalid_path = tmp_path / "invalid.py"
    _write(invalid_path, "def create_app():\n    return None\n")
    before = invalid_path.read_text(encoding="utf-8")
    assert patch_v2.main(
        ["--app-path", str(invalid_path), "--new-function-path", str(replacement_path)]
    ) == 1
    assert invalid_path.read_text(encoding="utf-8") == before


def test_spectator_patch_is_idempotent_and_rejects_missing_anchor(tmp_path):
    app_path = tmp_path / "src" / "cai" / "api" / "app.py"
    _write(
        app_path,
        """def create_app():
    app.include_router(qb_v2_router)

    async def send_message_stream():
        async def _gen():
            async for chunk in sse_stream_via_hooks(
                session.agent,
                composed,
            ):
                yield chunk

        return _gen()
""",
    )

    assert patch_spectator_app.main(["--app-path", str(app_path)]) == 0
    first = app_path.read_text(encoding="utf-8")
    assert first.count(patch_spectator_app.ROUTER_MARKER) == 1
    assert first.count(patch_spectator_app.HOOK_MARKER) == 1

    assert patch_spectator_app.main(["--app-path", str(app_path)]) == 0
    assert app_path.read_text(encoding="utf-8") == first

    invalid_path = tmp_path / "invalid_spectator.py"
    _write(invalid_path, "def create_app():\n    return None\n")
    before = invalid_path.read_text(encoding="utf-8")
    assert patch_spectator_app.main(["--app-path", str(invalid_path)]) == 1
    assert invalid_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("script", [patch_v2, patch_spectator_app])
def test_patch_scripts_report_missing_app(tmp_path, script):
    assert script.main(["--app-path", str(tmp_path / "missing.py")]) == 1
