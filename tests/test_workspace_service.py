"""Tests for Phase 7.2: WorkspaceService and workspace IPC handlers.

Covers:
- Workspace status, list, and open (with AgentLoop sync)
- File tree indexing with ignore rules and symlink hardening
- Path restriction / sandbox escape
- Text preview with binary sniff and large-file protection
- Pin/unpin files (metadata in MiQi data root, not project root)
- Recent file tracking
- Empty workspace
- IPC handler dispatch
"""

from __future__ import annotations

import os

import pytest

from miqi.config.schema import Config
from miqi.ipc.protocol import JsonRpcRequest
from miqi.providers.base import LLMProvider, LLMResponse
from miqi.runtime.factory import Runtime, create_runtime, wire_cron_callback
from miqi.runtime.workspace_service import WorkspaceService, _meta_dir_for_root
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────────────

class FakeProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test-key")

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, *, on_delta=None):
        return LLMResponse(content="fake")

    def get_default_model(self) -> str:
        return "fake-model"


def _make_fake_provider(config: Config) -> FakeProvider:
    return FakeProvider()


def _make_runtime(tmp_path: Path, monkeypatch, *, restrict: bool = False) -> Runtime:
    monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MIQI_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("MIQI_DATA_DIR", str(tmp_path / "data"))
    if restrict:
        monkeypatch.setenv("MIQI_TOOLS__RESTRICT_TO_WORKSPACE", "true")
    config = Config()
    rt = create_runtime(config, make_provider=_make_fake_provider, init_session_manager=True)
    wire_cron_callback(rt)
    return rt


def _make_svc(tmp_path: Path, *, restrict: bool = True, agent=None) -> WorkspaceService:
    return WorkspaceService(tmp_path, agent=agent, restrict_to_workspace=restrict)


