"""cleanup_orphan_results against a throwaway result tree."""

import pytest

from core.orchestrator import WorkflowEngine


class IsolatedEngine:
    @staticmethod
    def build(tmp_path, monkeypatch):
        from config import settings

        result = tmp_path / "result"
        session_dir = tmp_path / "sessions"
        for path in (result / "script", result / "image", result / "video", session_dir):
            path.mkdir(parents=True)
        monkeypatch.setattr(settings, "RESULT_DIR", str(result), raising=False)
        engine = WorkflowEngine()
        engine.sessions.clear()
        engine._session_dir = str(session_dir)
        return engine, result, session_dir


class TestOrphanCleanup:
    def test_removes_results_whose_session_is_gone_and_keeps_the_rest(self, tmp_path, monkeypatch):
        engine, result, session_dir = IsolatedEngine.build(tmp_path, monkeypatch)
        (session_dir / "live.json").write_text("{}", encoding="utf-8")
        engine.sessions["memory-only"] = object()

        (result / "script" / "live.json").write_text("{}", encoding="utf-8")
        (result / "script" / "memory-only.json").write_text("{}", encoding="utf-8")
        (result / "script" / "orphan.json").write_text("{}", encoding="utf-8")
        (result / "script" / "notes.txt").write_text("keep", encoding="utf-8")
        (result / "image" / "orphan").mkdir()
        (result / "image" / "live").mkdir()
        (result / "image" / "test_avail").mkdir()
        (result / "video" / "orphan").mkdir()
        (result / "video" / "memory-only").mkdir()

        report = engine.cleanup_orphan_results()

        assert report["status"] == "cleaned"
        assert report["cleaned"]["scripts"] == ["orphan"]
        assert report["cleaned"]["images"] == ["orphan"]
        assert report["cleaned"]["videos"] == ["orphan"]
        assert (result / "script" / "live.json").exists()
        assert (result / "script" / "memory-only.json").exists()
        assert (result / "script" / "notes.txt").read_text(encoding="utf-8") == "keep"
        assert (result / "image" / "live").is_dir()
        assert (result / "image" / "test_avail").is_dir()
        assert (result / "video" / "memory-only").is_dir()
        assert not (result / "image" / "orphan").exists()

    @pytest.mark.xfail(
        strict=True,
        raises=NotADirectoryError,
        reason=(
            "BUG orchestrator.py:1549 cleanup_orphan_results calls shutil.rmtree on every "
            "entry under result/image. A stray file raises NotADirectoryError and aborts "
            "before later orphan directories are removed. The video loop at line 1555 has "
            "the same shape. Repro: place result/image/readme.txt and call cleanup_orphan_results()."
        ),
    )
    def test_stray_file_in_the_image_dir_does_not_abort_cleanup(self, tmp_path, monkeypatch):
        engine, result, _session_dir = IsolatedEngine.build(tmp_path, monkeypatch)
        stray = result / "image" / "readme.txt"
        stray.write_text("not a session directory", encoding="utf-8")
        (result / "image" / "orphan-dir").mkdir()

        report = engine.cleanup_orphan_results()

        assert report["status"] == "cleaned"
        assert "orphan-dir" in report["cleaned"]["images"]
        assert not stray.exists()
        assert not (result / "image" / "orphan-dir").exists()
