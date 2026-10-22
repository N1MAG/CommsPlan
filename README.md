<p align="center">
  <img src="assets/CommsPlan_logo.png" alt="CommsPlan logo" width="150">
</p>

# CommsPlan 2.0.6

See the CommsPlan 2.0.6 release page for available platform packages, source
archives, checksums, and release notes.

### A coordinated operations console for HF digital stations

CommsPlan brings radio configuration, companion applications, schedules,
message intelligence, traffic review, operational awareness, and station
services into one desktop workspace. Version 2.0 makes multi-radio operation
the standard architecture while keeping a one-radio station simple and fully
supported.

CommsPlan does not replace FLRig, FLDigi, FLMsg, FLAmp, JS8Call, VarAC, VARA,
MeshCore, or Meshtastic. It coordinates those independently useful applications
as one coherent station while preserving their own profiles, records, and
protocol-specific controls.

<p align="center">
  <img src="docs/images/readme-2.0/ops-center.png" alt="CommsPlan Ops Center showing multi-radio activity, schedules, traffic, and operational awareness">
</p>

*Screenshots show a demonstration station with scrambled identities and fictional
exercise messages. Radio readbacks and green Mesh connection status are simulated
for illustration; no live radio or Mesh connection was used. Detail images show
the individual workspaces.*

## One operating picture for the station

CommsPlan connects the station's setup and operating surfaces so the operator
can move from configuration to a live, explainable operating picture:

- **Main and Radios:** define the station identity, operating groups, policies,
  and software once, then give each radio its own application identity, profile,
  ports, paths, message locations, plan assignment, and launch behavior.
- **Message Intelligence:** normalize supported traffic into source-aware
  summaries, topics, locations, status, and attention cues while preserving the
  original message and its receive or import provenance.
- **CommsPlan Spotter and CommStat:** use CommsPlan's built-in Spotter service to
  review Spotter forms and CommStat reports in the same station evidence model,
  with source and transport context kept visible.
  CommStat-derived or Internet-assisted evidence is not presented as proof that
  the selected radio heard it locally.
- **Plans and scheduling:** build reusable Frequency Plans from HF Daily, HF
  Nets, and SOP layers, assign them to radios, and review the resulting windows
  in Ops Center before guarded scheduler actions occur.
- **Messages and Compose:** review JS8Call, Spotter, CommStat, FLMsg, FLAmp,
  VarAC, Mesh, and BBS traffic in one inbox, then stage supported outbound forms,
  files, messages, and local-mesh traffic from one workspace.
- **Map and mesh:** use the offline map for station, regional, path, traffic,
  schedule, and mesh-node context. MeshCore and Meshtastic are station services
  that can contribute received text and positions without becoming HF radio
  profiles.
- **Station services:** monitor readiness and launch behavior per radio, and
  manage Net Control, Spotter watches and Expect policies, VarAC BBS
  publication, and FLAmp queues with explicit access and safety controls.

## HF Digital Tri-Mode

HF Digital Tri-Mode combines three complementary families of amateur-radio
software:

- **Fast Light:** FLRig provides radio control, FLDigi supports digital-mode
  operation, and FLMsg and FLAmp support structured messages and file transfer.
- **JS8Call:** provides weak-signal keyboard messaging, store-and-forward
  capabilities, and relay communication using JS8.
- **VarAC and VARA:** provide chat, messaging, file transfer, BBS, and related
  services.

CommsPlan helps bring these independently useful applications together as a
coordinated station. It can discover and configure application instances,
maintain separate identities for multiple radios, manage paths and endpoints,
monitor readiness, control application launching, associate schedules with
radios, and collect supported traffic into a common station message library.

For multi-radio stations, CommsPlan keeps each radio's application profiles,
ports, message locations, and launch identities distinct. Station-level
services such as CommsPlan Spotter, FLAmp Q, CommStat, and the CommsPlan BBS can
then work with received traffic according to the operator's configured access
and publication policies.

## Station setup: Main, Radios, and Local Mesh

Main configuration holds station-wide identity, operating groups, access
policies, and defaults. Each configured radio then has an explicit identity,
operating-model and Frequency Plan assignment, application configuration,
connections, and launch policy. Guided Add Radio and Software Administration
use the same saved software identities, so review and runtime behavior stay
aligned.

Launch Control distinguishes health monitoring, launch-at-startup policy, and
manual start actions. CommsPlan attributes process and endpoint readiness to the
correct radio instance before deciding that an application is already running.

Local Mesh connects supported MeshCore and Meshtastic devices at the station
level. Received text and node positions can enter the canonical inbox and map;
qualified connections can use the guarded Local Mesh Compose path when outbound
permission is explicitly enabled. Mesh transport state remains separate from
HF radio identity and schedule assignment.

## Plan Builder and scheduling

Plan Builder combines saved HF Daily schedules, HF Net rows, and SOP condition
layers into a named Frequency Plan. Assignments remain explicit, so operators
can see which radio and plan own a scheduled window. Ops Center shows the live
effective context, while the scheduler applies guarded changes with visible
holds, busy deferral, and manual-control boundaries.

