# MuMu Local Patch Tools

Private, sanitized helper package for the local MuMuPlayer crackme environment.

This repository intentionally contains only text tooling. Do not add or commit official binaries, patched binaries, backups, logs, AppData files, account cache files, tokens, or screenshots containing account data.

## Files

- `auto-patch-mumu.py` scans `MuMuNxMain.exe`, creates a timestamped backup, and applies known local patches when needed.
- `start-mumu-patched.cmd` runs the patcher first, then starts MuMu.
- `.gitignore` uses a default-deny allowlist so accidental binaries and logs are not committed.

## Usage

Run with the default local path:

```cmd
start-mumu-patched.cmd
```

Or pass an explicit target:

```cmd
start-mumu-patched.cmd "H:\MuMuPlayer\nx_main\MuMuNxMain.exe"
```

Run the patcher directly:

```cmd
python auto-patch-mumu.py --target "H:\MuMuPlayer\nx_main\MuMuNxMain.exe"
```

Dry run:

```cmd
python auto-patch-mumu.py --target "H:\MuMuPlayer\nx_main\MuMuNxMain.exe" --dry-run
```

## Safety Notes

- Close `H:\MuMuPlayer\nx_main\MuMuNxMain.exe` before patching.
- Do not kill or patch `H:\MuMuPlayerGlobal\nx_main\MuMuNxMain.exe` by mistake.
- The patcher refuses ambiguous pattern matches.
- The patcher creates a backup before writing unless `--no-backup` is used.
- After MuMu updates, run the patcher again before starting the client.

## Rollback

Copy the newest backup from `H:\MuMuPlayer\_ad_vip_tools` back to:

```text
H:\MuMuPlayer\nx_main\MuMuNxMain.exe
```

The backup file names start with:

```text
MuMuNxMain.exe.bak_auto_
```
