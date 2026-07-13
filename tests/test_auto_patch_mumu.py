from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).resolve().parents[1] / "auto-patch-mumu.py"
spec = importlib.util.spec_from_file_location("auto_patch_mumu", MODULE_PATH)
assert spec is not None
patcher = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = patcher
spec.loader.exec_module(patcher)


class AutoPatchMumuTests(unittest.TestCase):
    def patched_data(self, data: bytes) -> bytes:
        patched = bytearray(data)
        patched[2:3] = bytes.fromhex("BB")
        patched[5:7] = bytes.fromhex("EE FF")
        return bytes(patched)

    def make_manifest(self, root: Path, data: bytes) -> tuple[Any, Path]:
        target = root / "MuMuNxMain.exe"
        target.write_bytes(data)
        patched = self.patched_data(data)
        raw = {
            "schema_version": 1,
            "targets": [
                {
                    "key": "main",
                    "path": str(target),
                    "process_name": "MuMuNxMain.exe",
                    "baseline_sha256": patcher.sha256_bytes(data),
                    "patched_sha256": patcher.sha256_bytes(patched),
                    "entries": [
                        {"id": "one", "offset": "0x2", "original": "AA", "patched": "BB"},
                        {"id": "two", "offset": "0x5", "original": "CC DD", "patched": "EE FF"},
                        {"id": "keep", "offset": "0x9", "original": "11", "patched": "11", "mode": "validation"},
                    ],
                }
            ],
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        return patcher.load_manifest(manifest_path), target

    def make_multi_manifest(self, root: Path, data: bytes) -> tuple[Any, list[Path]]:
        first = root / "MuMuNxMain.exe"
        second = root / "MuMuNxService.exe"
        first.write_bytes(data)
        second.write_bytes(data)
        patched = bytearray(data)
        patched[2:3] = bytes.fromhex("BB")
        raw = {
            "schema_version": 1,
            "targets": [
                {
                    "key": "main",
                    "path": str(first),
                    "process_name": "MuMuNxMain.exe",
                    "baseline_sha256": patcher.sha256_bytes(data),
                    "patched_sha256": patcher.sha256_bytes(bytes(patched)),
                    "entries": [{"id": "one", "offset": "0x2", "original": "AA", "patched": "BB"}],
                },
                {
                    "key": "service",
                    "path": str(second),
                    "process_name": "MuMuNxService.exe",
                    "baseline_sha256": patcher.sha256_bytes(data),
                    "patched_sha256": patcher.sha256_bytes(bytes(patched)),
                    "entries": [{"id": "one", "offset": "0x2", "original": "AA", "patched": "BB"}],
                },
            ],
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        return patcher.load_manifest(manifest_path), [first, second]

    def original_data(self) -> bytes:
        data = bytearray(b"000000000000")
        data[2:3] = bytes.fromhex("AA")
        data[5:7] = bytes.fromhex("CC DD")
        data[9:10] = bytes.fromhex("11")
        return bytes(data)

    def test_original_patched_and_third_state_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.original_data()
            manifest, _ = self.make_manifest(root, original)
            target = manifest.targets["main"]

            analysis = patcher.analyze_target(target)
            self.assertEqual([state.state for state in analysis.entry_states], ["original", "original", "patched"])
            self.assertEqual(len(analysis.planned_diffs), 2)

            patched = patcher.apply_patch_bytes(original, analysis)
            patched_analysis = patcher.analyze_target(target, patched)
            self.assertTrue(patched_analysis.fully_patched)
            self.assertFalse(patched_analysis.needs_patch)

            third = bytearray(original)
            third[2] = 0x99
            with self.assertRaisesRegex(patcher.PatchError, "unexpected byte state"):
                patcher.analyze_target(target, bytes(third))

    def test_hash_gate_accepts_only_exact_baseline_or_exact_patched_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            target = manifest.targets["main"]
            self.assertTrue(patcher.analyze_target(target).hash_allowed)

            patched = self.patched_data(data)
            target_path.write_bytes(patched)
            self.assertTrue(patcher.analyze_target(target).fully_patched)

            changed = bytearray(data)
            changed[0] = 0x31
            target_path.write_bytes(changed)
            with self.assertRaisesRegex(patcher.PatchError, "SHA256"):
                patcher.analyze_target(target)

            changed = bytearray(patched)
            changed[0] = 0x31
            target_path.write_bytes(changed)
            with self.assertRaisesRegex(patcher.PatchError, "SHA256"):
                patcher.analyze_target(target)

    def test_exact_diff_ranges_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, _ = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            ranges = patcher.changed_ranges(data, patched)
            self.assertEqual(
                [(offset, patcher.hex_bytes(old), patcher.hex_bytes(new)) for offset, old, new in ranges],
                [(2, "AA", "BB"), (5, "CC DD", "EE FF")],
            )

    def test_backup_metadata_and_rollback_restore_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
            metadata = json.loads((backup_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["files"][0]["pre_sha256"], patcher.sha256_bytes(data))
            self.assertEqual(len(metadata["files"][0]["diffs"]), 2)

            target_path.write_bytes(patched)
            patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)
            self.assertEqual(target_path.read_bytes(), data)

    def test_path_allowlist_and_global_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, target_path = self.make_manifest(root, self.original_data())
            self.assertEqual(patcher.resolve_target(target_path, manifest).key, "main")
            with self.assertRaisesRegex(patcher.PatchError, "not allowlisted"):
                patcher.resolve_target(root / "Other.exe", manifest)
            with self.assertRaisesRegex(patcher.PatchError, "MuMuPlayerGlobal"):
                patcher.resolve_target(Path(r"H:\MuMuPlayerGlobal\nx_main\MuMuNxMain.exe"), manifest)

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            before = patcher.sha256_file(target_path)
            patcher.command_apply([manifest.targets["main"]], manifest, dry_run=True)
            after = patcher.sha256_file(target_path)
            self.assertEqual(before, after)
            self.assertEqual(target_path.read_bytes(), data)

    def test_process_matching_ignores_global_same_name_and_blocks_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, target_path = self.make_manifest(root, self.original_data())
            target = manifest.targets["main"]
            global_process = patcher.ProcessInfo("MuMuNxMain.exe", r"H:\MuMuPlayerGlobal\nx_main\MuMuNxMain.exe")
            local_process = patcher.ProcessInfo("MuMuNxMain.exe", str(target_path))
            self.assertEqual(patcher.blocking_process_names_from_records([target], [global_process]), [])
            self.assertEqual(
                patcher.blocking_process_names_from_records([target], [global_process, local_process]),
                ["MuMuNxMain.exe"],
            )

    def test_process_matching_blocks_unique_name_when_path_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self.make_manifest(root, self.original_data())
            target = manifest.targets["main"]
            hidden_path_process = patcher.ProcessInfo("MuMuNxMain.exe", None)
            self.assertEqual(
                patcher.blocking_process_names_from_records([target], [hidden_path_process]),
                ["MuMuNxMain.exe"],
            )

    def test_multi_target_apply_restores_written_target_after_later_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, paths = self.make_multi_manifest(root, data)
            original_require = patcher.require_processes_stopped
            original_unique_backup_dir = patcher.unique_backup_dir
            original_temp_replace = patcher.temp_replace_bytes
            calls: list[Path] = []

            def fake_replace(
                target: Path,
                new_data: bytes,
                expected_sha256: str,
                preflight: Any | None = None,
            ) -> None:
                calls.append(target)
                if target == paths[1]:
                    raise patcher.PatchError("synthetic second write failure")
                original_temp_replace(target, new_data, expected_sha256, preflight=preflight)

            try:
                setattr(patcher, "require_processes_stopped", lambda targets: None)
                setattr(patcher, "unique_backup_dir", lambda: root / "backup")
                setattr(patcher, "temp_replace_bytes", fake_replace)
                with self.assertRaisesRegex(patcher.PatchError, "synthetic second write failure"):
                    patcher.command_apply(list(manifest.targets.values()), manifest, dry_run=False, enforce_topology=False)
            finally:
                setattr(patcher, "require_processes_stopped", original_require)
                setattr(patcher, "unique_backup_dir", original_unique_backup_dir)
                setattr(patcher, "temp_replace_bytes", original_temp_replace)

            self.assertIn(paths[0], calls)
            self.assertIn(paths[1], calls)
            self.assertEqual(paths[0].read_bytes(), data)
            self.assertEqual(paths[1].read_bytes(), data)
            self.assertTrue((root / "backup" / "metadata.json").is_file())

    def test_process_query_failure_raises_and_blocks_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, _ = self.make_manifest(root, data)
            original_run = patcher.subprocess.run
            original_name = patcher.os.name

            def failed_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="boom")

            try:
                setattr(patcher.os, "name", "nt")
                setattr(patcher.subprocess, "run", failed_run)
                with self.assertRaisesRegex(patcher.PatchError, "process discovery failed"):
                    patcher.query_windows_processes(["MuMuNxMain.exe"])
                with self.assertRaisesRegex(patcher.PatchError, "process discovery failed"):
                    patcher.command_apply([manifest.targets["main"]], manifest, dry_run=False, enforce_topology=False)
            finally:
                setattr(patcher.subprocess, "run", original_run)
                setattr(patcher.os, "name", original_name)

    def test_process_query_bad_json_raises(self) -> None:
        original_run = patcher.subprocess.run
        original_name = patcher.os.name

        def bad_json_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args[0], 0, stdout="not-json", stderr="")

        try:
            setattr(patcher.os, "name", "nt")
            setattr(patcher.subprocess, "run", bad_json_run)
            with self.assertRaisesRegex(patcher.PatchError, "invalid JSON"):
                patcher.query_windows_processes(["MuMuNxMain.exe"])
        finally:
            setattr(patcher.subprocess, "run", original_run)
            setattr(patcher.os, "name", original_name)

    def test_malicious_process_name_rejected_before_powershell(self) -> None:
        with self.assertRaisesRegex(patcher.PatchError, "unsafe process name"):
            patcher.query_windows_processes(['MuMuNxMain.exe"; Stop-Process calc; "x.exe'])

    def test_custom_manifest_apply_and_rollback_are_refused_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
            with self.assertRaisesRegex(patcher.PatchError, "trusted default manifest"):
                patcher.command_apply([manifest.targets["main"]], manifest, dry_run=False)
            target_path.write_bytes(patched)
            with self.assertRaisesRegex(patcher.PatchError, "trusted default manifest"):
                patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [])

    def test_rollback_refuses_current_target_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
            drifted = bytearray(patched)
            drifted[0] = 0x31
            target_path.write_bytes(drifted)
            with self.assertRaisesRegex(patcher.PatchError, "rollback target drift"):
                patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)

    def test_rollback_noops_when_already_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
            before = patcher.sha256_file(target_path)
            patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)
            self.assertEqual(patcher.sha256_file(target_path), before)

    def test_rollback_rejects_backup_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, _ = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
            metadata_path = backup_dir / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["files"][0]["backup_file"] = "..\\evil.exe"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(patcher.PatchError, "backup_file mismatch"):
                patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)

    def test_manifest_invariants_reject_invalid_entries_and_duplicate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, _ = self.make_manifest(root, data)
            raw = manifest.raw
            raw["targets"][0]["entries"][0]["mode"] = "bad"
            path = root / "bad-mode.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(patcher.PatchError, "unsupported mode"):
                patcher.load_manifest(path)

            manifest, _ = self.make_manifest(root, data)
            raw = manifest.raw
            raw["targets"].append(dict(raw["targets"][0], key="other"))
            path = root / "dup-path.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(patcher.PatchError, "duplicate target path"):
                patcher.load_manifest(path)


    def test_default_manifest_unselected_target_topology_drift_is_refused(self) -> None:
        default_manifest = patcher.load_manifest(patcher.DEFAULT_MANIFEST)
        targets = dict(default_manifest.targets)
        service = targets["service"]
        targets["service"] = patcher.TargetSpec(
            key=service.key,
            path=Path(r"H:\MuMuPlayer\nx_main\AlteredService.exe"),
            process_name=service.process_name,
            baseline_sha256=service.baseline_sha256,
            patched_sha256=service.patched_sha256,
            entries=service.entries,
        )
        modified = patcher.Manifest(path=patcher.DEFAULT_MANIFEST, raw=default_manifest.raw, targets=targets, digest=default_manifest.digest)
        with self.assertRaisesRegex(patcher.PatchError, "manifest target service"):
            patcher.enforce_official_write_topology(modified, [modified.targets["main"]])

    def test_rollback_default_process_query_failure_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
            target_path.write_bytes(patched)
            original_run = patcher.subprocess.run
            original_name = patcher.os.name

            def failed_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="rollback boom")

            try:
                setattr(patcher.os, "name", "nt")
                setattr(patcher.subprocess, "run", failed_run)
                with self.assertRaisesRegex(patcher.PatchError, "process discovery failed"):
                    patcher.command_rollback(backup_dir, manifest, enforce_topology=False)
            finally:
                setattr(patcher.subprocess, "run", original_run)
                setattr(patcher.os, "name", original_name)


    def test_official_manifest_content_mutation_refuses_write_identity(self) -> None:
        default_manifest = patcher.load_manifest(patcher.DEFAULT_MANIFEST)
        raw = json.loads(json.dumps(default_manifest.raw))
        raw["targets"][0]["entries"][0]["offset"] = "0x1"
        mutated = patcher.Manifest(
            path=patcher.DEFAULT_MANIFEST,
            raw=raw,
            targets=default_manifest.targets,
            digest=patcher.canonical_manifest_sha256(raw),
        )
        with self.assertRaisesRegex(patcher.PatchError, "content digest"):
            patcher.enforce_official_write_topology(mutated, [mutated.targets["main"]])

    def test_computed_patched_hash_must_match_manifest_post_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, _ = self.make_manifest(root, data)
            target = manifest.targets["main"]
            bad_target = patcher.TargetSpec(
                key=target.key,
                path=target.path,
                process_name=target.process_name,
                baseline_sha256=target.baseline_sha256,
                patched_sha256="0" * 64,
                entries=target.entries,
            )
            bad_manifest = patcher.Manifest(path=manifest.path, raw=manifest.raw, targets={"main": bad_target}, digest=manifest.digest)
            with self.assertRaisesRegex(patcher.PatchError, "computed patched SHA256"):
                patcher.command_apply([bad_target], bad_manifest, dry_run=False, enforce_topology=False)

    def test_rollback_metadata_forgery_fields_refuse_before_write(self) -> None:
        cases = [
            ("pre_sha256", "0" * 64, "pre_sha256 mismatch"),
            ("post_sha256", "0" * 64, "post_sha256 mismatch"),
            ("target_path", "Other.exe", "target_path mismatch"),
            ("process_name", "Other.exe", "process_name mismatch"),
            ("backup_file", "Other.exe", "backup_file mismatch"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                data = self.original_data()
                manifest, target_path = self.make_manifest(root, data)
                analysis = patcher.analyze_target(manifest.targets["main"])
                patched = patcher.apply_patch_bytes(data, analysis)
                backup_dir = root / "backup"
                patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
                target_path.write_bytes(patched)
                metadata_path = backup_dir / "metadata.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["files"][0][field] = value
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                with self.assertRaisesRegex(patcher.PatchError, message):
                    patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)

    def test_process_discovery_uses_winapi_system_directory_and_ignores_poisoned_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ps = root / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            ps.parent.mkdir(parents=True)
            ps.write_text("", encoding="utf-8")
            poisoned = root / "poison"
            poisoned.mkdir()
            original_name = patcher.os.name
            original_env = dict(patcher.os.environ)
            original_run = patcher.subprocess.run
            original_windll = getattr(patcher.ctypes, "windll", None)
            seen: list[list[str]] = []

            class Kernel32:
                @staticmethod
                def GetSystemDirectoryW(buffer: Any, size: int) -> int:
                    value = str(root)
                    buffer.value = value
                    return len(value)

            class Windll:
                kernel32 = Kernel32()

            def ok_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                seen.append(args)
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            try:
                setattr(patcher.os, "name", "nt")
                patcher.os.environ["SystemRoot"] = str(poisoned)
                patcher.os.environ["WINDIR"] = str(poisoned)
                setattr(patcher.ctypes, "windll", Windll())
                setattr(patcher.subprocess, "run", ok_run)
                self.assertEqual(patcher.query_windows_processes(["MuMuNxMain.exe"]), [])
                self.assertEqual(Path(seen[0][0]), ps)
                self.assertNotIn("poison", str(seen[0][0]))
                ps.unlink()
                with self.assertRaisesRegex(patcher.PatchError, "system PowerShell executable not found"):
                    patcher.query_windows_processes(["MuMuNxMain.exe"])
            finally:
                setattr(patcher.subprocess, "run", original_run)
                setattr(patcher.os, "name", original_name)
                if original_windll is not None:
                    setattr(patcher.ctypes, "windll", original_windll)
                patcher.os.environ.clear()
                patcher.os.environ.update(original_env)

    def test_process_discovery_winapi_failure_refuses_before_subprocess(self) -> None:
        original_name = patcher.os.name
        original_windll = getattr(patcher.ctypes, "windll", None)
        original_run = patcher.subprocess.run
        calls: list[str] = []

        class Kernel32:
            @staticmethod
            def GetSystemDirectoryW(buffer: Any, size: int) -> int:
                return 0

        class Windll:
            kernel32 = Kernel32()

        def should_not_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append("run")
            return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

        try:
            setattr(patcher.os, "name", "nt")
            setattr(patcher.ctypes, "windll", Windll())
            setattr(patcher.subprocess, "run", should_not_run)
            with self.assertRaisesRegex(patcher.PatchError, "GetSystemDirectoryW"):
                patcher.query_windows_processes(["MuMuNxMain.exe"])
            self.assertEqual(calls, [])
        finally:
            setattr(patcher.subprocess, "run", original_run)
            setattr(patcher.os, "name", original_name)
            if original_windll is not None:
                setattr(patcher.ctypes, "windll", original_windll)

    def test_target_key_selection_and_launcher_transactional_main_service(self) -> None:
        default_manifest = patcher.load_manifest(patcher.DEFAULT_MANIFEST)
        args = patcher.build_parser().parse_args(["dry-run", "--targets", "main,service"])
        self.assertEqual([target.key for target in patcher.selected_targets(args, default_manifest)], ["main", "service"])
        with self.assertRaisesRegex(patcher.PatchError, "mutually exclusive"):
            args = patcher.build_parser().parse_args(["dry-run", "--targets", "main", "--target", str(patcher.OFFICIAL_TOPOLOGY["main"][0])])
            patcher.selected_targets(args, default_manifest)
        launcher = (MODULE_PATH.parent / "start-mumu-patched.cmd").read_text(encoding="utf-8")
        self.assertIn(r"%LocalAppData%\Programs\Python\Python312\python.exe", launcher)
        self.assertIn("apply --targets main,service", launcher)
        self.assertIn("verify --targets main,service", launcher)
        self.assertEqual(launcher.count("apply --targets main,service"), 1)
        self.assertNotIn("\npython ", launcher.lower())
        self.assertNotIn("C:\\Users\\didi\\", launcher)
        self.assertNotIn("--targets main,service,remote", launcher)

    def test_manifest_name_must_match_path_basename_and_process_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, _ = self.make_manifest(root, data)
            raw = manifest.raw
            raw["targets"][0]["name"] = "Other.exe"
            path = root / "bad-name.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(patcher.PatchError, "name must match"):
                patcher.load_manifest(path)

    def test_symlink_target_and_backup_are_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.exe"
            link = root / "link.exe"
            real.write_bytes(self.original_data())
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not supported in this environment")
            with self.assertRaisesRegex(patcher.PatchError, "symlink or reparse"):
                patcher.require_regular_non_reparse_file(link, "test target")


    def test_exclusive_temp_replacement_ignores_old_predictable_artifact_and_uses_unique_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "MuMuNxMain.exe"
            target.write_bytes(b"old")
            predictable = root / f".{target.name}.tmp-{patcher.os.getpid()}"
            predictable.write_bytes(b"do-not-touch")
            seen: list[Path] = []
            original_replace = patcher.os.replace

            def recording_replace(src: str | Path, dst: str | Path) -> None:
                seen.append(Path(src))
                original_replace(src, dst)

            try:
                setattr(patcher.os, "replace", recording_replace)
                patcher.temp_replace_bytes(target, b"new", patcher.sha256_bytes(b"new"))
            finally:
                setattr(patcher.os, "replace", original_replace)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(predictable.read_bytes(), b"do-not-touch")
            self.assertEqual(len(seen), 1)
            self.assertNotEqual(seen[0], predictable)

    def test_temp_replace_rejects_parent_symlink_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_dir = root / "real"
            real_dir.mkdir()
            link_dir = root / "link"
            try:
                link_dir.symlink_to(real_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlink creation is not supported in this environment")
            target = link_dir / "MuMuNxMain.exe"
            with self.assertRaisesRegex(patcher.PatchError, "symlink or reparse"):
                patcher.temp_replace_bytes(target, b"x", patcher.sha256_bytes(b"x"))

    def test_multi_target_rollback_second_drift_causes_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, paths = self.make_multi_manifest(root, data)
            analyses = [patcher.analyze_target(t) for t in manifest.targets.values()]
            patched_by_key = {a.spec.key: patcher.apply_patch_bytes(data, a) for a in analyses}
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, analyses, {k: patcher.sha256_bytes(v) for k, v in patched_by_key.items()})
            paths[0].write_bytes(patched_by_key["main"])
            drift = bytearray(patched_by_key["service"])
            drift[0] = 0x31
            paths[1].write_bytes(drift)
            with self.assertRaisesRegex(patcher.PatchError, "rollback target drift"):
                patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)
            self.assertEqual(paths[0].read_bytes(), patched_by_key["main"])

    def test_multi_target_rollback_second_replace_failure_recovers_first_to_patched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, paths = self.make_multi_manifest(root, data)
            analyses = [patcher.analyze_target(t) for t in manifest.targets.values()]
            patched_by_key = {a.spec.key: patcher.apply_patch_bytes(data, a) for a in analyses}
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, analyses, {k: patcher.sha256_bytes(v) for k, v in patched_by_key.items()})
            paths[0].write_bytes(patched_by_key["main"])
            paths[1].write_bytes(patched_by_key["service"])
            original_replace = patcher.temp_replace_bytes
            calls: list[Path] = []

            def failing_second(target: Path, blob: bytes, digest: str, preflight: Any | None = None) -> None:
                calls.append(target)
                if target == paths[1]:
                    raise patcher.PatchError("second restore failed")
                original_replace(target, blob, digest, preflight=preflight)

            try:
                setattr(patcher, "temp_replace_bytes", failing_second)
                with self.assertRaisesRegex(patcher.PatchError, "second restore failed"):
                    patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)
            finally:
                setattr(patcher, "temp_replace_bytes", original_replace)
            self.assertEqual(paths[0].read_bytes(), patched_by_key["main"])
            self.assertEqual(paths[1].read_bytes(), patched_by_key["service"])

    def test_multi_target_rollback_recovery_failure_reports_both_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, paths = self.make_multi_manifest(root, data)
            analyses = [patcher.analyze_target(t) for t in manifest.targets.values()]
            patched_by_key = {a.spec.key: patcher.apply_patch_bytes(data, a) for a in analyses}
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, analyses, {k: patcher.sha256_bytes(v) for k, v in patched_by_key.items()})
            paths[0].write_bytes(patched_by_key["main"])
            paths[1].write_bytes(patched_by_key["service"])
            original_replace = patcher.temp_replace_bytes
            mode = {"recover": False}

            def fail_restore_and_recover(target: Path, blob: bytes, digest: str, preflight: Any | None = None) -> None:
                if target == paths[1] and not mode["recover"]:
                    mode["recover"] = True
                    raise patcher.PatchError("restore failed")
                if target == paths[0] and mode["recover"]:
                    raise patcher.PatchError("recovery failed")
                original_replace(target, blob, digest, preflight=preflight)

            try:
                setattr(patcher, "temp_replace_bytes", fail_restore_and_recover)
                with self.assertRaisesRegex(patcher.PatchError, "rollback failed: restore failed; rollback recovery also failed: recovery failed"):
                    patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)
            finally:
                setattr(patcher, "temp_replace_bytes", original_replace)

    def test_successful_multi_target_rollback_restores_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, paths = self.make_multi_manifest(root, data)
            analyses = [patcher.analyze_target(t) for t in manifest.targets.values()]
            patched_by_key = {a.spec.key: patcher.apply_patch_bytes(data, a) for a in analyses}
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, analyses, {k: patcher.sha256_bytes(v) for k, v in patched_by_key.items()})
            paths[0].write_bytes(patched_by_key["main"])
            paths[1].write_bytes(patched_by_key["service"])
            patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)
            self.assertEqual(paths[0].read_bytes(), data)
            self.assertEqual(paths[1].read_bytes(), data)

    def test_launcher_mentions_main_and_service_but_not_remote_apply(self) -> None:
        launcher = (MODULE_PATH.parent / "start-mumu-patched.cmd").read_text(encoding="utf-8")
        self.assertIn("MuMuNxMain.exe", launcher)
        self.assertIn("MuMuNxService.exe", launcher)
        self.assertNotIn("MuMuRemoteService.exe", launcher)
        self.assertNotIn("\npython ", launcher.lower())

    def test_rollback_metadata_duplicate_target_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
            target_path.write_bytes(patched)
            metadata_path = backup_dir / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["files"].append(dict(metadata["files"][0]))
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(patcher.PatchError, "duplicate rollback target_key"):
                patcher.command_rollback(backup_dir, manifest, checker=lambda targets: [], enforce_topology=False)

    def test_command_rollback_uses_checker_without_global_monkeypatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, target_path = self.make_manifest(root, data)
            analysis = patcher.analyze_target(manifest.targets["main"])
            patched = patcher.apply_patch_bytes(data, analysis)
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, [analysis], {"main": patcher.sha256_bytes(patched)})
            target_path.write_bytes(patched)
            original_require = patcher.require_processes_stopped
            with self.assertRaisesRegex(patcher.PatchError, "target processes are running"):
                patcher.command_rollback(
                    backup_dir,
                    manifest,
                    checker=lambda targets: ["MuMuNxMain.exe"],
                    enforce_topology=False,
                )
            self.assertIs(patcher.require_processes_stopped, original_require)
            self.assertNotIn("globals()", MODULE_PATH.read_text(encoding="utf-8"))

    def test_rollback_recovery_rechecks_process_state_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, paths = self.make_multi_manifest(root, data)
            analyses = [patcher.analyze_target(t) for t in manifest.targets.values()]
            patched_by_key = {a.spec.key: patcher.apply_patch_bytes(data, a) for a in analyses}
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, analyses, {k: patcher.sha256_bytes(v) for k, v in patched_by_key.items()})
            paths[0].write_bytes(patched_by_key["main"])
            paths[1].write_bytes(patched_by_key["service"])
            original_replace = patcher.temp_replace_bytes
            mode = {"recover": False}

            def fail_second_then_normal(target: Path, blob: bytes, digest: str, preflight: Any | None = None) -> None:
                if target == paths[1] and not mode["recover"]:
                    mode["recover"] = True
                    raise patcher.PatchError("restore failed")
                original_replace(target, blob, digest, preflight=preflight)

            def checker(targets: Any) -> list[str]:
                return ["MuMuNxMain.exe"] if mode["recover"] else []

            try:
                setattr(patcher, "temp_replace_bytes", fail_second_then_normal)
                with self.assertRaisesRegex(patcher.PatchError, "rollback recovery also failed: target processes are running"):
                    patcher.command_rollback(backup_dir, manifest, checker=checker, enforce_topology=False)
            finally:
                setattr(patcher, "temp_replace_bytes", original_replace)
            self.assertEqual(paths[0].read_bytes(), data)
            self.assertEqual(paths[1].read_bytes(), patched_by_key["service"])

    def test_apply_transaction_restore_rechecks_process_state_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.original_data()
            manifest, paths = self.make_multi_manifest(root, data)
            analyses = [patcher.analyze_target(t) for t in manifest.targets.values()]
            patched_by_key = {a.spec.key: patcher.apply_patch_bytes(data, a) for a in analyses}
            backup_dir = root / "backup"
            patcher.create_backup(backup_dir, manifest, analyses, {k: patcher.sha256_bytes(v) for k, v in patched_by_key.items()})
            paths[0].write_bytes(patched_by_key["main"])
            original_require = patcher.require_processes_stopped
            original_replace = patcher.temp_replace_bytes
            calls = {"process_checks": 0}

            def blocked_process_check(targets: Any) -> None:
                calls["process_checks"] += 1
                raise patcher.PatchError("target processes are running; stop these process names and retry: MuMuNxMain.exe")

            try:
                setattr(patcher, "require_processes_stopped", blocked_process_check)
                with self.assertRaisesRegex(patcher.PatchError, "target processes are running"):
                    patcher.restore_written_targets(backup_dir, [analyses[0]])
            finally:
                setattr(patcher, "require_processes_stopped", original_require)
                setattr(patcher, "temp_replace_bytes", original_replace)
            self.assertGreaterEqual(calls["process_checks"], 1)
            self.assertEqual(paths[0].read_bytes(), patched_by_key["main"])


if __name__ == "__main__":
    unittest.main()