![Plan Builder linking frequency, daily, net, and SOP layers](docs/images/readme-2.0/plan-builder.png)

![HF Daily scheduling with explicit frequency and time windows](docs/images/readme-2.0/daily-schedule.png)

## Message Intelligence, Messages, and Compose

CommsPlan projects supported traffic into one canonical station message library
with stable source identity and external references. Message Intelligence adds
message summaries, topics, locations, status and attention cues, and suggested
routing context so operators can scan the inbox and map without losing the original
message, source, radio, endpoint, or receive-path evidence.

The library can include JS8Call, built-in CommsPlan Spotter traffic, reviewed
JS8Spotter imports, CommStat, FLMsg, FLAmp, VarAC, BBS, MeshCore, and
Meshtastic traffic. CommStat artifacts and
JS8-delivered CommStat reports retain their status and brevity context when
available; source labels preserve the difference between RF, Internet-assisted,
and imported evidence. Compose stages only the capabilities supported by the
selected application and transport, including FLMsg/FLAmp, JS8Call, CommStat
RF, Spotter forms, VarAC files, and qualified Local Mesh sends.

![Messages inbox with fictional multi-transport exercise traffic](docs/images/readme-2.0/messages.png)

## CommsPlan Spotter and automation

CommsPlan includes a built-in **CommsPlan Spotter** workspace for station-wide
watches, JS8 Expect responses, access policies, forms, and reviewed imports.
Actual received traffic stays in Messages, where source-neutral actions can send
it to the Map, an operator record, a reply workflow, or a saved watch.

The optional external **External Spotter/JS8Spotter** companion keeps its own
installation, profile, and application identity. CommsPlan can launch a
configured external companion or import its selected data explicitly; it does
not rename that companion to CommsPlan Spotter. Built-in responses and forms
use the existing guarded JS8Call path, with the selected radio, target, policy,
and payload visible before a send.

## Offline operational map

The map uses bundled geography and local station data, so it remains useful
without an Internet tile service, account, or API key. It combines known
stations, regions, paths, schedules, peer activity, Spotter and CommStat
evidence, message-derived context, and MeshCore/Meshtastic nodes or positions.
Layers and inspectors keep the source and age of evidence visible; the map is
decision support rather than a guarantee of current propagation or delivery.

![Offline operational map with station and regional context](docs/images/readme-2.0/operational-map.png)

## What's new in 2.0.6

CommsPlan 2.0.6 expands the station workflow in several areas:

- **Call Finder:** search offline FCC amateur and GMRS records and Canadian
  ISED amateur records, with optional U.S. and Canadian postal-reference estimates.
  Source status, record counts, dates, and estimate provenance are visible.
  **Update all** refreshes sources one at a time; cancel stops the current
  operation and prevents the remaining sources from starting, while a failed
  or cancelled replacement keeps the previous usable snapshot. NCS lookup can
  fill the current entry draft after review without adding a check-in or sending
  anything.
- **Guided configuration:** Station Setup leads from operator and group choices
  through radio, application, operating behavior, schedule, and final review.
  Radio and software editors save their own scope, retain the last committed
  view if saving fails, and show Complete, Review, Not checked, Unsaved, or
  Optional status with a reason. Review scheduling per radio before explicitly
  enabling it; disable is available from that radio's schedule controls.
- **Forms Library:** preview selected forms or a folder, then explicitly import
  valid new forms into the station library. Existing duplicates are reported
  and skipped; merely browsing a folder does not copy its files.
- **Map references:** the map includes bundled worldwide geography for offline
  use, category and text search, and a saved starting area. Optional license
  references can be searched and displayed using postal estimates after their
  source datasets are loaded. A reference is an estimate, not evidence of a
  station's current location or activity.
- **Radio-aware services:** launch readiness and application identity stay
  associated with the selected radio. BBS configuration and routing expose
  their owning station or radio context before service actions are applied.

### Release qualification

Native package availability and automated build, install/upgrade and startup
results are reported on the release page. Those checks do not establish every
physical-radio, operating-system or display configuration. Python 3.11 is the
tested and recommended interpreter; the broader Python 3.10 through 3.13
compatibility range has not been clean-host qualified in full.

A macOS/PySide test covering Compose comment wrapping and preserving oversized
saved text reproduced a segmentation fault; no fix is recorded. An earlier
combined test run omitted world-map grid lines at low zoom. The latest combined
release check passed all 192 tests, including that grid test; the cause of the
earlier failure has not been established.

## Supported systems

| Platform | CommsPlan 2.0 support |
|---|---|
| Linux amd64 | GitHub Release `.deb`, source install, and guided installer |
| Windows 10/11 x86-64 | GitHub Release installer and source install |
| macOS Intel and Apple Silicon | GitHub Release disk images and source install |

Python **3.10 through 3.13** is accepted by CommsPlan; Python 3.11 is the tested and
recommended release interpreter. Companion radio applications are installed
separately and remain under the operator's control.

## Existing station data

CommsPlan upgrades preserve the established station profile and data locations
through the documented platform procedure. The application keeps its existing
storage and compatibility identities so companion applications and saved
stations continue to work. Follow the installation guide when upgrading, and
make a verified backup before the first upgrade.

