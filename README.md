# MuMu Local Patch Tools

Private, sanitized helper package for the local MuMuPlayer crackme environment.

This repository intentionally contains only text tooling. Do not add or commit official binaries, patched binaries, backups, logs, AppData files, account cache files, tokens, or screenshots containing account data.

## Files

- `auto-patch-mumu.py` is a version-locked direct-EXE patcher for the current local `MuMuPlayer` binaries.
- `mumu-vip-manifest.json` is the machine-readable allowlist, hash gate, and byte-diff manifest for the current build.
- `start-mumu-patched.cmd` patches/verifies local Main and Service first, then starts local Main. It does not attempt RemoteService.
- `tests/test_auto_patch_mumu.py` uses only synthetic temporary files and never touches official binaries.
- `.gitignore` uses a default-deny allowlist so accidental binaries and logs are not committed.

## Direct-EXE Usage

Scan the default main target without writing:

```cmd
python auto-patch-mumu.py scan --target "H:\MuMuPlayer\nx_main\MuMuNxMain.exe"
```

Scan all allowlisted current-build targets:

```cmd
python auto-patch-mumu.py scan --all
```

Dry-run all current-build targets. This computes the exact byte ranges but does not write or create backups:

```cmd
python auto-patch-mumu.py dry-run --all
```

Apply the default main target:

```cmd
python auto-patch-mumu.py --target "H:\MuMuPlayer\nx_main\MuMuNxMain.exe"
```

Apply Main and Service together as one transaction. This is the launcher path and does not include RemoteService:

```cmd
python auto-patch-mumu.py apply --targets main,service
```

Verify that selected targets are fully patched. Main and Service should verify after apply; Remote remains pending until the elevated service-stop path is handled:

```cmd
python auto-patch-mumu.py verify --targets main,service
```

## Current Local State

- Main (`H:\MuMuPlayer\nx_main\MuMuNxMain.exe`) is patched. Post SHA-256: `95DD3F2C8CE6FAE61258E8ADA2610592A87DC3C970CFD6FA8BF7C74A4C5E1309`. Backup: `H:\MuMuPlayer\_ad_vip_tools\backups\direct-exe\20260713-215916`.
- Main coverage is 28 new manifest patches plus 3 validation-only prior states: 2099 expiry, member/device/type status, trial-used/inactive state, cache parse/serialize, VIP theme default, hidden trial UI, and expiry popup suppression.
- Service (`H:\MuMuPlayer\nx_main\MuMuNxService.exe`) is patched. Post SHA-256: `1C41A8DF731C7A0D0EFADC4681ACA6014FE1D2C2BD0D1EDFD2EB03DB8DFDFF74`. Backup: `H:\MuMuPlayer\_ad_vip_tools\backups\direct-exe\20260713-215919`.
- Service coverage is 18 new patch entries plus 1 validation-only existing local member return: `v1/member/trial` to `v1/member/local`, cache parse/serialize, and valid flag handling.
- RemoteService (`H:\MuMuPlayer\nx_main\MuMuRemoteService.exe`) is not patched. SHA-256 remains `7DE5204AFF853E2FB93617B96B99277836D23B8C89466AC716A94B6559F58B65`. predicted/expected post SHA-256 (not current): `14D86EA7A958FBE19E7383F33D5622E5441363955BD6C529FC1385D16FC9BB1D`. IDA-verified pending offsets are `0x323BA`, `0x30660`, and `0x30FA0`.
- `H:\MuMuPlayerGlobal` remains untouched.

## RemoteService Blocker

RemoteService still requires an elevated, path-guarded service stop before patching. Normal `Stop-Service`, `Stop-Process`, and `sc` attempts returned access denied. Do not claim RemoteService is patched until its hash and manifest entries verify after an elevated stop/apply cycle.

Safe elevated stop snippet:

```powershell
$svc = Get-CimInstance Win32_Service -Filter "Name='MuMuRemoteService'"
if ($svc.PathName -eq '"H:\MuMuPlayer\nx_main\MuMuRemoteService.exe" --service') {
    Stop-Service -Name $svc.Name -ErrorAction Stop
} else {
    throw "Refuse to stop unexpected service path: $($svc.PathName)"
}
```

Rollback from a specific timestamped backup:

```cmd
python auto-patch-mumu.py rollback --backup "H:\MuMuPlayer\_ad_vip_tools\backups\direct-exe\YYYYMMDD-HHMMSS"
```

Current exact rollback commands:

```cmd
python auto-patch-mumu.py rollback --backup "H:\MuMuPlayer\_ad_vip_tools\backups\direct-exe\20260713-215916"
python auto-patch-mumu.py rollback --backup "H:\MuMuPlayer\_ad_vip_tools\backups\direct-exe\20260713-215919"
```

## Launcher Compatibility

Run with the default local path:

```cmd
start-mumu-patched.cmd
```

Or pass the same explicit local main target:

```cmd
start-mumu-patched.cmd "H:\MuMuPlayer\nx_main\MuMuNxMain.exe"
```