def _populate_workspace(root: Path) -> None:
    """Create a small file tree for testing."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
    (root / "src" / "utils.py").write_text("def add(a, b): return a + b", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text("def test_hello(): pass", encoding="utf-8")
    (root / "README.md").write_text("# Test Project", encoding="utf-8")
    # Ignored directories
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00" * 100)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "foo.js").write_text("module.exports = {};", encoding="utf-8")
    # Binary file
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    # Hidden config
    (root / ".env").write_text("SECRET=abc123", encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# WorkspaceService unit tests
# ══════════════════════════════════════════════════════════════════════════

class TestWorkspaceStatus:

    def test_status_returns_project_root(self, tmp_path: Path):
        svc = _make_svc(tmp_path)
        result = svc.status()
        assert result["project_root"] == str(tmp_path.resolve())
        assert result["exists"] is True

    def test_status_nonexistent_root(self, tmp_path: Path):
        nonexistent = tmp_path / "nope"
        svc = WorkspaceService(nonexistent)
        result = svc.status()
        assert result["exists"] is False


class TestWorkspaceList:

    def test_list_returns_current_workspace(self, tmp_path: Path):
        svc = _make_svc(tmp_path)
        result = svc.list_workspaces()
        assert result["count"] >= 1
        current = [w for w in result["workspaces"] if w["is_current"]]
        assert len(current) == 1
        assert current[0]["path"] == str(tmp_path.resolve())

    def test_list_includes_history_after_open(self, tmp_path: Path):
        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        ws1.mkdir()
        ws2.mkdir()
        svc = _make_svc(ws1)
        svc.open_workspace(str(ws2))
        result = svc.list_workspaces()
        paths = {w["path"] for w in result["workspaces"]}
        assert str(ws1.resolve()) in paths
        assert str(ws2.resolve()) in paths


class TestWorkspaceOpen:

    def test_open_existing_dir(self, tmp_path: Path):
        new_dir = tmp_path / "new_ws"
        new_dir.mkdir()
        svc = _make_svc(tmp_path)
        result = svc.open_workspace(str(new_dir))
        assert result["project_root"] == str(new_dir.resolve())

    def test_open_nonexistent_raises(self, tmp_path: Path):
        svc = _make_svc(tmp_path)
        with pytest.raises(ValueError, match="does not exist"):
            svc.open_workspace(str(tmp_path / "nope"))

    @pytest.mark.asyncio
    async def test_open_syncs_agent_runtime(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        new_ws = tmp_path / "new_workspace"
        new_ws.mkdir()
        (new_ws / "hello.txt").write_text("hello from new workspace", encoding="utf-8")

        # Open the new workspace via the service
        rt.workspace_service.open_workspace(str(new_ws))

        # AgentLoop should now have the new workspace
        assert str(rt.agent.workspace) == str(new_ws.resolve())

        # File tools should read from the new root
        read_tool = rt.agent.tools.get("read_file")
        assert read_tool is not None
        result = await read_tool.execute(path="hello.txt")
        assert "hello from new workspace" in result

    @pytest.mark.asyncio
    async def test_open_old_root_files_not_current(self, tmp_path: Path, monkeypatch):
        (tmp_path / "old_file.txt").write_text("old workspace file", encoding="utf-8")
        rt = _make_runtime(tmp_path, monkeypatch)
        new_ws = tmp_path / "new_workspace"
        new_ws.mkdir()
        (new_ws / "new_file.txt").write_text("new workspace file", encoding="utf-8")

        rt.workspace_service.open_workspace(str(new_ws))

        # Agent workspace is now new_ws
        assert str(rt.agent.workspace) == str(new_ws.resolve())
        # File tools with relative paths should resolve to new_ws
        read_tool = rt.agent.tools.get("read_file")
        result = await read_tool.execute(path="new_file.txt")
        assert "new workspace file" in result


class TestWorkspaceIndex:

    def test_index_empty_workspace(self, tmp_path: Path):
        svc = _make_svc(tmp_path)
        result = svc.index()
        assert result["count"] == 0
        assert result["entries"] == []

    def test_index_returns_files_and_dirs(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.index()
        names = {e["name"] for e in result["entries"]}
        assert "src" in names
        assert "tests" in names
        assert "README.md" in names
        assert "image.png" in names
        assert ".env" in names

    def test_index_ignores_patterns(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.index()
        names = {e["name"] for e in result["entries"]}
        assert ".git" not in names
        assert "__pycache__" not in names
        assert "node_modules" not in names

    def test_index_subdir(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.index(subdir="src")
        names = {e["name"] for e in result["entries"]}
        assert "main.py" in names
        assert "utils.py" in names
        assert "README.md" not in names

    def test_index_entry_shape(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.index()
        for entry in result["entries"]:
            assert "name" in entry
            assert "path" in entry
            assert "is_dir" in entry
            assert "is_symlink" in entry
            assert "size" in entry
            assert "modified" in entry

    def test_index_depth_limit(self, tmp_path: Path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "file.txt").write_text("deep", encoding="utf-8")
        svc = _make_svc(tmp_path)
        result = svc.index(depth=1)
        assert result["count"] == 1
        assert result["entries"][0]["name"] == "a"

    def test_index_max_entries(self, tmp_path: Path):
        for i in range(50):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"file {i}", encoding="utf-8")
        svc = _make_svc(tmp_path)
        result = svc.index()
        assert result["count"] == 50

    def test_index_subdir_invalid_raises(self, tmp_path: Path):
        svc = _make_svc(tmp_path)
        with pytest.raises(ValueError, match="not a directory"):
            svc.index(subdir="nonexistent")

    def test_index_skips_symlinks_in_restricted_mode(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        target = tmp_path / "outside_target"
        target.mkdir()
        (target / "secret.txt").write_text("secret", encoding="utf-8")
        try:
            (tmp_path / "link_to_outside").symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform or user")

        svc = _make_svc(tmp_path, restrict=True)
        result = svc.index()
        names = {e["name"] for e in result["entries"]}
        # Symlink should be entirely skipped in restricted mode
        assert "link_to_outside" not in names

    def test_index_marks_symlinks_in_non_restricted_mode(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        target = tmp_path / "link_target"
        target.mkdir()
        (target / "file.txt").write_text("hi", encoding="utf-8")
        try:
            (tmp_path / "mylink").symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform or user")

        svc = _make_svc(tmp_path, restrict=False)
        result = svc.index()
        link_entries = [e for e in result["entries"] if e["name"] == "mylink"]
        assert len(link_entries) == 1
        assert link_entries[0]["is_symlink"] is True
        # Symlink dir should NOT be recursed into
        assert link_entries[0]["is_dir"] is False


class TestPathRestriction:

    def test_path_escape_blocked(self, tmp_path: Path):
        svc = _make_svc(tmp_path, restrict=True)
        with pytest.raises(PermissionError):
            svc._resolve_path("../../etc/passwd")

    def test_absolute_path_outside_blocked(self, tmp_path: Path):
        svc = _make_svc(tmp_path, restrict=True)
        outside = tmp_path.parent / "definitely_outside_ws"
        with pytest.raises(PermissionError):
            svc._resolve_path(str(outside))

    def test_path_inside_workspace_allowed(self, tmp_path: Path):
        svc = _make_svc(tmp_path, restrict=True)
        (tmp_path / "src").mkdir()
        resolved = svc._resolve_path("src/main.py")
        assert str(resolved).startswith(str(tmp_path.resolve()))

    def test_no_restriction_allows_outside(self, tmp_path: Path):
        svc = _make_svc(tmp_path, restrict=False)
        outside = tmp_path.parent / "outside_test_dir"
        outside.mkdir(exist_ok=True)
        resolved = svc._resolve_path(str(outside))
        assert resolved == outside.resolve()


class TestPreview:

    def test_preview_text_file(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.read_preview("README.md")
        assert result["exists"] is True
        assert result["is_binary"] is False
        assert result["content"] == "# Test Project"
        assert result["truncated"] is False

    def test_preview_binary_file(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.read_preview("image.png")
        assert result["exists"] is True
        assert result["is_binary"] is True
        assert result["content"] is None

    def test_preview_nonexistent_file(self, tmp_path: Path):
        svc = _make_svc(tmp_path)
        result = svc.read_preview("nope.txt")
        assert result["exists"] is False

    def test_preview_directory(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.read_preview("src")
        assert result["exists"] is True
        assert result["is_dir"] is True

    def test_preview_large_file_truncated(self, tmp_path: Path):
        big = tmp_path / "big.txt"
        big.write_text("x" * (5000), encoding="utf-8")
        svc = _make_svc(tmp_path)
        result = svc.read_preview("big.txt")
        assert result["truncated"] is True
        assert len(result["content"]) == 4096

    def test_preview_path_escape_blocked(self, tmp_path: Path):
        svc = _make_svc(tmp_path, restrict=True)
        with pytest.raises(PermissionError):
            svc.read_preview("../../etc/passwd")

    def test_preview_code_file(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.read_preview("src/main.py")
        assert result["exists"] is True
        assert "print" in result["content"]

    def test_preview_binary_sniff_nul_bytes(self, tmp_path: Path):
        """File with no binary extension but containing NUL bytes should be detected."""
        binish = tmp_path / "data.bin"
        # 50% NUL bytes — well above the 30% threshold
        binish.write_bytes(b"\x00" * 400 + b"A" * 400)
        svc = _make_svc(tmp_path)
        result = svc.read_preview("data.bin")
        assert result["is_binary"] is True
        assert result["content"] is None

    def test_preview_binary_sniff_control_bytes(self, tmp_path: Path):
        """File with high control-byte ratio should be detected as binary."""
        ctrl = tmp_path / "mystery"
        # Lots of control bytes (0x01-0x08)
        ctrl.write_bytes(bytes(range(1, 9)) * 500 + b" " * 500)
        svc = _make_svc(tmp_path)
        result = svc.read_preview("mystery")
        assert result["is_binary"] is True
        assert result["content"] is None

    def test_preview_text_without_extension_not_flagged(self, tmp_path: Path):
        """Normal text file without extension should not be flagged as binary."""
        textfile = tmp_path / "Makefile"
        textfile.write_text("all:\n\techo hello\n", encoding="utf-8")
        svc = _make_svc(tmp_path)
        result = svc.read_preview("Makefile")
        assert result["is_binary"] is False
        assert result["content"] is not None


class TestPinnedFiles:

    def test_pin_file(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.pin_file("README.md")
        assert result["pinned"] is True
        assert result["path"] == "README.md"

    def test_pin_file_idempotent(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        svc.pin_file("README.md")
        svc.pin_file("README.md")
        pinned = svc.list_pinned()
        paths = [f["path"] for f in pinned["files"]]
        assert paths.count("README.md") == 1

    def test_unpin_file(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        svc.pin_file("README.md")
        result = svc.unpin_file("README.md")
        assert result["pinned"] is False
        pinned = svc.list_pinned()
        assert pinned["count"] == 0

    def test_unpin_not_pinned_is_noop(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.unpin_file("README.md")
        assert result["pinned"] is False

    def test_pin_nonexistent_raises(self, tmp_path: Path):
        svc = _make_svc(tmp_path)
        with pytest.raises(ValueError, match="does not exist"):
            svc.pin_file("nope.txt")

    def test_list_pinned(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        svc.pin_file("README.md")
        svc.pin_file("src/main.py")
        result = svc.list_pinned()
        assert result["count"] == 2
        paths = {f["path"] for f in result["files"]}
        assert "README.md" in paths
        assert "src/main.py" in paths

    def test_pin_path_escape_blocked(self, tmp_path: Path):
        svc = _make_svc(tmp_path, restrict=True)
        with pytest.raises(PermissionError):
            svc.pin_file("../../etc/passwd")


class TestRecentFiles:

    def test_touch_recent(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        result = svc.touch_recent("README.md")
        assert result["recorded"] is True

    def test_list_recent(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        svc.touch_recent("README.md")
        svc.touch_recent("src/main.py")
        result = svc.list_recent()
        assert result["count"] == 2
        # Most recent first
        assert result["files"][0]["path"] == "src/main.py"

    def test_recent_dedup(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        svc.touch_recent("README.md")
        svc.touch_recent("src/main.py")
        svc.touch_recent("README.md")
        result = svc.list_recent()
        paths = [f["path"] for f in result["files"]]
        assert paths.count("README.md") == 1
        assert paths[0] == "README.md"

    def test_recent_limit(self, tmp_path: Path):
        svc = _make_svc(tmp_path)
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        for i in range(5):
            svc.touch_recent(f"f{i}.txt")
        result = svc.list_recent(limit=3)
        assert result["count"] == 3


class TestMetaPersistence:

    def test_metadata_in_data_root_not_project_root(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc = _make_svc(tmp_path)
        svc.pin_file("README.md")
        svc.touch_recent("src/main.py")

        # Metadata should NOT be in project root
        assert not (tmp_path / ".miqi").exists()
        assert not (tmp_path / ".miqi" / "workspace_meta.json").exists()

        # Metadata SHOULD be in MiQi data root
        meta_path = _meta_dir_for_root(tmp_path.resolve())
        assert meta_path.exists()
        import json
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "README.md" in data["pinned"]
        assert "src/main.py" in data["recent"]

    def test_pinned_persisted_across_instances(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc1 = _make_svc(tmp_path)
        svc1.pin_file("README.md")
        svc2 = _make_svc(tmp_path)
        result = svc2.list_pinned()
        assert result["count"] == 1
        assert result["files"][0]["path"] == "README.md"

    def test_recent_persisted_across_instances(self, tmp_path: Path):
        _populate_workspace(tmp_path)
        svc1 = _make_svc(tmp_path)
        svc1.touch_recent("src/main.py")
        svc2 = _make_svc(tmp_path)
        result = svc2.list_recent()
        assert result["count"] == 1
        assert result["files"][0]["path"] == "src/main.py"

    def test_validate_paths_filters_outside(self, tmp_path: Path):
        """If meta file contains paths outside root, they are filtered on load."""
        svc = _make_svc(tmp_path)
        # Manually inject a bad path
        svc._pinned = ["README.md", "../../etc/passwd"]
        svc._save_meta()

        svc2 = _make_svc(tmp_path)
        assert "../../etc/passwd" not in svc2._pinned


# ══════════════════════════════════════════════════════════════════════════
# IPC handler tests via RpcDispatcher
# ══════════════════════════════════════════════════════════════════════════

class TestWorkspaceIpcHandlers:

    @pytest.mark.asyncio
    async def test_workspace_status(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=1, method="workspace.status")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["exists"] is True
        assert "project_root" in resp.result

    @pytest.mark.asyncio
    async def test_workspace_list(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=2, method="workspace.list")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "workspaces" in resp.result
        assert resp.result["count"] >= 1

    @pytest.mark.asyncio
    async def test_workspace_index_empty(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=3, method="workspace.index")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] == 0

    @pytest.mark.asyncio
    async def test_workspace_index_with_files(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=4, method="workspace.index")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] >= 1

    @pytest.mark.asyncio
    async def test_workspace_preview(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        (tmp_path / "readme.txt").write_text("hello world", encoding="utf-8")
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=5, method="workspace.preview", params={"path": "readme.txt"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_workspace_preview_missing_path(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=6, method="workspace.preview", params={})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_workspace_pin_file(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        (tmp_path / "test.py").write_text("pass", encoding="utf-8")
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=7, method="workspace.pinFile", params={"path": "test.py"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["pinned"] is True

    @pytest.mark.asyncio
    async def test_workspace_unpin_file(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        (tmp_path / "test.py").write_text("pass", encoding="utf-8")
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        await dispatcher.dispatch(JsonRpcRequest(id=8, method="workspace.pinFile", params={"path": "test.py"}))
        req = JsonRpcRequest(id=9, method="workspace.unpinFile", params={"path": "test.py"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["pinned"] is False

    @pytest.mark.asyncio
    async def test_workspace_list_pinned(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=10, method="workspace.listPinned")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] == 0

    @pytest.mark.asyncio
    async def test_workspace_list_recent(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=11, method="workspace.listRecent")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "files" in resp.result

    @pytest.mark.asyncio
    async def test_workspace_methods_registered(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        names = dispatcher.method_names
        assert "workspace.status" in names
        assert "workspace.list" in names
        assert "workspace.open" in names
        assert "workspace.index" in names
        assert "workspace.preview" in names
        assert "workspace.pinFile" in names
        assert "workspace.unpinFile" in names
        assert "workspace.listPinned" in names
        assert "workspace.listRecent" in names

    @pytest.mark.asyncio
    async def test_workspace_open(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        new_ws = tmp_path / "new_workspace"
        new_ws.mkdir()
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=12, method="workspace.open", params={"path": str(new_ws)})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["project_root"] == str(new_ws.resolve())

    @pytest.mark.asyncio
    async def test_workspace_path_escape_returns_error(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch, restrict=True)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        outside = tmp_path.parent / "definitely_outside_ws_escape_test"
        req = JsonRpcRequest(id=13, method="workspace.preview", params={"path": str(outside)})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None
