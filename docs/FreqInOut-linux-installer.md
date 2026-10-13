# CommsPlan 2.0 Linux Installer Reference

> CommsPlan 2.0.5 Linux installer documentation. Historical native replacement
> remains bounded by the platform procedures in [CommsPlan-migration.md];
> package installation does not by itself prove station continuity or rollback.

`install-commsplan.sh` is the canonical Linux entry point. It accepts
`setup` (the default), `update`, `repair`, `desktop`, `uninstall`, and `profile`, then
forwards supported extra flags such as `--dir`, `--config-root`, and
`--dry-run` to the established installer. The legacy
`install_FreqInOut_linux.sh` remains available for explicit recovery and
support instructions.

## Canonical commands

```bash
cd "$HOME/Radio/FreqInOut"
./install-commsplan.sh setup
```

With no `--dir`, the wrapper uses its own checkout as the application directory.
An existing checkout such as `~/Radio/FreqInOut` is therefore a valid target.
The command names and their boundaries are:

- `setup` runs the verified Python installer, creating or repairing `.venv`,
  checking the installation receipt, making the profile backup, and then
  refreshing the desktop entry and local launchers.
- `update` follows the checkout's current branch and upstream, and fails closed
  if that upstream is missing. It does not silently switch repositories.
- `repair` reruns the verified environment and profile checks without
  recloning, then refreshes desktop integration.
- `desktop` refreshes local launchers and icons only. It does not run Git or
  pip and works with a non-Git checkout.
- `uninstall` removes the app-owned source/launcher integration after its
  prompts and leaves the operator profile in place.

Examples:

```bash
./install-commsplan.sh setup --dir "$HOME/Radio/FreqInOut"
./install-commsplan.sh update --dir "$HOME/Radio/FreqInOut" --dry-run
./install-commsplan.sh repair --dir "$HOME/Radio/FreqInOut"
./install-commsplan.sh desktop --dir "$HOME/Radio/FreqInOut"
./install-commsplan.sh uninstall --dir "$HOME/Radio/FreqInOut" --dry-run
```

Desktop integration is account-wide: setup and repair manage the shared
`~/.local/bin/commsplan`, `~/.local/bin/freqinout`, and
`freqinout.desktop` entry. The installer refuses to overwrite a command alias
owned by another installation. For side-by-side testing beside an existing
desktop-integrated install, use the direct `install_freqinout.py` plus
`start-commsplan.sh` path with a dedicated `FREQINOUT_CONFIG_DIR`, or use a
separate OS account.

The legacy installer still:

- validates Python 3.10 through 3.13;
- recognizes `apt`, `dnf`, `yum`, `pacman`, and `zypper`;
- installs applicable system dependencies when approved;
- creates or repairs the virtual environment;
- installs the runtime requirements;
- creates the desktop entry and icons; and
- writes detailed output to `~/freqinout-install.log` by default.

The installer prepares the application but does not silently finalize the
single-radio-to-2.0 configuration migration. CommsPlan owns the informed backup,
review, and confirmation flow on first launch.

For a frozen package, first startup performs a separate profile guard before
station schema or database writes. It records the actual verified cold-copy path
in `<PROFILE_ROOT>/.commsplan-packaged-startup.json`. The copy includes the raw
`config` tree and any pending SQLite WAL or journal sidecars; SQLite is checked
on a disposable copy so the raw backup is not mutated. This package-startup
backup covers the `config` tree only; it does not include credentials, logs,
caches, or companion-application data. Repeated launches of the same package
version reuse the receipt after manifest identity checks. A fresh profile is
recorded without a backup until data appears; the first later startup with both
station databases captures and records that backup. A missing receipt is normal
on first launch. Once a receipt exists, a malformed, changed, or linked receipt,
a missing or mismatched recorded manifest/backup, or unsafe station databases
stops startup for recovery.

## Profile-directory transition