The launcher refuses any target except `H:\MuMuPlayer\nx_main\MuMuNxMain.exe`, then applies and verifies Main plus Service before starting Main. It resolves Python from standard install paths under `%LocalAppData%` / `%ProgramFiles%` (3.12, then 3.13, then 3.11), not from PATH and not from a machine-specific user path:

```cmd
"%PYTHON_EXE%" auto-patch-mumu.py apply --targets main,service
"%PYTHON_EXE%" auto-patch-mumu.py verify --targets main,service
```

## Other Machine Checklist

Use this package on another Windows account/PC only when all of the following hold:

1. Product version is still `6.2.5.0` / channel `nochannel-mumu12`.
2. Install root is exactly `H:\MuMuPlayer` (paths and official topology are version-locked).
3. Close local `MuMuNxMain.exe` and `MuMuNxService.exe` before apply; do not touch `H:\MuMuPlayerGlobal`.
4. Install CPython 3.11+ for the current user (3.12 preferred).
5. Clone this repo, then either run `start-mumu-patched.cmd` or:

```cmd
python auto-patch-mumu.py scan --all
python auto-patch-mumu.py apply --targets main,service
python auto-patch-mumu.py verify --targets main,service
```

6. If baseline hashes do not match, stop. Re-lock offsets/hashes for that build; do not force this manifest.
7. RemoteService remains optional and needs an elevated path-guarded service stop before apply.
8. Do not block login/account domains. Do not run any Frida scripts from other tool folders unless you intentionally use a SAFE diagnostic.

## Safety Notes

- The only allowlisted targets are `H:\MuMuPlayer\nx_main\MuMuNxMain.exe`, `MuMuNxService.exe`, and `MuMuRemoteService.exe`.
- Any `H:\MuMuPlayerGlobal\...` target is refused before analysis.
- Every manifest entry must be exactly the original bytes or the patched bytes. Any third state aborts.
- Apply and rollback writes require the trusted default `mumu-vip-manifest.json`, its pinned canonical content digest, and the exact hardcoded Main/Service/Remote key, path, and process-name topology under `H:\MuMuPlayer\nx_main`. Custom manifests are read-only only: scan, verify, and dry-run.
- The listed baseline SHA-256 hashes are accepted only with exact original byte states. The listed patched SHA-256 hashes are accepted only with exact patched byte states. Unknown fully-patched hashes are refused.
- Apply and rollback require target processes to be stopped. Windows process discovery is executable-path aware and fails closed: PowerShell is resolved through `GetSystemDirectoryW` to the real Windows system directory, and launch, timeout, nonzero exit, invalid JSON, or missing system PowerShell aborts the write instead of assuming no process is running.
- Writes use exclusive unpredictable temp-file creation in the target directory, parent/target/temp non-reparse checks, flush/fsync, exact diff validation, computed post-hash verification against manifest `patched_sha256`, final pre-replace target/process/hash checks, `os.replace`, and post-write hash verification.
- Backups are created under `H:\MuMuPlayer\_ad_vip_tools\backups\direct-exe\<timestamp>` and include original files, `metadata.json`, and `manifest-snapshot.json`.
- Rollback is two-phase and treats metadata as untrusted: target key/path/process/backup filename/pre-hash/post-hash must reconcile with the current manifest, all restore plans are validated before any write, backup bytes must equal the manifest baseline hash, backup paths must not traverse or be reparse points, and the live target must equal manifest `patched_sha256` or already-restored `baseline_sha256`. If a later restore fails, already-restored targets are transactionally recovered to their exact pre-rollback bytes when possible. Any third hash is drift and is refused.
- After MuMu updates, do not force this manifest onto new binaries. Re-lock hashes and offsets first.
- Do not block login or account domains. Prior `api.mumu.nie.netease.com` and `mumu.nie.netease.com` blocks were rolled back and must remain unblocked.

## Tests

Run unit tests from this package:

```cmd
python -m unittest discover -s tests -v
```

The tests create temporary synthetic files only. They do not read or write official MuMu binaries.

Latest recorded validation:

- 38 unit tests passed; 2 symlink/reparse tests were skipped because symlink creation was unavailable in the environment.
- Manifest scan reports Main and Service fully patched; RemoteService unchanged and pending. Main and Service verification pass individually.
- Main PID `62280` and Service PID `36804` remained Responding for 90 seconds after patching; RemoteService PID `27120` was still running and unchanged.
- `last_crash` stayed `2026-07-12T12:57:18.635Z`.
- Login, avatar panel, visual 2099 display, and device-side `memberTypeNotification` still require human UI confirmation.

## Rollback Contents

Each backup directory contains:

```text
metadata.json
manifest-snapshot.json
MuMuNxMain.exe / MuMuNxService.exe / MuMuRemoteService.exe as applicable
```

`metadata.json` records pre/post hashes and the exact byte diffs for each backed up file. Rollback restores only from a specified backup directory and verifies the restored SHA-256 hashes.
