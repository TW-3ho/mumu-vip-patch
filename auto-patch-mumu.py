#!/usr/bin/env python3
"""Version-locked direct EXE patch helper for the local MuMuPlayer crackme.

Targets are bound to an install root (default H:\\MuMuPlayer) via --root while
keeping exact hash/byte gates from mumu-vip-manifest.json. Writes fail closed and
use timestamped backups under <root>\\_ad_vip_tools\\backups\\direct-exe.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PACKAGE_DIR / "mumu-vip-manifest.json"
DEFAULT_INSTALL_ROOT = Path(r"H:\MuMuPlayer")
FORBIDDEN_PATH_MARKER = "mumuplayerglobal"
CHUNK_SIZE = 1024 * 1024
EXPECTED_OFFICIAL_MANIFEST_SHA256 = "C894DB7F607B6E70F696D6C1B3128F99B44BE371D99B379C70B38BC090E2165D"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
SAFE_PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.exe$")
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
OFFICIAL_RELATIVE_TOPOLOGY = {
    "main": (Path("nx_main") / "MuMuNxMain.exe", "MuMuNxMain.exe"),
    "service": (Path("nx_main") / "MuMuNxService.exe", "MuMuNxService.exe"),
    "remote": (Path("nx_main") / "MuMuRemoteService.exe", "MuMuRemoteService.exe"),
}


def official_topology_for_root(root: Path) -> dict[str, tuple[Path, str]]:
    return {
        key: ((root / relative).resolve(strict=False), process_name)
        for key, (relative, process_name) in OFFICIAL_RELATIVE_TOPOLOGY.items()
    }


def backup_root_for(root: Path) -> Path:
    return (root / "_ad_vip_tools" / "backups" / "direct-exe").resolve(strict=False)


def normalize_install_root(root: Path | None = None) -> Path:
    candidate = DEFAULT_INSTALL_ROOT if root is None else Path(root)
    resolved = candidate.resolve(strict=False)
    norm = normalized_path(resolved)
    if not norm or norm.endswith(":"):
        raise PatchError(f"invalid install root: {root}")
    if FORBIDDEN_PATH_MARKER in norm.lower():
        raise PatchError(f"refusing forbidden MuMuPlayerGlobal install root: {resolved}")
    return resolved


# Default topology for the historical install path; bind_manifest_to_root remaps for --root.
OFFICIAL_TOPOLOGY = official_topology_for_root(DEFAULT_INSTALL_ROOT)
BACKUP_ROOT = backup_root_for(DEFAULT_INSTALL_ROOT)


class PatchError(RuntimeError):
    """Raised for validation failures that should be concise in CLI output."""


@dataclass(frozen=True)
class ByteEntry:
    target_key: str
    entry_id: str
    offset: int
    original: bytes
    patched: bytes
    mode: str = "patch"
    description: str = ""

    @property
    def length(self) -> int:
        return len(self.original)


@dataclass(frozen=True)
class TargetSpec:
    key: str
    path: Path
    process_name: str
    baseline_sha256: str
    patched_sha256: str
    entries: tuple[ByteEntry, ...]


@dataclass(frozen=True)
class ProcessInfo:
    name: str
    executable_path: str | None = None


@dataclass(frozen=True)
class Manifest:
    path: Path
    raw: dict[str, Any]
    targets: dict[str, TargetSpec]
    digest: str


@dataclass(frozen=True)
class EntryState:
    entry: ByteEntry
    state: str
    actual_hex: str


@dataclass(frozen=True)
class TargetAnalysis:
    spec: TargetSpec
    path: Path
    sha256: str
    size: int
    entry_states: tuple[EntryState, ...]
    planned_diffs: tuple[dict[str, Any], ...]
    fully_patched: bool
    hash_allowed: bool

    @property
    def needs_patch(self) -> bool:
        return bool(self.planned_diffs)


def bytes_from_hex(value: str) -> bytes:
    return bytes.fromhex(value.replace(" ", ""))


def hex_bytes(value: bytes) -> str:
    return value.hex(" ").upper()


def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest().upper()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).replace("/", "\\")


def canonical_manifest_sha256(raw: dict[str, Any]) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("utf-8"))


def windows_system_directory() -> Path:
    if os.name != "nt":
        return Path(r"C:\Windows\System32")
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    except Exception as exc:  # noqa: BLE001 - fail closed before process discovery.
        raise PatchError(f"GetSystemDirectoryW failed; refusing write: {exc}") from exc
    if size <= 0 or size >= len(buffer):
        raise PatchError("GetSystemDirectoryW returned an invalid or oversized result; refusing write")
    return Path(buffer.value)


def system_powershell_path() -> Path:
    powershell = windows_system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_absolute() or not powershell.is_file():
        raise PatchError(f"system PowerShell executable not found; refusing write: {powershell}")
    return powershell


def is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            import ctypes

            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs == 0xFFFFFFFF:
                raise OSError(f"GetFileAttributesW failed for {path}")
            return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
        return False
    except OSError as exc:
        raise PatchError(f"could not inspect file attributes for {path}: {exc}") from exc


def require_regular_non_reparse_file(path: Path, label: str) -> None:
    if not path.exists():
        raise PatchError(f"{label} is missing: {path}")
    if is_reparse_or_symlink(path):
        raise PatchError(f"{label} must not be a symlink or reparse point: {path}")
    if not path.is_file():
        raise PatchError(f"{label} must be a regular file: {path}")


def require_non_reparse_directory(path: Path, label: str) -> None:
    if not path.exists():
        raise PatchError(f"{label} is missing: {path}")
    if is_reparse_or_symlink(path):
        raise PatchError(f"{label} must not be a symlink or reparse point: {path}")
    if not path.is_dir():
        raise PatchError(f"{label} must be a directory: {path}")


def validate_sha256(value: str, label: str) -> str:
    digest = value.upper()
    if not SHA256_RE.fullmatch(digest):
        raise PatchError(f"{label} must be a 64-character SHA256 hex digest")
    return digest


def validate_process_name(value: str) -> str:
    if not SAFE_PROCESS_NAME_RE.fullmatch(value):
        raise PatchError(f"unsafe process name in manifest: {value!r}")
    return value


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Manifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    targets: dict[str, TargetSpec] = {}
    seen_paths: set[str] = set()
    seen_backup_names: set[str] = set()
    for target in raw["targets"]:
        key = target["key"]
        if key in targets:
            raise PatchError(f"duplicate target key in manifest: {key}")
        target_path = Path(target["path"])
        norm_path = normalized_path(target_path)
        if norm_path in seen_paths:
            raise PatchError(f"duplicate target path in manifest: {target_path}")
        seen_paths.add(norm_path)
        backup_name = target_path.name
        if backup_name in seen_backup_names:
            raise PatchError(f"duplicate backup basename in manifest: {backup_name}")
        seen_backup_names.add(backup_name)
        process_name = validate_process_name(target["process_name"])
        manifest_name = target.get("name")
        if manifest_name is not None and (manifest_name != target_path.name or manifest_name != process_name):
            raise PatchError(f"{key}.name must match path basename and process_name")
        if target_path.name != process_name:
            raise PatchError(f"{key} path basename must match process_name")
        baseline_sha256 = validate_sha256(target["baseline_sha256"], f"{key}.baseline_sha256")
        patched_sha256 = validate_sha256(target["patched_sha256"], f"{key}.patched_sha256")
        entries: list[ByteEntry] = []
        for entry in target["entries"]:
            original = bytes_from_hex(entry["original"])
            patched = bytes_from_hex(entry["patched"])
            mode = entry.get("mode", "patch")
            offset = int(str(entry["offset"]), 16)
            if mode not in {"patch", "validation"}:
                raise PatchError(f"{key}:{entry['id']} has unsupported mode {mode!r}")
            if offset < 0:
                raise PatchError(f"{key}:{entry['id']} offset must be nonnegative")
            if len(original) != len(patched):
                raise PatchError(f"{key}:{entry['id']} original/patched length mismatch")
            if mode == "validation" and original != patched:
                raise PatchError(f"{key}:{entry['id']} validation entries must have identical bytes")
            if mode == "patch" and original == patched:
                raise PatchError(f"{key}:{entry['id']} patch entries must change bytes")
            entries.append(
                ByteEntry(
                    target_key=key,
                    entry_id=entry["id"],
                    offset=offset,
                    original=original,
                    patched=patched,
                    mode=mode,
                    description=entry.get("description", ""),
                )
            )
        targets[key] = TargetSpec(
            key=key,
            path=target_path,
            process_name=process_name,
            baseline_sha256=baseline_sha256,
            patched_sha256=patched_sha256,
            entries=tuple(entries),
        )
    return Manifest(path=path, raw=raw, targets=targets, digest=canonical_manifest_sha256(raw))


def manifest_snapshot(manifest: Manifest) -> str:
    return json.dumps(manifest.raw, indent=2, sort_keys=True) + "\n"


def allowed_paths(manifest: Manifest) -> dict[str, TargetSpec]:
    return {normalized_path(spec.path): spec for spec in manifest.targets.values()}


def resolve_target(path: Path, manifest: Manifest) -> TargetSpec:
    norm = normalized_path(path)
    if FORBIDDEN_PATH_MARKER in norm.lower():
        raise PatchError(f"refusing forbidden MuMuPlayerGlobal path: {path}")
    spec = allowed_paths(manifest).get(norm)
    if spec is None:
        allowed = ", ".join(str(item.path) for item in manifest.targets.values())
        raise PatchError(f"target is not allowlisted: {path}; allowed targets: {allowed}")
    return spec


def selected_targets(args: argparse.Namespace, manifest: Manifest, root: Path = DEFAULT_INSTALL_ROOT) -> list[TargetSpec]:
    selected_modes = sum(bool(value) for value in (args.all, args.targets, args.target))
    if selected_modes > 1:
        raise PatchError("--all, --targets, and --target are mutually exclusive")
    if args.all:
        return list(manifest.targets.values())
    if args.targets:
        keys = [part.strip() for part in args.targets.split(",") if part.strip()]
        if not keys:
            raise PatchError("--targets requires at least one target key")
        unknown = [key for key in keys if key not in manifest.targets]
        if unknown:
            raise PatchError(f"unknown target key(s): {', '.join(unknown)}")
        if len(set(keys)) != len(keys):
            raise PatchError("--targets must not contain duplicate target keys")
        return [manifest.targets[key] for key in keys]
    default_main = official_topology_for_root(root)["main"][0]
    return [resolve_target(Path(args.target or default_main), manifest)]


def is_default_manifest_path(path: Path) -> bool:
    return normalized_path(path) == normalized_path(DEFAULT_MANIFEST)


def bind_manifest_to_root(manifest: Manifest, root: Path) -> Manifest:
    """Remap official target paths onto install root. Digest stays pinned to raw JSON."""
    if not is_default_manifest_path(manifest.path):
        raise PatchError("--root requires the trusted default manifest; custom manifests are read-only")
    root = normalize_install_root(root)
    topology = official_topology_for_root(root)
    if set(manifest.targets) != set(topology):
        raise PatchError("trusted manifest target keys do not match the official write topology")
    remapped: dict[str, TargetSpec] = {}
    for key, spec in manifest.targets.items():
        official_path, official_process = topology[key]
        if spec.process_name != official_process or Path(spec.path).name != official_process:
            raise PatchError(f"manifest target {key} process/name does not match the official write topology")
        remapped[key] = TargetSpec(
            key=spec.key,
            path=official_path,
            process_name=official_process,
            baseline_sha256=spec.baseline_sha256,
            patched_sha256=spec.patched_sha256,
            entries=spec.entries,
        )
    return Manifest(path=manifest.path, raw=manifest.raw, targets=remapped, digest=manifest.digest)


def enforce_official_write_topology(
    manifest: Manifest,
    targets: Iterable[TargetSpec],
    root: Path = DEFAULT_INSTALL_ROOT,
) -> None:
    if not is_default_manifest_path(manifest.path):
        raise PatchError("apply/rollback require the trusted default manifest; custom manifests are read-only")
    if manifest.digest != EXPECTED_OFFICIAL_MANIFEST_SHA256:
        raise PatchError("trusted default manifest content digest does not match the official pinned manifest")
    root = normalize_install_root(root)
    topology = official_topology_for_root(root)
    target_list = list(targets)
    if set(manifest.targets) != set(topology):
        raise PatchError("trusted manifest target keys do not match the official write topology")
    for spec in manifest.targets.values():
        official = topology.get(spec.key)
        if official is None:
            raise PatchError(f"target {spec.key} is not in the official write topology")
        official_path, official_process = official
        if normalized_path(spec.path) != normalized_path(official_path) or spec.process_name != official_process:
            raise PatchError(f"manifest target {spec.key} does not match the official write topology for root {root}")
    for spec in target_list:
        if spec.key not in topology:
            raise PatchError(f"selected target {spec.key} is not in the official write topology")


def classify_entry(data: bytes, entry: ByteEntry) -> EntryState:
    end = entry.offset + entry.length
    if end > len(data):
        actual = data[entry.offset:] if entry.offset < len(data) else b""
        return EntryState(entry, "third", hex_bytes(actual))
    actual = data[entry.offset:end]
    if actual == entry.patched:
        return EntryState(entry, "patched", hex_bytes(actual))
    if actual == entry.original:
        return EntryState(entry, "original", hex_bytes(actual))
    return EntryState(entry, "third", hex_bytes(actual))


def analyze_target(spec: TargetSpec, data: bytes | None = None) -> TargetAnalysis:
    path = spec.path
    if data is None:
        if not path.is_file():
            raise PatchError(f"target not found: {path}")
        data = path.read_bytes()
    entry_states = tuple(classify_entry(data, entry) for entry in spec.entries)
    third_states = [state for state in entry_states if state.state == "third"]
    if third_states:
        details = "; ".join(
            f"{state.entry.entry_id}@0x{state.entry.offset:X} actual={state.actual_hex or '<EOF>'}"
            for state in third_states
        )
        raise PatchError(f"{spec.key}: unexpected byte state: {details}")

    planned: list[dict[str, Any]] = []
    for state in entry_states:
        entry = state.entry
        if entry.mode == "patch" and state.state == "original":
            planned.append(
                {
                    "entry_id": entry.entry_id,
                    "description": entry.description,
                    "offset": entry.offset,
                    "offset_hex": f"0x{entry.offset:X}",
                    "length": entry.length,
                    "original": hex_bytes(entry.original),
                    "patched": hex_bytes(entry.patched),
                }
            )
        elif entry.mode not in {"patch", "validation"}:
            raise PatchError(f"{spec.key}:{entry.entry_id} has unsupported mode {entry.mode!r}")

    digest = sha256_bytes(data)
    fully_patched = all(state.state == "patched" for state in entry_states)
    fully_original = all(state.state == "original" or state.entry.mode == "validation" for state in entry_states)
    hash_allowed = (digest == spec.baseline_sha256 and fully_original) or (digest == spec.patched_sha256 and fully_patched)
    if not hash_allowed:
        raise PatchError(
            f"{spec.key}: SHA256 {digest} does not match the byte state; "
            "only exact baseline or exact patched hashes are accepted"
        )

    return TargetAnalysis(
        spec=spec,
        path=path,
        sha256=digest,
        size=len(data),
        entry_states=entry_states,
        planned_diffs=tuple(planned),
        fully_patched=fully_patched,
        hash_allowed=hash_allowed,
    )


def changed_ranges(before: bytes, after: bytes) -> list[tuple[int, bytes, bytes]]:
    if len(before) != len(after):
        raise PatchError("patched file size changed; refusing write")
    ranges: list[tuple[int, bytes, bytes]] = []
    index = 0
    while index < len(before):
        if before[index] == after[index]:
            index += 1
            continue
        start = index
        while index < len(before) and before[index] != after[index]:
            index += 1
        ranges.append((start, before[start:index], after[start:index]))
    return ranges


def planned_ranges(diffs: Iterable[dict[str, Any]]) -> list[tuple[int, bytes, bytes]]:
    return [
        (int(diff["offset"]), bytes_from_hex(diff["original"]), bytes_from_hex(diff["patched"]))
        for diff in diffs
    ]


def validate_exact_diff(before: bytes, after: bytes, expected: list[tuple[int, bytes, bytes]], label: str) -> None:
    for offset, original, patched in expected:
        end = offset + len(original)
        if before[offset:end] != original or after[offset:end] != patched:
            raise PatchError(f"{label}: planned diff bytes do not match at 0x{offset:X}")
    actual = changed_ranges(before, after)
    for offset, old, _new in actual:
        end = offset + len(old)
        if not any(offset >= plan_offset and end <= plan_offset + len(plan_old) for plan_offset, plan_old, _ in expected):
            raise PatchError(
                f"{label}: exact diff validation failed; "
                f"expected={format_ranges(expected)} actual={format_ranges(actual)}"
            )


def apply_patch_bytes(data: bytes, analysis: TargetAnalysis) -> bytes:
    patched = bytearray(data)
    by_id = {entry.entry_id: entry for entry in analysis.spec.entries}
    for diff in analysis.planned_diffs:
        entry = by_id[diff["entry_id"]]
        start = entry.offset
        end = start + entry.length
        if bytes(patched[start:end]) != entry.original:
            raise PatchError(f"{analysis.spec.key}:{entry.entry_id} changed while planning")
        patched[start:end] = entry.patched
    result = bytes(patched)
    expected = planned_ranges(analysis.planned_diffs)
    validate_exact_diff(data, result, expected, analysis.spec.key)
    return result


def format_ranges(ranges: Iterable[tuple[int, bytes, bytes]]) -> str:
    return ", ".join(f"0x{offset:X}:{hex_bytes(old)}->{hex_bytes(new)}" for offset, old, new in ranges)


def process_matches_target(process: ProcessInfo, target: TargetSpec, unique_process_names: set[str]) -> bool:
    if process.name.lower() != target.process_name.lower():
        return False
    if process.executable_path:
        process_path = normalized_path(Path(process.executable_path))
        target_path = normalized_path(target.path)
        if FORBIDDEN_PATH_MARKER in process_path.lower():
            return False
        return process_path == target_path
    return target.process_name.lower() in unique_process_names


def blocking_process_names_from_records(targets: Iterable[TargetSpec], processes: Iterable[ProcessInfo]) -> list[str]:
    target_list = list(targets)
    target_name_counts: dict[str, int] = {}
    for target in target_list:
        name = target.process_name.lower()
        target_name_counts[name] = target_name_counts.get(name, 0) + 1
    unique_process_names = {name for name, count in target_name_counts.items() if count == 1}

    running: list[str] = []
    for target in target_list:
        for process in processes:
            if process_matches_target(process, target, unique_process_names):
                running.append(target.process_name)
                break
    return sorted(set(running))


def query_windows_processes(process_names: Iterable[str]) -> list[ProcessInfo]:
    names = sorted(set(process_names))
    for name in names:
        validate_process_name(name)
    if not names or os.name != "nt":
        return []
    powershell = system_powershell_path()
    env = os.environ.copy()
    env["MUMU_PATCH_PROCESS_NAMES_JSON"] = json.dumps(names)
    command = (
        "$ErrorActionPreference='Stop'; "
        "$names=ConvertFrom-Json $env:MUMU_PATCH_PROCESS_NAMES_JSON; "
        "$items=Get-CimInstance Win32_Process | "
        "Where-Object { $names -contains $_.Name } | "
        "Select-Object Name,ExecutablePath; "
        "$items | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PatchError("process discovery timed out; refusing write") from exc
    except OSError as exc:
        raise PatchError(f"process discovery failed to launch; refusing write: {exc}") from exc
    if result.returncode != 0:
        raise PatchError(f"process discovery failed; refusing write: {result.stderr.strip() or result.stdout.strip()}")
    if not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PatchError("process discovery returned invalid JSON; refusing write") from exc
    if isinstance(payload, dict):
        payload = [payload]
    processes: list[ProcessInfo] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("Name"):
            continue
        executable_path = item.get("ExecutablePath")
        processes.append(ProcessInfo(str(item["Name"]), str(executable_path) if executable_path else None))
    return processes


def running_target_process_names(targets: Iterable[TargetSpec]) -> list[str]:
    target_list = list(targets)
    processes = query_windows_processes(target.process_name for target in target_list)
    return blocking_process_names_from_records(target_list, processes)


def require_processes_stopped(
    targets: Iterable[TargetSpec],
    checker: Callable[[Iterable[TargetSpec]], list[str]] = running_target_process_names,
) -> None:
    running = checker(targets)
    if running:
        names = ", ".join(running)
        raise PatchError(f"target processes are running; stop these process names and retry: {names}")


def unique_backup_dir(root: Path = BACKUP_ROOT) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = root / stamp
    suffix = 1
    while candidate.exists():
        candidate = root / f"{stamp}-{suffix:02d}"
        suffix += 1
    return candidate


def temp_replace_bytes(target: Path, data: bytes, expected_sha256: str, preflight: Callable[[], None] | None = None) -> None:
    parent = target.parent
    require_non_reparse_directory(parent, "target parent directory")
    handle: int | None = None
    tmp_path: Path | None = None
    try:
        handle, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(handle, "wb") as fh:
            handle = None
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        require_regular_non_reparse_file(tmp_path, "temporary replacement file")
        actual = sha256_file(tmp_path)
        if actual != expected_sha256:
            raise PatchError(f"temp write hash mismatch for {target}: expected {expected_sha256}, got {actual}")
        if preflight is not None:
            preflight()
        os.replace(tmp_path, target)
        tmp_path = None
    except PermissionError as exc:
        raise PatchError(f"target is locked; close related MuMu processes and retry: {target}") from exc
    finally:
        if handle is not None:
            os.close(handle)
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def create_backup(
    backup_dir: Path,
    manifest: Manifest,
    analyses: Iterable[TargetAnalysis],
    patched_hashes: dict[str, str],
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, Any]] = []
    for analysis in analyses:
        backup_file = backup_dir / analysis.path.name
        shutil.copy2(analysis.path, backup_file)
        backup_hash = sha256_file(backup_file)
        if backup_hash != analysis.sha256:
            raise PatchError(f"backup verification failed for {analysis.path}")
        files.append(
            {
                "target_key": analysis.spec.key,
                "target_path": str(analysis.path),
                "process_name": analysis.spec.process_name,
                "backup_file": backup_file.name,
                "pre_sha256": analysis.sha256,
                "post_sha256": patched_hashes[analysis.spec.key],
                "diffs": list(analysis.planned_diffs),
            }
        )
    (backup_dir / "manifest-snapshot.json").write_text(manifest_snapshot(manifest), encoding="utf-8")
    metadata = {
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "tool": "auto-patch-mumu.py",
        "files": files,
    }
    (backup_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return backup_dir


def restore_written_targets(backup_dir: Path, analyses: Iterable[TargetAnalysis]) -> list[str]:
    restored: list[str] = []
    for analysis in reversed(list(analyses)):
        backup_file = backup_dir / analysis.path.name
        if not backup_file.is_file():
            raise PatchError(f"transaction rollback file missing: {backup_file}")
        data = backup_file.read_bytes()
        if sha256_bytes(data) != analysis.sha256:
            raise PatchError(f"transaction rollback backup hash mismatch: {backup_file}")
        def preflight(analysis: TargetAnalysis = analysis) -> None:
            require_regular_non_reparse_file(analysis.path, f"{analysis.spec.key} transaction restore target")
            require_processes_stopped([analysis.spec])

        temp_replace_bytes(analysis.path, data, analysis.sha256, preflight=preflight)
        restored_hash = sha256_file(analysis.path)
        if restored_hash != analysis.sha256:
            raise PatchError(f"transaction rollback restore hash mismatch: {analysis.path}")
        restored.append(f"{analysis.spec.key}:{restored_hash}")
    return restored


def backup_file_from_metadata(backup_dir: Path, backup_name: str) -> Path:
    require_non_reparse_directory(backup_dir, "rollback backup directory")
    raw = Path(backup_name)
    if raw.name != backup_name or raw.is_absolute() or ".." in raw.parts:
        raise PatchError(f"unsafe rollback backup_file value: {backup_name!r}")
    resolved_dir = backup_dir.resolve(strict=False)
    resolved = (backup_dir / backup_name).resolve(strict=False)
    if resolved.parent != resolved_dir:
        raise PatchError(f"rollback backup file escapes backup directory: {backup_name!r}")
    require_regular_non_reparse_file(resolved, "rollback backup file")
    return resolved


def print_analysis(analysis: TargetAnalysis) -> None:
    status = "patched" if analysis.fully_patched else ("pending" if analysis.needs_patch else "verified")
    print(f"[{analysis.spec.key}] {analysis.path}")
    print(f"  sha256: {analysis.sha256}")
    print(f"  status: {status}; entries={len(analysis.entry_states)}; pending={len(analysis.planned_diffs)}")
    for diff in analysis.planned_diffs:
        print(f"  diff {diff['entry_id']} {diff['offset_hex']}: {diff['original']} -> {diff['patched']}")


def command_scan(targets: list[TargetSpec]) -> int:
    for spec in targets:
        print_analysis(analyze_target(spec))
    return 0


def command_verify(targets: list[TargetSpec]) -> int:
    failed: list[str] = []
    for spec in targets:
        analysis = analyze_target(spec)
        print_analysis(analysis)
        if analysis.needs_patch:
            failed.append(spec.key)
    if failed:
        raise PatchError(f"not fully patched: {', '.join(failed)}")
    return 0


def command_apply(
    targets: list[TargetSpec],
    manifest: Manifest,
    dry_run: bool,
    no_backup: bool = False,
    enforce_topology: bool = True,
    root: Path = DEFAULT_INSTALL_ROOT,
) -> int:
    if no_backup and not dry_run:
        raise PatchError("--no-backup is not allowed for direct-EXE writes")
    install_root = normalize_install_root(root)
    analyses: list[TargetAnalysis] = []
    patched_data: dict[str, bytes] = {}
    patched_hashes: dict[str, str] = {}

    for spec in targets:
        data = spec.path.read_bytes()
        analysis = analyze_target(spec, data)
        analyses.append(analysis)
        print_analysis(analysis)
        if analysis.needs_patch:
            patched = apply_patch_bytes(data, analysis)
            patched_hash = sha256_bytes(patched)
            if patched_hash != spec.patched_sha256:
                raise PatchError(
                    f"{spec.key}: computed patched SHA256 {patched_hash} does not match manifest patched_sha256 {spec.patched_sha256}"
                )
            patched_data[spec.key] = patched
            patched_hashes[spec.key] = patched_hash
        else:
            patched_hashes[spec.key] = analysis.sha256

    changing = [analysis for analysis in analyses if analysis.needs_patch]
    if not changing:
        print("[ok] no changes needed")
        return 0

    if dry_run:
        print("[dry-run] changes were not written")
        return 0

    if enforce_topology:
        enforce_official_write_topology(manifest, [analysis.spec for analysis in changing], root=install_root)
    for analysis in changing:
        require_regular_non_reparse_file(analysis.path, f"{analysis.spec.key} target")
    require_processes_stopped(analysis.spec for analysis in changing)
    backup_dir = create_backup(unique_backup_dir(backup_root_for(install_root)), manifest, changing, patched_hashes)
    written: list[TargetAnalysis] = []
    try:
        for analysis in changing:
            def preflight(analysis: TargetAnalysis = analysis) -> None:
                if enforce_topology:
                    enforce_official_write_topology(manifest, [analysis.spec], root=install_root)
                require_regular_non_reparse_file(analysis.path, f"{analysis.spec.key} target")
                require_processes_stopped([analysis.spec])
                current_hash = sha256_file(analysis.path)
                if current_hash != analysis.sha256:
                    raise PatchError(f"{analysis.spec.key}: target changed before replace; refusing write")

            current = analysis.path.read_bytes()
            if sha256_bytes(current) != analysis.sha256:
                raise PatchError(f"{analysis.spec.key}: target changed after backup; refusing write")
            temp_replace_bytes(analysis.path, patched_data[analysis.spec.key], patched_hashes[analysis.spec.key], preflight=preflight)
            written.append(analysis)
            post = sha256_file(analysis.path)
            if post != patched_hashes[analysis.spec.key]:
                raise PatchError(f"{analysis.spec.key}: post-write hash mismatch")
    except Exception as exc:
        print(f"[backup] preserved at {backup_dir}", file=sys.stderr)
        if written:
            try:
                restored = restore_written_targets(backup_dir, written)
                print(f"[transaction-rollback] restored {', '.join(restored)}", file=sys.stderr)
            except Exception as rollback_exc:
                raise PatchError(f"apply failed: {exc}; transaction rollback also failed: {rollback_exc}") from exc
        raise

    print(f"[backup] {backup_dir}")
    for analysis in changing:
        print(f"[target-sha256] {analysis.spec.key} {patched_hashes[analysis.spec.key]}")
    return 0


@dataclass(frozen=True)
class RollbackPlan:
    spec: TargetSpec
    target: Path
    backup_file: Path
    restore_data: bytes
    current_data: bytes
    current_hash: str
    noop: bool


def build_rollback_plans(
    backup_dir: Path,
    manifest: Manifest,
    files: list[dict[str, Any]],
    checker: Callable[[Iterable[TargetSpec]], list[str]] = running_target_process_names,
) -> list[RollbackPlan]:
    specs: list[TargetSpec] = []
    plans: list[RollbackPlan] = []
    seen_keys: set[str] = set()
    for item in files:
        key = item.get("target_key")
        if not isinstance(key, str):
            raise PatchError(f"rollback target key is not in current manifest: {key}")
        spec = manifest.targets.get(key)
        if spec is None:
            raise PatchError(f"rollback target key is not in current manifest: {key}")
        if key in seen_keys:
            raise PatchError(f"duplicate rollback target_key in metadata: {key}")
        seen_keys.add(key)
        metadata_path = Path(item.get("target_path", ""))
        if normalized_path(metadata_path) != normalized_path(spec.path):
            raise PatchError(f"rollback metadata target_path mismatch for {spec.key}")
        if item.get("process_name") != spec.process_name:
            raise PatchError(f"rollback metadata process_name mismatch for {spec.key}")
        if item.get("backup_file") != spec.path.name:
            raise PatchError(f"rollback metadata backup_file mismatch for {spec.key}")
        if item.get("pre_sha256", "").upper() != spec.baseline_sha256:
            raise PatchError(f"rollback metadata pre_sha256 mismatch for {spec.key}")
        if item.get("post_sha256", "").upper() != spec.patched_sha256:
            raise PatchError(f"rollback metadata post_sha256 mismatch for {spec.key}")
        specs.append(spec)

    require_processes_stopped(specs, checker=checker)
    for item in files:
        spec = manifest.targets[item["target_key"]]
        backup_file = backup_file_from_metadata(backup_dir, item["backup_file"])
        restore_data = backup_file.read_bytes()
        if sha256_bytes(restore_data) != spec.baseline_sha256:
            raise PatchError(f"rollback backup hash mismatch: {backup_file}")
        require_regular_non_reparse_file(spec.path, f"{spec.key} rollback target")
        current_data = spec.path.read_bytes()
        current_hash = sha256_bytes(current_data)
        if current_hash == spec.baseline_sha256:
            plans.append(RollbackPlan(spec, spec.path, backup_file, restore_data, current_data, current_hash, True))
            continue
        if current_hash != spec.patched_sha256:
            raise PatchError(
                f"rollback target drift for {spec.path}: expected current post hash {spec.patched_sha256} "
                f"or pre hash {spec.baseline_sha256}, got {current_hash}"
            )
        plans.append(RollbackPlan(spec, spec.path, backup_file, restore_data, current_data, current_hash, False))
    return plans


def recover_rollback_writes(
    written: list[RollbackPlan],
    checker: Callable[[Iterable[TargetSpec]], list[str]] = running_target_process_names,
) -> list[str]:
    recovered: list[str] = []
    for plan in reversed(written):
        def preflight(plan: RollbackPlan = plan) -> None:
            require_regular_non_reparse_file(plan.target, f"{plan.spec.key} rollback recovery target")
            require_processes_stopped([plan.spec], checker=checker)
            if sha256_file(plan.target) != plan.spec.baseline_sha256:
                raise PatchError(f"{plan.spec.key}: rollback recovery target changed before replace")

        temp_replace_bytes(plan.target, plan.current_data, plan.current_hash, preflight=preflight)
        restored = sha256_file(plan.target)
        if restored != plan.current_hash:
            raise PatchError(f"{plan.spec.key}: rollback recovery hash mismatch")
        recovered.append(f"{plan.spec.key}:{restored}")
    return recovered


def command_rollback(
    backup_dir: Path,
    manifest: Manifest,
    checker: Callable[[Iterable[TargetSpec]], list[str]] = running_target_process_names,
    enforce_topology: bool = True,
    root: Path = DEFAULT_INSTALL_ROOT,
) -> int:
    install_root = normalize_install_root(root)
    metadata_path = backup_dir / "metadata.json"
    if not metadata_path.is_file():
        raise PatchError(f"rollback metadata not found: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    files = metadata.get("files", [])
    if not files:
        raise PatchError(f"rollback metadata has no files: {metadata_path}")

    if enforce_topology:
        specs_for_topology: list[TargetSpec] = []
        for item in files:
            key = item.get("target_key")
            if isinstance(key, str) and key in manifest.targets:
                specs_for_topology.append(manifest.targets[key])
        enforce_official_write_topology(manifest, specs_for_topology, root=install_root)

    plans = build_rollback_plans(backup_dir, manifest, files, checker=checker)

    written: list[RollbackPlan] = []
    try:
        for plan in plans:
            if plan.noop:
                print(f"[rollback] {plan.target} already restored sha256={plan.current_hash}")
                continue

            def preflight(plan: RollbackPlan = plan) -> None:
                if enforce_topology:
                    enforce_official_write_topology(manifest, [plan.spec], root=install_root)
                require_regular_non_reparse_file(plan.target, f"{plan.spec.key} rollback target")
                require_processes_stopped([plan.spec], checker=checker)
                if sha256_file(plan.target) != plan.current_hash:
                    raise PatchError(f"{plan.spec.key}: rollback target changed before replace; refusing write")

            temp_replace_bytes(plan.target, plan.restore_data, plan.spec.baseline_sha256, preflight=preflight)
            restored = sha256_file(plan.target)
            if restored != plan.spec.baseline_sha256:
                raise PatchError(f"rollback restore hash mismatch: {plan.target}")
            written.append(plan)
            print(f"[rollback] {plan.target} sha256={restored}")
    except Exception as exc:
        if written:
            try:
                recovered = recover_rollback_writes(written, checker=checker)
                print(f"[rollback-recovery] restored {', '.join(recovered)}", file=sys.stderr)
            except Exception as recovery_exc:
                raise PatchError(f"rollback failed: {exc}; rollback recovery also failed: {recovery_exc}") from exc
        raise
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Version-locked direct EXE patcher for local MuMuPlayer binaries.")
    parser.add_argument("command", nargs="?", choices=("scan", "dry-run", "apply", "verify", "rollback"), default=None)
    parser.add_argument("--target")
    parser.add_argument("--all", action="store_true", help="operate on all allowlisted current-build targets")
    parser.add_argument("--targets", help="comma-separated target keys, for example: main,service")
    parser.add_argument("--root", help="MuMuPlayer install root; default H:\\MuMuPlayer")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--backup", help="backup directory for rollback")
    parser.add_argument("--dry-run", action="store_true", help="compatibility alias for the dry-run command")
    parser.add_argument("--no-backup", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or ("dry-run" if args.dry_run else "apply")
    if args.dry_run and args.command not in (None, "dry-run", "apply"):
        raise PatchError("--dry-run can only be combined with apply or the default command")

    install_root = normalize_install_root(Path(args.root) if args.root else None)
    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    if args.root and not is_default_manifest_path(manifest_path):
        raise PatchError("--root requires the trusted default manifest; custom manifests are read-only")
    if is_default_manifest_path(manifest_path):
        manifest = bind_manifest_to_root(manifest, install_root)
    try:
        if command == "rollback":
            if not args.backup:
                raise PatchError("rollback requires --backup <backup-dir>")
            return command_rollback(Path(args.backup), manifest, root=install_root)
        targets = selected_targets(args, manifest, root=install_root)
        if command == "scan":
            return command_scan(targets)
        if command == "verify":
            return command_verify(targets)
        if command == "dry-run":
            return command_apply(targets, manifest, dry_run=True, no_backup=args.no_backup, root=install_root)
        if command == "apply":
            return command_apply(
                targets, manifest, dry_run=args.dry_run, no_backup=args.no_backup, root=install_root
            )
        raise PatchError(f"unsupported command: {command}")
    except PatchError:
        raise
    except PermissionError as exc:
        raise PatchError(f"permission denied: {exc}") from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001 - CLI should print a concise failure.
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1)
