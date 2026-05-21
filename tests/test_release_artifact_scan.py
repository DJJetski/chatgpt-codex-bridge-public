import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


class ReleaseArtifactScanTests(unittest.TestCase):
    def test_release_artifact_scan_accepts_clean_archives_and_plain_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            plain = root / "install.sh"
            plain.write_text("#!/usr/bin/env sh\nset -eu\n", encoding="utf-8")
            wheel = root / "chatgpt_codex_bridge-0.1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("chatgpt_codex_bridge/__init__.py", "__version__ = '0.1.0'\n")
            source = root / "chatgpt-codex-bridge-v0.1.0-source.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                readme = root / "README.md"
                readme.write_text("# clean\n", encoding="utf-8")
                archive.add(readme, arcname="chatgpt-codex-bridge-v0.1.0/README.md")

            result = _run_scan(plain, wheel, source)

            self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_release_artifact_scan_rejects_runtime_paths_and_private_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad_archive = root / "bad-source.tar.gz"
            private_file = root / "thread.txt"
            private_home = "/" + "Users" + "/example/private"
            thread_id = "019abcde" + "-0000-0000-0000-000000000000"
            private_file.write_text(
                f"{private_home} {thread_id}\n",
                encoding="utf-8",
            )
            with tarfile.open(bad_archive, "w:gz") as archive:
                archive.add(private_file, arcname="chatgpt-codex-bridge-v0.1.0/state/thread.txt")

            result = _run_scan(bad_archive)

            self.assertEqual(result.returncode, 1)
            self.assertIn("runtime_path", result.stderr)
            self.assertIn("private_home_path", result.stderr)
            self.assertIn("codex_thread_id", result.stderr)

    def test_release_artifact_scan_rejects_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sidecar = root / "supervisor.sqlite-wal"
            sidecar.write_text("sqlite sidecar\n", encoding="utf-8")

            result = _run_scan(sidecar)

            self.assertEqual(result.returncode, 1)
            self.assertIn("blocked_suffix", result.stderr)

    def test_release_artifact_scan_rejects_local_codesearch_cache(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad_archive = root / "bad-source.tar.gz"
            index_file = root / "index.bin"
            index_file.write_bytes(b"generated index")
            with tarfile.open(bad_archive, "w:gz") as archive:
                archive.add(index_file, arcname="chatgpt-codex-bridge-v0.1.0/.coa/codesearch/indexes/index.bin")

            result = _run_scan(bad_archive)

            self.assertEqual(result.returncode, 1)
            self.assertIn("runtime_path", result.stderr)

    def test_release_artifact_scan_rejects_private_docs_and_control_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad_archive = root / "bad-source.tar.gz"
            private_doc = root / "DECISIONS.md"
            private_doc.write_text("# decisions\n", encoding="utf-8")
            private_control_file = root / "AGENTS.md"
            private_control_file.write_text("# agents\n", encoding="utf-8")
            with tarfile.open(bad_archive, "w:gz") as archive:
                archive.add(private_doc, arcname="chatgpt-codex-bridge-v0.1.0/docs/private/DECISIONS.md")
                archive.add(private_control_file, arcname="chatgpt-codex-bridge-v0.1.0/AGENTS.md")

            result = _run_scan(bad_archive)

            self.assertEqual(result.returncode, 1)
            self.assertIn("private_doc_path", result.stderr)
            self.assertIn("private_control_file", result.stderr)

    def test_release_artifact_scan_rejects_plain_private_paths_with_directory_context(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            private_dir = root / "docs" / "private"
            private_dir.mkdir(parents=True)
            private_doc = private_dir / "README.md"
            private_doc.write_text("# private\n", encoding="utf-8")

            result = _run_scan(private_doc)

            self.assertEqual(result.returncode, 1)
            self.assertIn("private_doc_path", result.stderr)

    def test_release_artifact_scan_accepts_directory_argument_and_preserves_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            public_dir = root / "public"
            public_dir.mkdir()
            (public_dir / "README.md").write_text("# public\n", encoding="utf-8")

            result = _run_scan(public_dir)

            self.assertEqual(result.returncode, 0, msg=result.stderr)


def _run_scan(*paths: Path) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "check_release_artifacts.py"), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )


if __name__ == "__main__":
    unittest.main()