For an explicit, backed-up move to `~/.commsplan`, use `profile plan` followed
by the reviewed `profile copy` procedure in the
[Linux profile transition guide](CommsPlan-migration.md#guided-linux-profile-transition-to-commsplan).
The copy does not switch the desktop profile. Verify it with a one-shot launch
before running `setup --config-root` to remember the new root.

Historical Linux 1.2.8 source installations retain the source installation path
and informed station-upgrade flow. Identify the actual profile used by that
checkout, preserve the old application and verified backup, and use the source
procedure before accepting CommsPlan. A native Linux package install is not a
substitute for that historical source path.

## Common commands

Install or repair an explicitly selected checkout:

```bash
./install-commsplan.sh setup --dir "$HOME/Apps/FreqInOut"
```

Bind the launcher to an intentionally separate profile:

```bash
./install-commsplan.sh setup \
  --dir "$HOME/Apps/FreqInOut" \
  --config-root "$HOME/.freqinout-field"
```

Review without changing the system:

```bash
./install-commsplan.sh setup --dry-run
```

Use a supplied repository and branch explicitly:

```bash
./install-commsplan.sh update \
  --dir "$HOME/Apps/FreqInOut" \
  --repo "$REPO_URL_FROM_SUPPORT" \
  --branch "$BRANCH_FROM_SUPPORT"
```

The `--repo` and `--branch` options are explicit overrides for the compatibility
installer. Keep them when support assigns a specific branch; ordinary updates
should use the checkout's current branch and upstream.

For the legacy path, use the same options after the script name:

```bash
bash install_FreqInOut_linux.sh --offline --dir "$HOME/FreqInOut"
```

## Update behavior

Close CommsPlan, FreqInOut and companion radio applications before updating, then rerun
`./install-commsplan.sh update` with the same `--dir` and `--config-root`
values used originally. The canonical update follows the current branch and
its upstream; a missing upstream is an error. The compatibility installer defaults to
the public `https://github.com/N1MAG/CommsPlan.git` repository and `main` when
it is asked to clone or update without an explicit source.

For unattended policy control:

```bash
bash install_FreqInOut_linux.sh \
  --yes \
  --on-dirty fail \
  --on-running fail \
  --on-non-git fail
```

Available policies are:

- `--on-dirty prompt|stash|skip|fail`
- `--on-running prompt|skip|fail`
- `--on-non-git prompt|replace|skip|fail`

An explicit repository or branch can be selected with `--repo` and `--branch`.
Ordinary operators should retain the public defaults unless support supplies a
specific recovery instruction.

## Profile ownership and backups

Profile selection follows this order: an explicit `--config-root` or
`FREQINOUT_CONFIG_DIR`, the `profile_root` remembered in the verified install
receipt, then the standard Linux profile under `~/.freqinout`. The selected
root is persisted and honored by the canonical launcher on later starts. The
compatibility storage name is intentional: there is no automatic move to
`~/.commsplan`, and no database is renamed.

Compatibility identities remain historical by design: profiles stay under
`~/.freqinout` unless explicitly overridden; databases remain `freqinout.db`
and `freqinout_nets.db`; the Debian package ID remains `freqinout`; and both
`freqinout` and `commsplan` commands remain available as shared command routes.

Historical 1.2.8 installations may have used an app-local `config` directory,
a sibling runtime directory, or another explicit root. Identify the actual
profile used by that installation and pass it with `--config-root` before the
first CommsPlan launch. The installer does not guess between multiple
historical profiles, merge them, or rename one automatically.

The installer checks for a running CommsPlan process and protects dirty Git
worktrees. It creates rollback state for managed launcher, desktop, and icon
files. The verified Python installer quarantines an unusable `.venv`; a healthy
dependency environment is not guaranteed to have a rollback copy. Installer
backups and CommsPlan migration backups are recovery points, not working
directories.

The source installer and the frozen-package startup guard are separate recovery
records. For source installation, retain the exact `Verified pre-install station
backup:` path and the `.freqinout-install-verified.json` receipt in the selected
checkout. For a package, retain the `<PROFILE_ROOT>/.commsplan-packaged-startup.json`
receipt and the backup directory it names. Always use the actual path reported by
the tool; Windows may retain a verified temporary backup name when a rename is
blocked.

Before restoring:

1. close CommsPlan and companion applications;
2. retain the failed installation and logs;
3. copy the current profile to a separate holding location;
4. inspect the backup manifest or archive; and
5. validate restored data under a separate `FREQINOUT_CONFIG_DIR` before
   replacing a live profile.

The `--repair` option repairs the application environment. It is not permission
to overwrite production configuration.

## Running CommsPlan

After installation:

- use the desktop application menu and search for **CommsPlan**; or
- run `commsplan` in a terminal.

`freqinout` remains a compatibility alias for existing scripts and pinned
launchers. Both commands select the same CommsPlan application and profile.

If a desktop entry or icon does not appear, log out and back in, then rerun the
installer with the same paths if necessary.

## Logs

The installer log defaults to `~/freqinout-install.log`. Select another path
with `--log-file`.

Application logs are in the active CommsPlan profile root:

- `freqinout.log`
- `perf_metrics.log`
- generated `fio_cpu_hotspot_*.txt` files
- generated UI-hang evidence, when present

## Uninstall

Review uninstall actions without changing the system:

```bash
./install-commsplan.sh uninstall --dir "$HOME/FreqInOut" --dry-run
```

Run the uninstaller:

```bash
./install-commsplan.sh uninstall --dir "$HOME/FreqInOut"
```

The uninstaller can remove app-owned launchers, the desktop entry, and installed
icons. It intentionally leaves the operator profile and radio data in place. It
may retain the source/application folder when it contains `config`, `runtime`,
or `VarAC.ini`; archive that folder manually after confirming which embedded
operator data is still needed. Profile data is never removed by this command.

## Troubleshooting

- Use `./install-commsplan.sh repair` after dependency or virtual-environment
  damage.
- Use `--dry-run` before the first unattended or custom-path invocation.
- Use `--offline` only when the required repository and dependencies are
  already present locally.
- If CommsPlan is reported as running, close it rather than forcing an in-place
  update.
- On Debian/Ubuntu-family desktops, native Qt Map support may require
  `libxcb-cursor0` and `libxcb-xinerama0`.
