#!/usr/bin/env python3
"""Local MuMuPlayer crackme patch helper.

This script patches only a selected MuMuNxMain.exe. It never searches user
profile data and never uploads anything. Keep it private and use only in the
authorized local crackme environment.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import shutil
import sys
from pathlib import Path


MEMBER_ORIGINAL = "B8 04 00 00 00 E9 ?? ?? ?? ?? 85 C0 0F 85 ?? ?? ?? ?? 48 8B B9 20 06 00 00"
MEMBER_PATCHED = "B8 01 00 00 00 E9 ?? ?? ?? ?? 85 C0 0F 85 ?? ?? ?? ?? 48 8B B9 20 06 00 00"
MEMBER_REWRITE = bytes.fromhex("B8 01 00 00 00")

DISPLAY_REWRITES = (
    (b"%1 expired\x00", b"VIP\x00"),
    (b"have expired\x00", b"VIP\x00"),
)


def parse_pattern(pattern: str) -> list[int | None]:
    parts: list[int | None] = []
    for token in pattern.split():
        parts.append(None if token == "??" else int(token, 16))
    return parts


def find_pattern(data: bytes | bytearray, pattern: list[int | None]) -> list[int]:
    hits: list[int] = []
    plen = len(pattern)
    end = len(data) - plen + 1
    for offset in range(max(end, 0)):
        for index, expected in enumerate(pattern):
            if expected is not None and data[offset + index] != expected:
                break
        else:
            hits.append(offset)
    return hits


def replace_unique(data: bytearray, original: bytes, replacement: bytes, label: str) -> bool:
    hits = find_all(data, original)
    padded = replacement + b"\x00" * (len(original) - len(replacement))
    patched_hits = find_all(data, padded)

    if len(hits) == 1:
        offset = hits[0]
        data[offset : offset + len(original)] = padded
        print(f"[patch] {label}: 0x{offset:X}")
        return True

    if not hits and patched_hits:
        print(f"[skip] {label}: already patched")
        return False

    if not hits:
        raise RuntimeError(f"{label}: original text was not found")
    raise RuntimeError(f"{label}: ambiguous original text matches: {len(hits)}")


def find_all(data: bytes | bytearray, needle: bytes) -> list[int]:
    hits: list[int] = []
    start = 0
    while True:
        hit = data.find(needle, start)
        if hit < 0:
            return hits
        hits.append(hit)
        start = hit + 1


def backup_target(target: Path) -> Path:
    root = target.parent.parent
    backup_dir = root / "_ad_vip_tools"
    if not backup_dir.is_dir():
        backup_dir = target.parent
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"MuMuNxMain.exe.bak_auto_{stamp}"
    shutil.copy2(target, backup)
    return backup


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def patch_member_type(data: bytearray) -> bool:
    original = parse_pattern(MEMBER_ORIGINAL)
    patched = parse_pattern(MEMBER_PATCHED)
    original_hits = find_pattern(data, original)
    patched_hits = find_pattern(data, patched)

    if len(original_hits) == 1 and not patched_hits:
        offset = original_hits[0]
        data[offset : offset + len(MEMBER_REWRITE)] = MEMBER_REWRITE
        print(f"[patch] member type: 0x{offset:X}")
        return True

    if not original_hits and len(patched_hits) == 1:
        print("[skip] member type: already patched")
        return False

    raise RuntimeError(
        "member type pattern mismatch: "
        f"original_hits={len(original_hits)}, patched_hits={len(patched_hits)}"
    )


def patch_file(target: Path, dry_run: bool, no_backup: bool) -> int:
    if target.name.lower() != "mumunxmain.exe":
        raise RuntimeError(f"refusing unexpected target name: {target}")
    if not target.is_file():
        raise RuntimeError(f"target not found: {target}")

    data = bytearray(target.read_bytes())
    changed = False
    changed |= patch_member_type(data)
    for original, replacement in DISPLAY_REWRITES:
        changed |= replace_unique(data, original, replacement, original.decode("ascii", "ignore"))

    if not changed:
        print("[ok] no changes needed")
        print(f"[sha256] {sha256(target)}")
        return 0

    if dry_run:
        print("[dry-run] changes were not written")
        return 0

    backup = None if no_backup else backup_target(target)
    try:
        target.write_bytes(data)
    except PermissionError as exc:
        raise RuntimeError("target is locked; close MuMuNxMain.exe and retry") from exc

    if backup:
        print(f"[backup] {backup}")
        print(f"[backup-sha256] {sha256(backup)}")
    print(f"[target-sha256] {sha256(target)}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Patch local MuMuNxMain.exe by signature.")
    parser.add_argument("--target", default=r"H:\MuMuPlayer\nx_main\MuMuNxMain.exe")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args(argv)

    try:
        return patch_file(Path(args.target), args.dry_run, args.no_backup)
    except Exception as exc:  # noqa: BLE001 - CLI should print a concise failure.
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