## Download CommsPlan 2.0.6

Use the [CommsPlan release page](https://github.com/N1MAG/CommsPlan/releases/latest)
for available 2.0.6 installers, source archives, checksums, and release notes.
Use only the artifacts actually listed there. Unsigned Windows/macOS packages
are identified in their filenames and release notes.

Verify downloaded files against the checksums published on the release page.

## Install or update from the public source release

Clone the public repository and select the 2.0.6 release tag. For an existing
checkout, fetch the public tag and select it before running the installer.
Close CommsPlan and its companion applications, use the same application folder
as the existing installation, and require the final `Installation verified`
line before launching CommsPlan. A Git pull by itself is not a complete update.

```bash
git clone https://github.com/N1MAG/CommsPlan.git
cd CommsPlan
git checkout v2.0.6
```

For an existing checkout:

```bash
git fetch origin --tags
git checkout v2.0.6
```

### Windows PowerShell

With Git and 64-bit Python 3.11 installed:

```powershell
py -3.11 install-commsplan.py
.\start-commsplan.cmd
```

### macOS

With Python 3.11 installed:

```bash
python3.11 install-commsplan.py
./start-commsplan.sh
```

### Linux

The guided installer installs required system packages where supported,
creates CommsPlan's virtual environment, and prepares the desktop launcher.
Use `setup` for first setup, `update` for a source update, and `desktop` to
refresh desktop integration after an update, including when an older package
launcher is still installed. Run these commands from the checked-out 2.0.6
release:

```bash
bash install-commsplan.sh setup
# For a source update: bash install-commsplan.sh update
# To refresh the desktop launcher: bash install-commsplan.sh desktop
```

See the [installation guide](docs/Installation.md) for distribution details,
custom paths, repair, update, and uninstall instructions.

## Upgrade an existing station

CommsPlan carries an existing single-radio station forward and updates an
existing 2.0 installation through the verified installer. Follow the platform
procedures and operator checks in the migration guide.
Before upgrading:

1. Close CommsPlan and affected companion applications.
2. Create and verify a restorable backup of the existing station profile.
3. Update the application using the documented platform procedure.
4. Review the proposed default-radio conversion before confirming it.
5. Verify software paths, endpoints, schedules, and Launch Control after the
   first 2.0 start.

The first frozen-package start also records guarded backup evidence before
station schema or database writes. Keep the actual backup path from
`<PROFILE_ROOT>/.commsplan-packaged-startup.json`; repeated starts of the same
version reuse that receipt after manifest checks. A fresh profile is recorded
without a backup until its databases appear, then the first data-bearing start
captures one. This evidence supports recovery preparation and does not by itself
qualify historical native replacement or rollback. The package-startup copy
covers the selected profile's `config` tree only; it does not include credentials,
logs, caches, or companion-application data.

The migration preserves the existing station as the default radio and brings
its schedules, messages, software settings, and operating data forward. Adding
a second radio is optional. The conversion path has automated rehearsal coverage,
but operators should treat the verified backup as mandatory and be prepared to
review or rebuild affected companion-application settings if an older station
differs from the rehearsed profile. If you are upgrading a much older
installation, use the staged upgrade path in the migration guide for your
platform and release history.

## First configuration

The recommended order is:

1. Enter the station identity and location under **Configuration**.
2. Define the operating groups used by scheduling, filtering, access policies,
   and publication rules.
3. Add or review the first radio and select the software it actually uses.
4. Review Software Administration and Launch Control for that radio.
5. Create or assign a frequency plan and schedule.
6. Open Ops Center, Messages, and Map to confirm the resulting station context.

The in-app [CommsPlan Guide](docs/guide.html) provides the full reference for
each workspace, setup flow, application family, and recovery action.

## Data, privacy, and diagnostics

- CommsPlan stores its settings and operational data locally.
- The native map is designed to remain useful offline.
- External network services are used only by features the operator enables.
- Companion-app paths and credentials remain on the local station.
- OS credential storage is used for supported saved passphrases when a keyring
  backend is available; CommsPlan does not fall back to plaintext storage.

When reporting a problem, include the operating system, CommsPlan version, affected
radio/application, reproduction steps, and the relevant application logs.
Review every attachment for callsigns, message contents, access codes, local
paths, and other information you do not want to share. Database files are not
normally needed unless support specifically requests them.

The main CommsPlan logs are located in the configured profile root:

- the application log
- `perf_metrics.log`
- performance or UI-hang evidence files, when present

## Documentation

- [CommsPlan User Guide](docs/guide.html)
- [Installation and update guide](docs/Installation.md)
- [Changelog](CHANGELOG.md)
- [Security and support](SECURITY.md)
- [Credits](CREDITS.md)

## Dedication and continued development

Dedicated to my Dad (SK), a U.S. Navy Radioman who learned HF Digital Tri-Mode
at age 86.

If CommsPlan benefits your station, please consider supporting its continued
development. There's more to come.

[buymeacoffee.com/n1mag](https://buymeacoffee.com/n1mag)

## License

CommsPlan is licensed under the GNU General Public License v3. See
[LICENSE.md](LICENSE.md).
