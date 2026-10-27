# Changelog

## [2.0.8.1]
- Fixed: Saving Fast Light application folders now keeps the reviewed folder, saved resource claims and radio settings consistent. This fixes FLMsg and FLAmp message-folder readback errors after enabling an application.
- Fixed: The same save correction covers all four Fast Light settings folders and FLDigi log/check-in folders, including changes, intentional clearing and reopening existing configurations. Failed readback still rolls back the save.
- Qualification: Automated configuration and package checks do not establish a native Linux operator pass or live radio readiness. Existing 2.0.8 hardware, transport and macOS limitations remain unchanged.

## [2.0.8]
- Fixed: Assigned schedules follow automatically after validation, without a separate enablement step. Schedule reads remain isolated to the assigned radio and source; Quick QSY stays until its next eligible transition, and gaps retain current tuning.
- Fixed: Scheduler and QSY feedback clears after verified completion. Schedule refreshes retain their last display, and schema reads no longer repeatedly rebuild valid indexes or invalidate schedule snapshots. Incorrect legacy identity indexes still repair automatically without deleting roster data.
- Changed: Plan Builder shows radio assignments and provides schedule review/swapping with RF Guard checks and informational cautions for conflicting radios, including inactive peers. Compact actions, source selectors, tooltips and font-aware sizing leave more room for schedule rows.
- Added: Reviewed manual Mesh-to-JS8Call and JS8Call-to-Mesh forwarding from the Inbox reader, with connection-scoped group/channel or callsign/node selection, type-to-filter recipients, exact addressed preview, byte limits, expiry, drafts and forwarding activity. Saved drafts do not send automatically.
- Added: Forwarding can explicitly clear and verify a conflicting JS8Call selection. Unsupported icons and non-ASCII/control content are removed from outgoing Mesh sender/body text for JS8; the original remains available. Oversized or empty cleaned messages are blocked.
- Fixed: Mesh/Any time Inbox filtering accepts the latest requested results and displays delayed progress. Background verification-policy reads, durable ingest retries, bounded file scans, projection writes and repeated presentation work have been improved to reduce stalls and contention.
- Changed: Hidden Compose work and optional startup work are deferred until needed. Process inventories are shared across routine consumers, FLDigi log reads are bounded, and repeated unchanged warning/widget work is reduced.
- Fixed: Linux MeshCore Bluetooth helper allocation, cancellation and cleanup retain exclusive ownership until a child has stopped. Uncertain failures keep reconnect paused for operator action. Diagnostics now publish complete reports with bounded retention and clearer launch/heartbeat details.
- Changed: Help and startup wording match assignment-driven scheduling, the cleaner Plan Builder and reviewed forwarding workflow.
- Known limitations: High process CPU remains under investigation despite improved responsiveness. The previously recorded macOS/PySide Compose comments crash remains unresolved. Automated package checks do not establish live RF, physical Mesh/Bluetooth reliability, every display configuration or successful message delivery. The full Linux two-radio restart/readback/soak and Bluetooth hardware/24-hour soak matrices remain unverified; the maintainer reports improved responsiveness, working scheduling and a successful Bluetooth session. Automatic cross-transport bridging/mailbox workflows and the complete worker-published Plan Builder input catalog remain deferred.

## [2.0.7]
- Added: Offline Station Traffic Summaries with independently editable RF Brief and Detailed reports, evidence review, group/source/path/geographic filters, paired draft persistence, export and Compose staging.
- Added: SpotterX review enrichments: verified JS8 Clear selection; peer history and passive hearing paths; policy-filtered structured Mesh analysis; reviewed received PINs; roster/map distance and bearing; redacted support reports; MeshCore contact favorites/history.
- Added: Optional per-net FLDigi Center + Narrow presets with configurable audio offset/filter width, signal-preserving dial compensation, one-time startup application and explicit Restore. Operators retain tuning freedom during a net.
- Fixed: Fresh radio setup and existing-radio edits preserve independent application choices without unrelated JS8Call replacement errors or false self-conflicts.
- Changed: Radio setup distinguishes Apply to radio setup, Save radio, and Save draft and close; retained drafts have Resume setup, required-field guidance and explicit save-failure warnings.
- Fixed: Component discovery fills the selected application's path and collapses duplicate installation suggestions while preserving distinct native profiles. Message-file browsing remains available independently of application startup choices.
- Fixed: Radio setup actions and supported bands wrap for compact windows and larger fonts; validation and draft guidance scroll with the active section. Keyboard traversal reaches the form and save actions.
- Changed: Help contents sit beside the guide with search; operator documentation covers 2.0.7 traffic summaries, review tools, tuning and accumulated setup fixes.
- Known limitation: The previously recorded macOS/PySide Compose comments crash remains unresolved; see the README qualification notes.
- Qualification: Package install/startup/upgrade checks and platform qualification are recorded with this release. Automated checks do not establish physical-radio/Mesh/filter/transfer behavior or every display/DPI combination. Outbound PIN broadcast, active peer-query automation and cross-transport relay are not implemented by this update.


## [2.0.6.1]
- Fixed: Fresh radio setup saves FLRig-only configurations without false application-path readback errors or a database error from the unused FLDigi port.
- Fixed: FLRig, FLDigi, FLMsg and FLAmp can each be configured independently. Hidden endpoint fields preserve existing ports, and edited FLMsg/FLAmp executable paths save correctly.
- Fixed: Application setup keeps Save and Cancel reachable in compact Software Administration views. Suggestions, validation messages and recovery controls scroll with the fields across JS8Call, Fast Light and VarAC editors.
- Changed: Release metadata validation accepts the four-part hotfix version 2.0.6.1.
- Qualification: Automated configuration and compact-layout checks pass. Native builds perform their platform checks; physical-radio behavior and every Windows display/scaling combination require operator verification. Existing 2.0.6 known limitations remain applicable.

## [2.0.6]
- Release status: Approved for public publication. Download availability and package checks are reported on the CommsPlan Releases page; physical-radio and platform qualification limits remain noted below.
- Added: Offline Call Finder searches US Amateur, GMRS, and Canadian amateur callsign references, with source details, mailing-postal grid estimates, reviewed Add to Operators, and draft-only NCS lookup. Update all downloads and imports the five source datasets one at a time, stopping on failure or cancellation.
- Changed: Configuration now uses a saved-setup checklist and scoped radio/application editors, with explicit launch responsibility, retained drafts, atomic save/readback checks, and consistent configuration status explanations. BBS setup reads the station catalog rather than creating competing library state.
- Changed: Automatic scheduling requires per-radio Review & enable approval of the saved setup. Disable immediately fences further automatic commands; changed command-affecting inputs require review again. Radio, application identity, endpoint and RF safety checks remain enforced.
- Added: The station CP Forms Library supports reviewed imports, duplicate protection, bounded validation and shared catalog selection across Spotter, Compose and Net Control.
- Changed: The offline map now includes worldwide country geography, category-based tools, geographic Country filtering, saved starting areas, a selection inspector and optional license-reference searches with explicit local index preparation and mailing-location caveats.
- Fixed: Mainland geometry remains visible in the bundled map, recovered JS8 endpoints clear obsolete health warnings, and saved Radio/Software status uses consistent scoped checks and correction routes.
- Fixed: Map Paths exposes Off, My Station, and All in the main filter strip. All includes links between other stations within current filters, and layer off/on retains the chosen scope.
- Changed: The Help guide, README and website cover these workflows under the CommsPlan name. A branded cross-platform source-installer entry point delegates to the existing verified installer.
- Known qualification gaps: A recorded macOS/PySide Compose comments crash remains unresolved. Native packages undergo automated build, startup, install/upgrade and rollback checks; these checks do not establish physical-radio or every operating-system/display configuration. An earlier combined-run world-grid test failed despite passing separately; the fresh combined release check now passes all 192 tests, including that grid test, although the earlier cause is unconfirmed.

## [2.0.5]
- Release note: This entry covers the CommsPlan 2.0.5 scope. Native package upgrade, uninstall, recovery, and hardware behavior remain subject to their platform procedures and operator checks; the complete Plan Builder input catalog remains deferred beyond 2.0.5.
- Changed: The CommsPlan brand retains FreqInOut profile roots, `freqinout.db` and `freqinout_nets.db`, compatibility commands, the Windows `AppId=FreqInOut`, the Linux package identity, and the historical macOS bundle identifier. Migration, profile-copy/recovery, and Linux source-checkout relocation workflows preserve explicit paths and rollback artifacts.
- Added: Frozen-package startup now guards pre-schema database writes with a station-lock check and a verified cold copy of the selected profile `config` tree, including raw WAL/journal sidecars. It records the actual receipt and manifest paths, validates SQLite on a disposable copy, reuses valid same-version evidence, and captures a fresh profile when data first appears.
- Changed: Multi-radio hardware, BBS routing, and Launch Control now keep software, endpoints, rig-control instances, readback evidence, and retries tied to the selected radio. Ambiguous or failed states remain visible and stop unsafe actions; schedule changes preserve per-radio ownership and source/offset explanations while background refreshes stay bounded. Scheduler caches reuse unchanged plan, assignment, and schedule inputs and reload after saved source revisions change.
- Added: Ops Center now uses a tabbed dashboard with shared roster, readiness, and status inputs. SOP work adds bounded attention/evidence search and summary authoring, while radio controls clarify active/default-radio ownership and operator actions.
- Changed: Map presentation uses bounded worker snapshots and preserves visible scenes across focus changes while retaining offline vector operation; linked station context, layers, route updates, and MeshCore map projection are handled through the same guarded refresh path.
- Fixed: Messages and navigation now keep stable source-aware status/action state and avoid unnecessary refresh work. Compose handles default and disconnected Mesh states more clearly, with improved Spotter comment layout and saved-form field decoding.

## [2.0.4]
- Added: A consistent first-run welcome guides new stations into Guided Add Radio, while **Set Up Later** leaves a persistent **Set Up First Radio** action in Ops Center. Existing single-radio upgrades continue through the separate **Upgrade Existing Station** flow.
- Fixed: Windows startup no longer flashes internal command windows or transient unpainted FIO windows while the splash screen is active.
- Fixed: Radio health indicators now use cached operational evidence consistently. A working radio remains green when optional software has a harmless advisory, unknown or stale evidence is neutral, and yellow is reserved for conditions that affect operation.
- Changed: Launch Control starts one radio stack at a time, requires fresh read-only proof that the configured rig-control endpoint can reach that radio, skips the remaining applications for an offline radio, and then continues with the next radio.
- Fixed: FLDigi and JS8Call advance as soon as their configured endpoints respond, process-only applications advance after a short stability check, slow VarAC/JS8Call startup no longer causes premature dependent skips, and an early nonzero exit is reported immediately.
- Added: Official GitHub Release packaging for Windows x86-64, Linux amd64, macOS Intel, and macOS Apple Silicon, with checksums and build provenance. Until signing is configured, Windows and macOS download filenames explicitly include `-unsigned` and the release notes explain the operating-system trust prompts.

## [2.0.3]
- Fixed: On Windows, installation no longer fails when security or indexing software denies only the final rename of an otherwise complete and verified station backup. FIO retries the rename, retains and reports the verified temporary backup path when necessary, and still blocks installation for every copy, hash, database, manifest, missing-data, or other filesystem failure.
- Fixed: The native-default JS8Call identity now specifically means the configured executable running without `-r` or `--rig-name`, so a named sibling process can no longer suppress launch of the upgraded station's default JS8Call instance.
- Fixed: JS8Call status and fallback control remain scoped to their configured endpoint, preventing a process or shared connection on port `2442` from being reported as the instance on port `2443`.
- Changed: The installation and upgrade guide now covers 1.2.8 through 2.0.2 starting points, uses the immutable `v2.0.3` release tag, and explains the Windows verified-backup naming fallback.

## [2.0.2]
- Fixed: JS8Call launches for an upgraded single-radio station now preserve the native default profile when that is the configured instance, so an existing FT-DX10 installation does not silently open an invalid FIO-generated profile.
- Fixed: If an obsolete FIO-generated JS8Call profile is still running during the transition, Launch Control stops safely and tells the operator to close that process before retrying instead of starting a duplicate instance.
- Fixed: MeshCore connection workers now pause automatic retries after terminal Linux Bluetooth failures such as an unavailable adapter, a powered-off controller, a cancelled BlueZ connection, or failed pairing. The UI reports that operator action is required instead of repeatedly destabilizing the device connection.
- Changed: The installation and upgrade guide now covers 1.2.8, 2.0, 2.0.1, and earlier 2.0.2 candidate starting points, uses the public `main` branch and immutable `v2.0.2` release tag, and verifies the application version in the title bar.

## [2.0.1]
- Added: Message Compose now provides guarded Local Mesh sending through qualified Meshtastic and MeshCore connections, with explicit destination selection, exact payload preview, final confirmation, and evidence-specific results.
- Fixed: MeshCore and Meshtastic traffic accepted for Inbox presentation now enters the canonical Messages read model while remaining available to Ops Center and Map according to channel policy.
- Fixed: On Linux, connecting a secured MeshCore Bluetooth device now performs one bounded pair-before-connect retry when service discovery requires authentication, allowing the desktop PIN prompt to complete while FIO remains open.
- Changed: Local Mesh now shows the default-off **Allow Send** permission with the selected saved device instead of hiding it inside Advanced connection details; qualified Meshtastic and MeshCore TCP/USB/BLE connections use the same visible permission and connected-only Compose guard.
- Changed: New installations and upgrades now use the same verified source installer and neutral `start-freqinout` launcher. The installer validates and backs up an existing closed profile, repairs incomplete environments, verifies the runtime, and prevents launch after an incomplete installation.
- Changed: Existing single-radio profiles now complete the mandatory **Upgrade Existing Station** review before runtime services start. The review preserves manufacturer and model and offers only **Back Up and Upgrade Station** or **Exit FIO**.
- Fixed: Managed VarAC cluster nodes now retain their radio-specific VarAC and VARA configuration paths, launch the intended node, and correctly attribute a shared VarAC executable without blocking the second radio.
- Fixed: Launch Control now discards and reaps exited child processes before evaluating a new start, allowing a failed VarAC or companion-app launch to be retried without a restart.
- Added: BBS Automation Rules are available again from the BBS workspace, with radio-aware VarAC, FLMsg, and FLAmp routing and a durable delivery ledger that prevents unchanged files from being copied repeatedly.
- Added: Radio Service now provides a safe, idempotent `Initialize BBS…` action for creating or repairing the selected radio's managed BBS folders and metadata without replacing operator files.
- Fixed: The first 1.2.8-to-2.0 startup now establishes the shared JS8 message schema and source identity before message projection and automation begin, improving upgrade reliability without requiring a manual JS8 traffic reload.

## [2.0.0]
- Added: Multi-radio operation with profile-scoped radio runtime configuration, launch control, health monitoring, and JS8/Fast Light path handling. One-radio stations remain fully supported.
- Changed: FreqInOut 2.0 is distributed as a source installation for Windows, macOS, and Linux. No 2.0 executable or signed application bundle is currently published.
- Changed: Existing 1.2.8 single-radio profiles are offered an explicit, backup-first conversion into the 2.0 default-radio model. Operators should retain the verified backup and expect to review or rebuild affected companion-application settings if their station differs from the rehearsed upgrade path.
- Added: Map intelligence now includes Regional Intel heat-map behavior, focused path rendering, station/report action panels, CommStat reported-for/reported-by handling, and map-to-Messages handoff.
- Changed: Map now opens in one reusable nonmodal window so operators can keep it visible while working elsewhere in FIO; its validated normal placement and maximized state persist without changing the main window, and closing Map hides it for fast reuse.
- Added: Map now provides a persistent `Show FIO` action, and station-detail Inbox/Compose actions bring the existing main window forward after navigation without closing, moving, or resizing Map.
- Changed: Replaced the Chromium/WebEngine/Leaflet Map surface with one native Qt Quick and Qt Location renderer. The renderer is built in its final hidden parent before the Map window is shown, uses bounded worker-built overlays, and updates the existing surface without browser navigation or JavaScript handoffs.
- Changed: The native Map now uses a provider-free offline coordinate canvas with bundled US, Canadian, and Mexican vector geography; it never requests map tiles, an API key, or network access.
- Fixed: First Map launch no longer creates a browser native surface after the window is visible, eliminating the known flash, swipe, monitor jump, and window reconstruction path on macOS, Linux, and Windows.
- Fixed: Native Map wheel/button/pinch zoom, drag panning, marker selection, and polygon detail actions now preserve the established Map interaction model without transporting recursive QML objects into Python.
- Fixed: Native Map FEMA Regions, state labels, five-band SNR path colors, SitRep/Regional status colors, and data-aware legend now retain the proven layer meanings; crowded city labels are deterministically suppressed until zoom provides room.
- Fixed: The native Map now shows the production SitRep station-pin key, keeps View/Topic/Group/Age together when space permits, constrains the Age chooser to its active screen, and reveals the native scene only after its first complete projection.
- Fixed: `Show FIO` and Map station-detail handoffs now queue main-window presentation outside the originating Qt button signal, preventing the macOS/PySide re-entrant signal-connection deadlock observed from the direct toolbar action.
- Fixed: Switching Light and Dark themes now updates the open Map canvas, selected-station detail, Map chrome, and Settings navigation from one shared palette without moving a window or rebuilding Map data.
- Fixed: Message and map topic filtering no longer treats status labels such as `Power: Not Reported` as real topic evidence while preserving real reports such as outages or contaminated water.
- Changed: Launch Control now presents configured supported apps and custom tools through one operator-facing software surface with Monitor Health, Launch at Startup, Start/Stop, and Status behavior.
- Changed: Station Health and readiness guidance now better reflect multi-radio operating environments and the difference between monitored apps and manually managed tools.

## [1.2.8]
- Changed: Station Health now shows one latest scheduler success plus an issue log instead of repeating routine already-applied scheduler checks.
- Changed: Informational scheduler holds and old transient FLDigi busy-check diagnostics no longer count as active station responsiveness issues.
- Fixed: ControlFreq now colors the `FLMsg / FLAmp` message-summary row consistently with other rows, highlighting only when new files are present.
- Fixed: Routine companion-app detection now uses one thread-safe shared process inventory instead of independent scheduler, JS8Call, VarAC, and executable-path scans, reducing repeated Linux `/proc` reads and idle CPU activity.
- Fixed: Process discovery now recognizes Windows-style command paths on Linux, improving VarAC and other Wine-launched companion detection without requiring repeated process scans.
- Fixed: JS8Call scheduler and status checks now share one process-global JS8Net connection instead of starting new RX, TX, and heartbeat threads from each short-lived client, preventing thread count and CPU use from climbing over time.
- Changed: VarAC Managed BBS Vault full reconciliation now adapts from active five-second checks to 30-second and two-minute idle checks while a lightweight five-second activity signature still wakes it promptly when files, logs, or settings change.
- Fixed: Unchanged Managed BBS publish manifests and runtime state are no longer rewritten on every idle vault run, reducing repeated directory scans and SQLite WAL writes on Linux stations.

## [1.2.6.1]
- Fixed: CommStat artifact rows deleted from Messages are now hidden with a durable local FIO tombstone instead of only deleting the temporary artifact row, so they do not reappear after refresh, restart, or CommStat ingest while the original CommStat source database remains untouched.
- Fixed: `Resume Schedule` now forces the active operating plan back into FLDigi mode/offset enforcement instead of skipping FLDigi when the current schedule row looks already applied.
- Fixed: Scheduler JS8 offset status now uses fresh readback before declaring `Off Schedule`, so a correct JS8Call offset does not remain falsely flagged after `Resume Schedule` while FIO still manages JS8 offset under FLRig/Rigctld control.
- Added: Scheduler decisions are now persisted in a bounded `scheduler_events` journal so Station Health can show why FIO applied, skipped, held, retried, or failed a schedule action.
- Changed: Station Health now includes recent scheduler decisions, making schedule-miss reports easier to diagnose without relying only on transient logs.
- Fixed: FLDigi RX-busy holds now record the 3-minute watchdog recheck and the authoritative break-away decision before proceeding with the schedule change.

## [1.2.6]
- Added: Settings now includes a JS8Spotter Form Mapper so discovered `MCF*.txt` forms can be assigned operator-friendly purposes such as Net Check-in, SitRep / StatRep, Net Notification, Weather / Storm, Hazard / Early Warning, Intel / RFI, Medical / Hospital, and Station Capability.
- Added: JS8Spotter mapper routing now drives Messages visibility, unread alert highlighting, and Map evidence for forms whose sender already has known station location data, using cached form-code sets instead of per-row Settings reads.
- Added: Map now has operator-controlled layers for Stations, Links, Weather Reports, Alerts, and Infrastructure, with JS8Spotter weather/alert/infrastructure forms rendered as clustered operational icons when the sender has known station location data.
- Changed: Map layer controls for Stations, Links, Weather, Alerts, and Infrastructure now live above the map so operators can reduce clutter without opening an overlay box.
- Changed: Messages now uses a cleaner inbox control row with visible-tab refresh choices, a five-second countdown under the time display, a single clickable BBS status indicator, and a More menu for less common actions.
- Changed: Dropdown controls across the app now auto-fit their displayed text and popup width more consistently, improving Messages and JS8 NCS readability.
- Changed: ControlFreq now labels direct VarAC messages and VarAC BBS folder files separately so the message summary is understandable at a glance.
- Changed: Help and README content now explain map icon interpretation, JS8Spotter form mapping, Messages refresh/BBS behavior, Managed BBS, and first-run setup in plainer operator language.
- Changed: JS8 NCS check-in and net-notification handling now uses mapper-selected forms, so custom or group-specific check-in forms can flow to NCS without hard-coding new form IDs.
- Changed: JS8Spotter SitRep ingest now uses mapper-selected status forms while preserving the existing conservative parsed-status behavior for `F!104`, `F!301`, and `F!304`.
- Fixed: JS8Spotter form discovery now supports alpha-suffix form IDs such as `F!702A` when the form file is named like `MCF702A.txt`.

## [1.2.5.6]
- Fixed: VarAC VGuard now validates inbound file senders with session-aware traffic-log evidence before applying trust policy, including portable/mobile base-callsign matching such as `W1ABC/P` to `W1ABC`.
- Added: VGuard trust policy can now allow VarAC BBS Allowed Callsigns, Operator History `TRUSTED` callsigns, or both, giving operators a narrow allow-list mode or a broader FIO-trusted-operator mode.
- Changed: VGuard records clearer reasons for file decisions, including sender conflicts and unresolved senders, so support can distinguish a truly unauthorized file from an ambiguous VarAC log event.

## [1.2.5.5]
- Fixed: JS8Call API/live-queue messages are now normalized before listener fan-out so malformed queue entries, non-dictionary params, null bytes, and oversized fields cannot reach Map or JS8 NCS listeners in unsafe shapes.
- Fixed: JS8 inbox DB ingest now validates and bounds each new row before it enters FIO's local message cache. Unreadable rows are quarantined in `js8_bad_records` and included in the ingest checkpoint so one bad JS8 row is not retried forever.
- Fixed: Messages now skips malformed local JS8 cache rows and guards table-model row access, preserving currently supported JS8 message display while reducing native Qt crash exposure from corrupt or unexpected data.

## [1.2.5.4]
- Added: FLDigi Net Control roster rows now carry a stable check-in sequence number so operators can sort or report stations in the order they were added.
- Changed: FLDigi Net Control default roster order is now operational: NCS, ANCS, PP traffic, RR traffic, other traffic, then QRU, preserving check-in order inside each group. UI column sorting remains available, but generated roster files continue to use the operational order.
- Added: FLDigi Net Control can parse optional keyword text before PP/RR traffic, keeps callsign/name/state as the compare identity, and writes relay selections to role-specific `*_CheckIns_Relays.txt` files.
- Added: ANCS relay compare now supports the case where the local ANCS roster has stations the NCS list missed, making those stations available as `Stations to Relay to NCS`.
- Changed: Station Health now reports scheduler holds alongside external dependency responsiveness, marks stale OK checks as warnings, and describes FLDigi busy watchdog break-aways as possible stale/hung external app busy states.
- Fixed: FLDigi busy schedule holds now force a fresh recheck after 3 minutes and break away if FLDigi still reports busy, preventing stale receive-state indications from blocking HF/SOP schedule changes indefinitely.
- Fixed: Multi-rig scheduler status snapshot, shared PTT, and control-task stalls are now logged and surfaced through Station Health so support can distinguish FIO scheduler recovery from companion-app responsiveness problems.

## [1.2.5.3]
- Fixed: Multi-rig VarAC Managed BBS FLAMP block requests now accept clear operator-intent forms such as `BLKS 7,8 E957`, `BLOCK 7,8 E957`, `BLOCKS 7,8 E957`, and glued final-token forms such as `BLKS 8E957`, while keeping `LIST E957` / `LIST BLKS E957` as block-list inspection commands.
- Changed: FLAMP BBS helper files now teach `LIST <queue>` for inspection and `BLKS <blocks> <queue>` for block-file generation, and incomplete commands such as `BLKS E957` publish a helper notice instead of being silently ignored or guessed.
- Fixed: `Resume Schedule` actions from the left ledge and ControlFreq are now authoritative operator actions even when JS8Call, VarAC, or FLDigi appear RX-busy; PTT protection still prevents unsafe immediate changes while a transmitter is actively keyed.

## [1.2.5.2]
- Fixed: Multi-rig VarAC Managed BBS FLAMP block-fill files now remain stable per radio profile after `BLK ...` requests. A follow-up `<BLR>` refresh republishes the same block-fill file, and FIO recreates the live overlay if VarAC consumes or removes it during download handling.

## [1.2.5.1]
- Fixed: Multi-rig VarAC Managed BBS now treats each active radio profile's VarAC traffic log as the authoritative command source and tracks durable per-log cursors instead of relying on `last_request_ts`.
- Fixed: Managed BBS views remain published until a new command or session disconnect, preventing long file-transfer or multi-file retrieval sessions from being reset back to root too early.
- Fixed: active radio profiles with duplicate live VarAC BBS directories are skipped with a clear warning so concurrent radios cannot overwrite each other's BBS listing.
- Fixed: Public-visible code-protected BBS locations now appear in the root listing while access enforcement still happens when the remote station opens the location.
- Fixed: FLAMP Managed BBS helper views are standalone and no longer blend with the selected managed location's files.
- Fixed: Access-code commands are case-insensitive and accept bracketed entry such as `HUBS [MRHUB]`.

## [1.2.4]
- Changed: FLDigi / SSB Net Control now lists scheduled nets that are currently active or coming up within the near operating window, avoiding duplicate daily repeats while still letting late-starting operators select the intended net.
- Changed: FLDigi Net Control roster copy output now includes the station role for NCS and ANCS rows, followed by traffic when present, and repeated callsign rows are merged so corrections do not create duplicate check-ins.
- Added: FLDigi Net Control now tracks `Directed By` and `Acked By` with visible row chips, scoped action buttons for `NCS`, `ANCS`, `Shared`, and `All`, `ACK Needed` for unacknowledged check-ins, and `Next TFC` for stepping through one directed traffic station at a time.
- Added: FLDigi Net Control now writes role-first macro files such as `NCS_ACK_Pending.txt`, `ANCS_ACK_Pending.txt`, `NCS_Next_TFC.txt`, and `ANCS_Next_TFC.txt`, while keeping full and role-scoped check-in files current from the roster without requiring `Save Check-ins`.
- Changed: FLDigi Net Control moves `Start Net` and `End Net` into the session/QSY row, separates `Save Check-ins` from live actions, highlights that save action only when roster edits are unsaved, renames `Local Roster` to `Net Roster`, and gives the roster table more practical column sizing.
- Changed: FLDigi macro setup is now a compact collapsible header that shows `Macro: None`, `Macro: Needs Mapping`, or `Macro: Mapped`, hiding setup controls during normal mapped operation so the roster gets more room.
- Added: Station Health provides a dedicated view of external dependency responsiveness, including grouped background ingest health and issue-since/cooldown context, while keeping traffic-busy state separate from app/dependency health.
- Changed: Messages export/filtering, FLAMP incomplete awareness, and CommStat message handling were tightened for clearer field review and spreadsheet export behavior.
- Changed: external dependency polling and background ingest paths now use stronger isolation/backoff behavior so slow or unreachable companion applications are less likely to stall visible UI workflows.
- Changed: FLDigi log-assisted auto-add controls are hidden for 1.2.4 because real FLDigi logs can contain scripts, acknowledgements, repeated text, form payloads, and noisy decodes; the code is retained with comments for a future review-only/RX-only design.
- Changed: the in-app Help guide now explains the updated multi-rig FLDigi / SSB Net Control workflow, including scheduled-net selection, role-aware roster copying, copy/file combinations, macro file behavior, deduplication behavior, save highlighting, and visible-only NCS controls.

## [1.2.3]
- Changed: Message Auth signature/hash verification now applies to VarAC and VarAC BBS `.k2s/.b2s` files and signature sidecars using the same trusted-key/hash workflow already used for signed FLAmp files.
- Changed: `FLDigi Net Control` now offers `Copy Check-ins` instead of the callsign-only summary button, copying the full consolidated TFC/QRU/LATE check-in log and maintaining a `CheckIns_ALL.txt` macro feed beside the existing per-category check-in files.
- Changed: the in-app Help guide now teaches the major FreqInOut tabs more explicitly, expanding the purpose, workflow, and cross-tab interaction notes for ControlFreq, Messages, Map, schedules, operators, SOP Builder, and Settings so operators can learn not only how controls work, but why those screens matter and how related settings influence them.
- Fixed: VarAC schedule protection now also watches recent `VarAC.db` transfer lifecycle events, so scheduler-driven frequency changes are more reliably deferred during inbound or outbound file transfers even when log-tail visibility is incomplete.
- Changed: `Map` now skips no-op refresh rebuilds when its lightweight input signature has not changed, reducing repeated redraw work during clustered filter and visibility activity while keeping full reloads for real config changes.
- Changed: multi-rig `Settings` now throttles repeated VarAC BBS operator-lookup rebuilds and avoids redundant section height relayouts during repeated visits, while `HF Daily` reuses its last activation token so unchanged schedule views do not pay the full activation-refresh cost.
- Changed: `Messages` now fingerprints its JS8/local message stores and skips redundant JS8, JS8Spotter, CommStat, and unified SitRep reload passes when the backing DB files have not changed, reducing repeat activation churn.
- Changed: `Map` now reuses short-lived query snapshots for repeated propagation, operator-activity, recent-calls, and status rollup lookups during clustered refresh bursts instead of immediately re-querying every source on each refresh request.
- Fixed: multi-rig `Settings` now tolerates radio-scoped VarAC Managed BBS runtime/cache values when they are loaded back from storage as JSON text, preventing startup failures while opening the selected radio software view.
- Fixed: `ControlFreq` now reloads saved settings before rebuilding software status visibility and readiness context, so radio-software LEDs stay aligned with the currently saved radio/software selections instead of lagging behind the Settings tab.
- Changed: `Map` now groups the `SitRep State Summary` panel by FEMA region, making the status rollup easier to scan operationally while still showing the same state-level counts inside each region.
- Fixed: `Map` SitRep summary now rolls up all matching active states instead of truncating the summary source to the busiest eight state rows, keeping the visible region summary aligned with the pins shown on the map.
- Fixed: `Map` `SitRep Status` mode now suppresses station-to-station links entirely, so recency changes and active link selections no longer clutter the status-only situational view.
- Changed: `Settings` now hides focused radio readiness guidance once the selected radio or dialog draft is already `Ready`, groups radio software-used choices into cleaner Fast Light and JS8 rows with `VarAC` on its own row, and reshapes `VarAC Settings` into clearer `Paths and Launch`, `BBS Settings`, and `Vault / VGuard Settings` subsections for the selected radio.
- Fixed: `VarAC.ini` BBS allowed-callsign sync now writes comma-separated callsigns without added spaces, matching VarAC's expected list style more closely.
- Added: radio-scoped `Managed BBS Services` now extends the Managed Vault workflow with alias-driven virtual folders, callsign-aware root-menu visibility, VarAC.db session parsing, optional FLAMP relay queue/block responses, and clearer help for menu-style BBS exchanges while keeping each radio's live BBS directory stable for VarAC.
- Added: CommStat now has first-class `Messages` tab artifacts for `CommStat StatRep`, `CommStat Message`, and `CommStat Alert`, with local staging, first-pass merged provenance, and filter entries for `CommStat`, `CommStat/StatRep`, `CommStat/Message`, and `CommStat/Alert`.
- Changed: CommStat status artifacts now use stronger near-time semantic dedupe across CommStat and JS8Spotter-adjacent paths, reducing duplicate report rows in `Messages` while preserving the unified SitRep model used by `Map`, `Operator History`, and status rollups.
- Added: `Settings -> VarAC Settings` now includes a radio-scoped `Managed BBS Vault` workflow with named locations, hashed access codes, managed-root initialization/import, default-location reset, background trigger handling from the VarAC traffic log, and compact status visibility in `Settings`, `ControlFreq`, and `Messages`.
- Changed: `Settings -> VarAC Settings` now manages BBS allowed callsigns through a lookup-assisted selected list with manual callsign fallback, reducing whitelist typos while preserving the existing VarAC.ini-compatible callsign format for both the station view and radio-scoped VarAC settings.
- Changed: crowded Settings, ControlFreq, and Map control bands now use roomier grouped layouts, including calmer top-level settings rows, a less cramped radio software chooser, a stacked setup-review banner, and a less compressed map filter bar for a friendlier operator experience.
- Fixed: multi-rig runtime readiness now respects explicit per-radio software participation more strictly, so an active/default radio can intentionally opt out of JS8, Fast Light, or VarAC without stale projected global paths keeping setup reminders or status LEDs alive.
- Fixed: FLDigi Net Control macro/status chips now derive their colors from the active theme so dark mode no longer shows pale light-mode pills in the macro workspace strip.
- Changed: `Map` support now exposes `Copy Diagnostics` only while the map is `warming`, `loading`, or `degraded`, keeping the ready-state map UI quieter while preserving the support export when it is operationally relevant.
- Fixed: multi-rig `ControlFreq` and `Settings` setup review now read the current saved operator identity, frequency prompt, and VarAC BBS readiness keys consistently, eliminating false `Callsign missing` warnings and keeping readiness guidance aligned with the actual saved configuration.
- Fixed: `Map` now uses a staged refresh lifecycle with coalesced light/medium/full refresh requests, safer load-failure handling, inline recovery actions, and copyable diagnostics so tab switches and filter changes are less likely to trigger unstable redraw behavior.
- Changed: multi-rig `Map` background refresh behavior is quieter when the tab is hidden, while visible-map refreshes now emit structured telemetry to make future field stability reports easier to diagnose.
- Changed: `Messages` now adds `Copy Summary` for inbox/compose support context and skips unnecessary pending-backlog table rebuilds when the pending data has not changed.
- Changed: several shared `ControlFreq` schedule/operator read paths now use the same SQLite helper discipline added for this slice, improving consistency around busy-timeout behavior and supportable performance instrumentation.
- Changed: readiness guidance now uses the same shared wording model in `Settings` and `ControlFreq`, and `ControlFreq` adds `Copy Summary` so operators can quickly share the current setup review with support or teammates.
- Changed: `SOP Builder` now pauses its UI refresh timers while the tab is hidden, matching the broader 1.2.3 lifecycle cleanup that keeps hidden-tab overhead lower without changing active workflow behavior.
- Added: `Settings -> VarAC Settings` now supports conflict-aware `VarAC.ini` BBS write-back with explicit `Sync From VarAC.ini` and `Write to VarAC.ini` actions, while preserving the rest of the file and keeping VarAC as the source of truth.
- Added: multi-rig `VarAC Settings` now manages BBS access per radio, including `VarAC.ini`, BBS enablement, announce mode, access limiting, and allowed callsigns, while `Messages` adds a `Manage VarAC BBS` shortcut and live BBS access summary; guide/help text now credits KG5RKW for the Vault and VGuard operational inspiration.
- Added: multi-rig `VarAC Settings` now also includes an opt-in VGuard-style file-protection slice for VarAC inbound transfers, with explicit log-only/delete/quarantine modes, a quarantine folder picker, retry timing, and a background guard job that watches the VarAC traffic log without conflating file enforcement with BBS access control.
- Added: first-wave contextual in-app help for `Settings`, `Messages`, `ControlFreq`, and `Map`, using focused `Help` actions that deep-link into the relevant guide section instead of relying on the full guide alone.
- Added: `Peer Schedules` now supports manual peer HF schedule entry and row editing in the UI, treats manual rows as authoritative explicit schedule data alongside imports, refreshes `ControlFreq` / `Map` immediately after save, and conservatively upserts peer operator identity/group metadata into `operator_checkins`.
- Fixed: `Map` activation is now more crash-resistant on Linux and Windows by deferring first-ingest startup until after the first successful page load, coalescing render requests raised during page load, and preventing overlapping HTML/page replacement while Qt WebEngine is still loading.
- Fixed: DB admin/init tooling now routes through the same runtime schema initializer and migration helpers used at app startup, so 1.2.3 DB changes are applied consistently for both fresh installs and upgrade installs.
- Changed: FLDigi macro mappings now open in a high-confidence default view and add a confidence filter control so operators can quickly review clearly identified mappings or expand to all rows.
- Changed: Multi-rig schedule assignment is now radio-first in Settings: `Radio Profiles` shows each radio's `Assigned Schedule`, adds direct `Assign Schedule...` and `Restore Schedule` actions, and keeps the advanced assignment grid available under clearer `Schedule Profiles` and `Radio Schedule Assignments` language.
- Changed: Multi-rig `JS8Call Settings`, `Fast Light Settings`, and `VarAC Settings` now stay close to the familiar single-rig layout while editing a selected radio bundle instead of one global shell, with a new `Radio Software View` selector clarifying which radio owns those software settings.
- Changed: Multi-rig `VarAC Clusters` and `VarAC Memberships` now sit directly below `VarAC Settings` and only appear when `Enable Cluster Mode` is turned on; cluster data is preserved when the mode is off, reducing noise for ordinary single-VarAC operators.
- Changed: Multi-rig `Launch Control` now explains which Station Default radio bundle it is acting on, shows only apps that belong to that projected radio bundle plus global custom tools, and gives clearer guidance when no default radio has been selected.
- Added: Multi-rig `Radio Profiles` now include a searchable radio-model picker that prefers the local Hamlib `rigctl -l` catalog when available, falls back to a bundled common-rig list when it is not, supports an explicit catalog refresh, and persists the selected model identity with each radio profile for clearer operator setup.
- Changed: Multi-rig `Add Radio` and `Edit Radio` now use software-aware filtering so FLDigi and VarAC sections only appear when that radio is set up to use them, keeping the dialog closer to a guided radio workflow.
- Changed: Multi-rig setup review now carries radio-specific readiness context through the shared readiness engine, shows richer per-radio diagnostics in `Radio Profiles`, and `ControlFreq -> Review Now` can jump directly to the radio row that needs attention.
- Changed: Multi-rig startup setup review dismissal is now more operator-friendly: `Dismiss` suppresses the same review digest across restarts until the setup state changes, and `Do Not Remind Again For This Version` suppresses reminders for the current app version only.
- Changed: `Settings -> Radio Profiles` now includes an inline readiness detail summary for the focused radio, so operators can see the radio-specific setup checklist and resolution hints without opening another dialog.
- Changed: Multi-rig `Add Radio` and `Edit Radio` now use a scrollable sectioned layout with click-for-help `?` affordances, a collapsible optional-groups section, wider combo sizing, and a live in-dialog radio readiness panel to better coach setup while editing.
- Changed: Multi-rig `Radio Profiles` now surfaces a highlighted full-width readiness card near the top of the tab for the focused radio, and the `Add Radio` / `Edit Radio` dialog now shows the live radio-readiness guidance in a matching top support card instead of burying it lower in the form.
- Changed: Multi-rig `Operating Profiles`, `Device Assignments`, `VarAC Clusters`, and `VarAC Memberships` now use the same top-of-section guidance-card pattern as `Radio Profiles`, with focused row guidance, fuller-width support text, and row-selection context to reduce operator guesswork while configuring related settings.
- Added: Multi-rig `Radio Profiles` now includes `Copy Readiness Summary` so operators can copy a compact in-app readiness digest to the clipboard for support or self-review without leaving Settings.
- Changed: Multi-rig `Radio Profiles` now model a fuller radio software bundle instead of implying one backend plus extras: `Primary Rig Control` is now distinct from the radio's `Software Used` stack, `JS8Call`/`JS8Spotter`/`CommStat` are explicit per-radio options alongside Fast Light and VarAC, and the active/default radio's compatibility projection now only carries the software that is actually enabled for that radio.
- Added: Multi-rig now includes a shared station-readiness review path that warns about missing default or active radios, incomplete active-radio backend settings, and key JS8, Fast Light, and VarAC setup gaps without requiring blocking startup popups.
- Changed: Multi-rig `Device Profiles` are now presented as `Radio Profiles` in Settings, and `ControlFreq` plus `Settings` now show only status LEDs for configured integrations instead of always rendering every supported software indicator.
- Added: `Settings -> VarAC Settings` now includes an explicit `VarAC Outbox Directory`, `Messages -> Compose` uses that configured Outbox path for staged VarAC copies, and `Settings -> Custom Tools` can define named script/tool launch commands that also appear in `Launch Control`.
- Added: `Messages` now includes a stage-only `Compose` mode for outbound CUSTOM and standard blank-form traffic, with standardized filename previews, optional FLAmp signing, and staging targets for FLMsg, FLAmp, VarAC Outbox, and VarAC BBS.
- Fixed: Cross-platform font rendering now stays more consistent across the main navigation accordion headers, HF/Net schedule menu-style action buttons, and Map overlay text by aligning shared button baselines and scaling map legend text from the UI text-size setting instead of fixed pixels.
- Fixed: Minor UI polish across Settings, ControlFreq, Map, HF Frequency Schedule, and Net Schedules: settings sections now scroll instead of compressing rows, combo popups size to their contents, default logging is disabled, ControlFreq defaults to Schedule view, map legend text is larger, and schedule-resource action labels/buttons use consistent sizing.
- Fixed: Main navigation accordion headers now use the same font as the child navigation buttons so `NCS`, `Schedules`, and `Operators` render consistently across operating systems.
- Changed: Informational popups now show as non-blocking auto-closing notices across the UI, while warnings, errors, and confirmation prompts still require operator action.
- Fixed: Messages now cleans and normalizes filenames before copying files into the VarAC BBS folder, preventing Linux-visible names with backslashes, trailing spaces, or other unsafe characters from causing VarAC BBS handling problems.
- Fixed: FLAmp file authentication now verifies canonical `-sig.k2s` / `-sig.b2s` files, dot-style signed names, and detached `.k2s.sig` / `.b2s.sig` sidecars through bounded FLAmp-origin scanning instead of skipping common operator naming variants.
- Changed: FLDigi Net Control now uses a unified editable local roster table as the primary working surface, with category outputs and live check-in file sync derived from the table instead of the legacy left-side text buckets.
- Added: FLDigi macro-profile discovery and mapping for `.mdf` files now parses structured `<FILE:...>` references, keeps review-only path guesses separate from authoritative detections, and lets operator-configured rows persist without activating mapped mode until a complete enabled mapping exists.
- Added: Macro-profile persistence now stays scoped by absolute profile path, supports profile-local custom-name fallback, and preserves incomplete or disabled operator edits as reviewable stored state instead of silently discarding them.
- Added: FLDigi log-assisted intake now supports session-owned TX context as a review-only annotation, carries the most recent TX prompt across incremental polls, and keeps TX lines out of inbound candidate generation and saved check-ins.
- Fixed: FLDigi role-sensitive operator add controls now replace the legacy add-to-main wiring, and the Joiner role is shown with UI-facing `Joiner` labels while internal role normalization remains uppercase.
- Fixed: Task 2 regression coverage now exercises the real `.mdf` fixture, manual fallback review paths, and the activation/persistence split so the new workflow stays reviewable before Task 3.
- Fixed: Task 3 workspace refinement now surfaces enabled custom mappings as visible workspace cards and keeps compare-source options aligned with the current role and mapped profile without mutating saved mappings.
- Fixed: Compare dialogs now resolve `ncs_reference` / `ancs_reference` back to the visible reference pane, and QRU check-ins now persist and import alongside main check-ins instead of being dropped at save/end-net time.

## [1.2.2]
- Changed: The Linux guided installer now converts installed app checkouts to a runtime-oriented sparse checkout that excludes `tests/` and other developer-only paths, so end-user installs and later `git pull` updates in that managed install folder stay lean while developer/source clones remain unchanged.
- Added: CommStat sitrep fusion now decodes CommStat 4.x sitrep-bearing traffic into the unified `SitRep` pipeline, preserves CommStat remarks/brevity/transport/report-group metadata, merges CommStat and JS8Spotter provenance into the latest sitrep view, seeds untrusted operator rows from CommStat sitrep traffic, and adds fast state rollups plus richer sitrep metadata in Messages, Operator History, and Map.
- Added: `HF Operator History` now uses the same high-contrast checkbox indicator styling as `Messages`, and `Manage Operators` now includes `Sync to VarAC` to reconcile `VarAC_callsign_tags.conf` from known operator rows using the unquoted `CALLSIGN,NAME / STATE / GROUP1 / GROUP2 / GROUP3 / ROLE` format while add/edit/import/delete workflows keep the managed VarAC callsign-tag entries aligned.
- Fixed: `ControlFreq` `Next Change` now shows the upcoming target frequency instead of repeating the currently active scheduled frequency.
- Fixed: `ControlFreq` `Next Change` now falls back to the same upcoming row set used by `Schedule Outlook`, preventing `Next Change: --` when no row is active now but a later scheduled change is already visible in the outlook list.
- Fixed: `ControlFreq` frequency hero now re-syncs to the actual active radio frequency more reliably after automatic schedule-driven QSY and other runtime frequency changes, while still preserving an intentional pending user selection.
- Fixed: `ControlFreq` `Activity` now honors the selected recent-time window directly instead of hiding valid traffic through schedule-start narrowing; recent overnight/current traffic is no longer dropped simply because the nearest daily-schedule row starts on another band.
- Changed: `ControlFreq` `Activity` now excludes `@GROUP` address tokens from `Callsigns Seen`, adds a short filter/data-aware result cache, and benefits from new low-risk time-query indexes for `js8_messages`, `spotter_traffic`, and `fldigi_checkins`.
- Changed: `Settings` section navigation now uses reusable section-health warnings for incomplete single-radio setup, highlighting core identity/groups and clearly partial `JS8Call`, `Fast Light`, and `VarAC` configuration without warning on untouched optional integrations.
- Fixed: `Settings` `JS8Call` section-health warnings now flag a missing `DIRECTED.TXT` when other JS8 integration paths are already configured.
- Fixed: `Settings` section-health no longer treats `CommStat` as requiring `JS8Spotter forms`, and `FLDigi Log Path` no longer creates a missing-`FLDigi` warning by itself.
- Changed: `Settings` `JS8Call` section-health now keys JS8 completeness off `JS8Call Install Folder`, requiring host, TCP port, and `DIRECTED.TXT`, while `JS8Spotter` only warns when its forms path is missing. The `JS8Call Install Folder` field is now shown above `DIRECTED.TXT`.
- Fixed: `Settings` selected section headers now mirror warning-state health, so missing configuration remains visible even when the current nav item’s selected style hides the warning tint.
- Fixed: `Settings` left-nav warning sections now stay visibly highlighted whether selected or not, instead of relying only on the clicked/selected state.
- Fixed: `Settings` left-nav custom rendering now gives items more vertical room so label descenders are not clipped.
- Changed: `Settings` section-health now also warns when `FLMsg`/`FLAmp` executables are set without their companion message folders, when `FLRig`/`FLDigi` executables are set without required endpoint fields, and when `VarAC Install Folder` is set without `Incoming Files`.
- Changed: When a schedule hold is already active, changing the hold-duration preset from `ControlFreq` or the main sidebar now immediately adjusts the active hold window instead of only changing the default for the next hold.
- Fixed: `HF Operator History` CSV import now accepts UTF-8-with-BOM files such as Excel `CSV UTF-8` exports.
- Fixed: `Messages` now treats `.k2s` fallback payloads like `.b2s` transport forms so received NBEMS transport files decode through the existing form-friendly renderer instead of generic unknown-form output.
- Fixed: `Map` station markers now honor the selected group/region filters in station-display mode, and marker filtering now uses merged operator group membership from both `group1/2/3` and stored group lists.
- Fixed: `Map` tooltip activity now distinguishes overall `Last Seen` from direct inbound `Last Contact`; overall last-seen data is shared with `HF Operator History`, and outbound-only attempts no longer appear as contact.
- Changed: `Map` legend is now docked across the bottom of the map area as compact inline key rows, with `Link SNR:` and `SitRep Status:` rendered left-to-right on single rows and conditional `Peer Sched Now:` / `Best Band Now:` rows below only when enabled.
- Fixed: `Map` `Link SNR` legend colors now derive from the same color mapping as live link lines, restoring correct orange/red bins for weak links.
- Fixed: `Map Controls` now let the `Paths to` selector yield width before the `Refresh Links`, `Peer Sched Now`, and `SitRep Status` buttons, reducing button-label compression when the controls drawer is visible.
- Changed: FLDigi log-based RX busy detection now uses a short shared status cache, short-lived resolved-log-path cache, bounded token/timestamp memoization, and targeted perf spans to reduce repeated CPU load during active readable-text receive bursts without changing busy-detection rules.
- Fixed: Reliability Baseline Phase 1 now persists `FLRig XMLRPC Port`, applies the saved FLRig endpoint at startup, and aligns logger/DB-tool paths with the active runtime profile root so diagnostics and DB admin actions operate on the same profile the app is using.
- Changed: Reliability Baseline Phase 2 moved background ingest work off the GUI thread into a serialized worker with duplicate-trigger suppression and worker-local settings objects, reducing startup/UI stall risk without changing ingest cadence.
- Fixed: Reliability Baseline Phase 3 centralizes `operator_checkins` schema ownership in core DB code, upgrades legacy layouts during startup, removes destructive schema rebuild ownership from `Operator History`, and aligns DB tool schema metadata with the unified runtime schema.
- Fixed: Reliability Baseline Phase 4 makes `JS8` and `FLRig` status badges endpoint-aware so the configured instance shows `ok`, while mismatched process-running / endpoint-unreachable cases show `warn` instead of false green status; Settings now probes FLRig status against the currently entered port value.
- Fixed: Reliability Baseline Phase 5 ensures VarAC local tables are created during cold-start DB initialization, eliminating fresh-profile startup errors when `Stations Map` reads `varac_callsign_stats` before the first VarAC ingest run.
- Fixed: Reliability Baseline Phase 6 tracks `SPEC.md` and the Phase 1-5 targeted regression tests in git, replaces the remaining deprecated `datetime.utcnow()` settings path with timezone-aware UTC, and adds FLDigi XML-RPC host/port persistence plus endpoint-aware FLDigi status validation in Settings.
- Fixed: Updater archive extraction now validates ZIP entry paths before extraction, rejecting unsafe absolute or traversal paths and storing downloads under the active runtime config root.
- Fixed: Startup single-instance lock acquisition now uses a single definitive lock attempt instead of redundant checks that could report an already-running state inconsistently.
- Fixed: Scheduler shutdown now stops and tears down the serialized control executor cleanly, best-effort cancels any in-flight control future, and ignores stale control callbacks during app exit to reduce shutdown hangs and orphaned worker-thread risk.
- Fixed: `SettingsManager` now enforces thread affinity at runtime so cross-thread reuse fails fast with a clear SQLite-style programming error instead of surfacing as intermittent thread-bound connection failures later.
- Changed: Main-window teardown now emits targeted debug logging when scheduler/background-ingest/JS8/widget shutdown steps fail, and adds focused regression coverage for scheduler-stop cleanup and settings thread-affinity guardrails.
- Changed: WebEngine startup prewarm now defaults to enabled on Windows and disabled on macOS/Linux unless `map_webengine_startup_prewarm` explicitly overrides the platform default.
- Changed: App/documentation/installer version references updated to `1.2.2`.

## [1.2.1]
- Added: Temporary schedule hold controls now use shared `30` / `60` / `90` / `120` minute presets across sidebar `Schedule Status`, `ControlFreq`, `HF Daily`, `FLDigi / SSB`, `JS8Call`, and prompt-based pause actions, with live countdowns and synchronized near-real-time updates across tabs.
- Fixed: Changing the hold duration in one QSY/Suspend surface now updates the other hold selectors immediately in-process instead of waiting for a delayed settings reload.
- Added: `Messages` now includes a persistent `Hide Types` filter so multiple message types can be hidden from the default view while explicit `MSG Type...` selections still show that type for the current view.
- Changed: `Clear Filters` in `Messages` now resets only the temporary filter row/search controls and intentionally leaves the persistent hidden-type list in place.
- Changed: App/documentation version references updated to `1.2.1`.

## [1.2.0]
- Fixed: Bundled `Net Resources` SitRep seasonal sets were corrected from verified source data; shipped `Winter` and `Summer` resource files now contain the updated schedules, and `Fall` stays aligned with corrected `Winter` as the current fallback set.
- Changed: Existing installs now perform a one-time builtin `Net Resources` refresh during Net Schedule bootstrap so corrected bundled `Winter`/`Summer` rows replace stale builtin rows while preserving user-added/manual resource entries.
- Added: `Settings -> JS8Call Settings` now includes configurable `TCP Host` (`js8_host`) with default `127.0.0.1`, and JS8 status/control/net/map integrations now use the configured hostname/IP instead of assuming localhost.
- Changed: `Settings -> JS8Call Settings -> Load JS8 Traffic` now shows an in-process indicator (button busy state + progress/status text) during manual JS8 traffic rebuilds so long reloads provide immediate UI feedback.
- Fixed: On Windows, launching with `python -m freqinout.main` now sets a FreqInOut-specific app identity/icon early so the taskbar button shows the FreqInOut icon instead of the default Python icon.
- Changed: `HF Schedule` now re-sorts the Active Schedule table immediately after a successful save (using the existing time sort) so newly added rows do not remain visually out of order until reload.
- Fixed: `HF Schedule` save now blocks on row time-format errors (`HH:MM`) instead of performing a partial save that silently skips malformed rows.
- Fixed: `ControlFreq` top-row `Operating Status` LED container now expands correctly to fit status indicators/labels without avoidable clipping from the outer layout spacer.
- Fixed: Clicking `QSY Now` from `ControlFreq` `Schedule Outlook` now forces a hero-frequency resync so the Frequency Control hero indicator reflects the active frequency after QSY.
- Fixed: `ControlFreq` Frequency Control hero indicator now re-syncs when the scheduler changes to a new scheduled frequency, preventing stale hero frequency display after automatic schedule-driven QSY changes.
- Fixed: `ControlFreq` top-row clock/time display now stays contained on narrower window widths by allowing shrink/eliding instead of forcing a large minimum width that could overflow the right edge.
- Added: `SOP Builder` now includes a `Versions` menu (`Save Version`, `Load Version`, `Delete Version`) so operators can keep named HF/Local SOP snapshots and load them back into the builder as drafts.
- Added: `SOP Builder` now provides a guided conflict workflow with collapsible `Activation Defaults` and `Conflict Workbench`, workbench filters, and clearer next-step/readiness guidance.
- Changed: `SOP Builder` `Suggested Start` behavior now auto-computes timing suggestions for timing conflicts and explicitly shows `Not needed` for rows that do not require a timing adjustment.
- Fixed: SOP action-row `Condition Levels` multi-select interaction and persistence were stabilized so saved condition-level edits reliably reload and render across tabs.
- Changed: Main-tab condition-level edits now fan out through a lightweight debounced refresh path so SOP/FreqPlanner/ControlFreq updates are faster and automation-ready.
- Changed: VarAC BBS archive behavior is now explicit and consistent: manual `Archive` moves files from `BBS Directory` to `BBS Archive`, and auto-archive runs on first Messages activation after startup and then periodically (daily).
- Fixed: Linux Launch Control can now start VarAC under Wine using a new optional `VarAC Launch Command` setting; when omitted, path-derived `VarAC.exe` launches are Wine-wrapped automatically when Wine is available.
- Changed: Launch Control readiness is now dependency-aware: `JS8Call` must be API-reachable (not only process-running) before it is marked ready, and launch pacing adds targeted settle delays after `VarAC` and before `JS8Spotter`/`CommStat` when they depend on `JS8Call`.
- Fixed: VarAC custom launch-command mode now hardens launch context by normalizing user/env path tokens and preferring `VarAC Install Folder` as working directory, reducing Linux profile/config drift between desktop-launch and Launch Control.
- Changed: `VarAC Launch Command` UI wording now marks it as an advanced override and recommends leaving it blank unless default auto-launch fails.
- Changed: In-app guide wording now clarifies `Resume Schedule`, FLDigi offset expectations (including Operating Group fallback), and `Prompt` vs `On Schedule Change` behavior.
- Changed: `docs/guide.html` now provides a fuller step-by-step SOP workflow covering `SOP Builder`, `HF Daily` conflict handling, `HF Nets` policy review, `Show Effective Schedule`, and `Return to Normal`.
- Changed: Release perf guidance now explicitly covers the `FreqPlanner`, `HF Daily`, `HF Nets`, and `SOP Builder` paths, and the perf benchmark helper now defaults to dedicated `perf_metrics.log` files instead of mixing in normal app logs.
- Changed: Contextual help now extends beyond the first-wave tab buttons to cover `HF Daily`, `HF Nets`, `Messages` compose and BBS flows, `Map` path controls, and deeper `Settings` sections including `Fast Light`, `HF Operating Groups`, and `Local Comms Groups`.
- Changed: `release_builder.py` now runs `python -m compileall freqinout` by default so the release helper matches the required verification baseline.
- Changed: App/documentation version references updated to `1.2.0`.

## [1.1.9]
- Changed: ControlFreq `Frequency Control` now keeps the schedule-state badge focused on `On Schedule` / `Off Schedule` / `Unknown`, and shows traffic/PTT gating on the QSY action button as `Busy: {reason}` (`PTT active`, `JS8Call`, `VarAC`, `FLDigi`) while the action is temporarily disabled.
- Fixed: Scheduler FLDigi off-schedule detection now distinguishes `FLDigi Mode` vs `FLDigi Offset` (offset drift no longer masquerades as mode mismatch), preserving off-schedule notification while using the existing FLDigi enforcement mode (`On Schedule Change` / `Prompt`) for resolution handling.
- Fixed: FLDigi `Prompt` enforcement no longer immediately re-applies offset drift due to mismatched FLDigi prompt-gating entry-key comparisons; offset-only drift now notifies first as expected.
- Fixed: FLDigi offset drift is no longer re-queued for immediate enforcement by same-entry resume/retry/reapply paths; offset drift can remain off-schedule until prompt/apply or an actual schedule entry change.
- Fixed: FLDigi `On Schedule Change` enforcement now keys off real scheduler row transitions (not internal reapply-key differences), and `Prompt` mode treats changed FLDigi offset drift values as a new prompt cycle.
- Changed: `Resume Schedule` responsiveness improved in ControlFreq and sidebar `Schedule Status` by reducing duplicate resume refresh pulses and repeated scheduler/FLDigi status polling in hot UI refresh paths.

## [1.1.8]
- Added: Accessibility `Text Size` setting in `Settings` with bounded presets (`Normal` 100%, `Medium` 110%, `Large` 125%) applied app-wide without restart.
- Changed: UI text-size presets now persist across restarts via `ui_text_size` and are applied centrally through app-theme refresh.
- Changed: ControlFreq frequency hero display remains fixed-size across text-size presets so dashboard frequency readability stays stable.
- Changed: Sidebar navigation and Schedule Status action controls now auto-size from text metrics to reduce clipping risk at larger UI text sizes.
- Changed: Settings section navigator width now auto-sizes to section labels, improving readability at larger UI text sizes.
- Changed: Settings forms now apply width guards for fixed-width labels/buttons/combos so `Large` text size avoids clipped control text.
- Changed: Messages tab header/filter controls now auto-fit minimum widths from font metrics to improve `Large` readability.
- Fixed: Messages filter combo popups (`MSG Type`, `Status`, `From`, `To`) now expand to fit filter values, and `From/To` dropdown arrow affordance is fully visible.
- Changed: SOP header/action controls now auto-fit minimum widths, and SOP PDF export filter controls now use expandable minimum widths for `Large` text size.
- Changed: SOP Action Rows `Resource` popup width is now capped for a more comfortable dropdown reading width.
- Added: HF Schedule now includes an `SOP Runtime` panel with `Now/Next` source summary, active SOP count, SOP category visibility, and one-click `Activate`/`Deactivate` controls.
- Added: SOP manager now supports lightweight profile summaries with inferred SOP category (`HF`, `Local Net`, `HF + Local Net`) and direct active-state toggles for low-latency UI controls.
- Changed: SOP terminology is now clearer in UI/docs (`Group Name (HF)`, `SOP Group`, `SOP Category`) to reduce user interpretation friction.
- Changed: HF Schedule SOP category labels now render as `SOP-HF`, `SOP-Local Net`, or `SOP-Mixed` to make clear that toggle actions apply to SOP sets only (not the baseline HF schedule).
- Added: HF Schedule now includes a `Schedule Issues` panel with severity tiers (`Conflict`, `Needs Review`, `Info`), guided action buttons, and `Dismiss Until Change` behavior.
- Added: HF Schedule now includes a consolidated `Schedule Resources` workspace showing NET/SOP/HF rows with source/category/text filters and explicit priority ordering context.
- Changed: HF Schedule `Schedule Resources` now excludes NET source rows and supports Active-Schedule parity workflows (`Move Selected to Resources`, `Add Selected/Filtered to Active Schedule`) backed by persisted `hf_schedule_resources`.
- Changed: HF Schedule now uses explicit section labels (`Active Schedule`, `Schedule Resources`) in the same visual style as Net Schedule section headers.
- Fixed: Settings section navigator now shows a vertical scrollbar as needed so lower sections, including `Launch Control`, remain reachable without window resize workarounds.
- Changed: ControlFreq now uses bounded SOP window caches (`Today`/`Tomorrow`) with TTL and explicit cache-bust on SOP/settings changes for long-run responsiveness without restart.
- Changed: ControlFreq settings-save handling is now lightweight when tab is inactive (deferred heavy refresh until activation).
- Changed: SOP tab action-source labels now use `Resource` (replacing `Software`) in Action Rows and Upcoming Actions tables.
- Changed: SOP PDF export now renders a blended single-day action checklist (Time, Resource, Action, Band/Freq, Contact, Description) without Status, and includes complete same-day action occurrences.
- Added: SOP PDF export now includes a separate `Periodic Actions` section (Week(s) of Month, Day of Week, Resource, Action, Band/Freq, Contact, Description) when periodic schedule-layer rows exist.
- Fixed: ControlFreq `Tomorrow` outlook now expands SOP reminder occurrences across the full tomorrow window (not only each action's immediate next due), restoring complete SOP visibility alongside net rows.
- Changed: ControlFreq `Schedule Outlook` second section now shows `Tomorrow` only (not a rolling 7-day window) and uses strict mutually-exclusive day boundaries with `Today`.
- Added: SOP legacy Local Net interval migration now auto-converts common fractional-hour cadence rows (`:15/:30/:45`) into interval + phase (`interval_phase_minutes`) so existing stagger intent is preserved.
- Fixed: SOP save/delete/import/complete now emits a data-change signal that invalidates SOP status caches, forces scheduler refresh, and prompts immediate ControlFreq SOP outlook update.
- Changed: ControlFreq SOP outlook performance improved by reusing a tab-scoped `SOPManager` and using window-aligned SOP prefetches (`Today` from now, `Tomorrow` from tomorrow-start) instead of rebuilding manager/query path per section.
- Added: SOP Interval now supports optional phase offset syntax (`HH:MM@MMm`, for example `03:00@30m`) for staggered reminders; due-time evaluation, JSON import/export, and DB persistence now carry per-action `interval_phase_minutes`.
- Fixed: ControlFreq SOP `Schedule Outlook` now excludes disabled SOP action rows; only active-profile + enabled-action reminders are shown.
- Fixed: ControlFreq `Schedule Outlook` now buckets “Today” using local-day boundaries when `Showing: Local`, preventing tonight-local items from being pushed only into `7 Days`.
- Added: SOP `Local Net` action catalog now includes `NCS`, `Check-in`, and `Message` options (with legacy local action-key compatibility for existing SOP rows).
- Changed: Settings `Local Net Profiles` add/edit/delete now emits a lightweight `local_net_profiles_changed` path to refresh SOP targets without full app-wide settings refresh fanout.
- Fixed: Local Net Profile add/edit/delete now persists `local_net_profiles` directly, so SOP Local Net action targets are available even when unrelated Settings validation would block a full save.
- Fixed: SOP `Local Net` actions remain fully supported for reminders; Layer Sync messaging now explicitly states it applies only to HF/Net schedule actions.
- Changed: SOP `Populate/Rebuild Layer` controls are now disabled when no eligible non-local action rows exist, preventing false impression that local reminders were removed.
- Added: SOP Builder now shows a `Layer Sync` hint (In Sync / Out of Sync / No matching windows) comparing current SOP Layer rows to action-derived candidates.
- Changed: `Rebuild Layer Preview` now gets contextual warning emphasis when SOP Layer drift is detected.
- Changed: SOP layer-sync checks are now debounced and candidate-cached in UI paths to keep editing responsive while still updating guidance quickly.
- Added: Scheduler status now includes source-resolution rationale (`source_reason*`, `sop_selected_reason*`) and next-source transition preview metadata (`next_source*`) for UI transparency.
- Changed: ControlFreq and HF Schedule `Effective Source` / `Next Change` hints now include low-noise rationale and upcoming source-transition guidance when applicable.
- Added: SOP Builder now shows a runtime source hint line with contention context and next-source transition guidance.
- Added: SOP profiles now include a persisted `Priority` value (lower wins) used for runtime SOP-layer conflict arbitration.
- Changed: Scheduler SOP-layer overlap arbitration is now deterministic: `priority` -> profile `updated_utc` recency -> stable row tie-breakers.
- Added: Scheduler status now reports SOP contention metadata (selected profile and contenders) for UI warning surfaces.
- Changed: ControlFreq, HF Schedule, and sidebar Schedule Status now surface explicit SOP contention warnings when multiple active SOP profiles overlap.
- Changed: ControlFreq `Message Summary` row height now uses the shared default table row height for visual consistency with `Schedule Outlook`.
- Added: SOP Schedule Layer row-level pre-save warnings for invalid/missing time or frequency, missing `Periodic` weeks, and potential row overlaps.
- Added: SOP Builder `Rebuild Layer Preview` action with add/remove/unchanged diff summary before applying action-derived layer updates.
- Changed: `Populate Layer from Actions` now uses the same diff-style preview flow so users can safely append missing rows or rebuild with clear impact.
- Added: ControlFreq Frequency Control now shows `Effective Source` so operators can see when `SOP Layer` is overriding baseline HF schedule.
- Added: HF Schedule header now shows `Effective Source` with the same SOP/Net/HF precedence visibility.
- Changed: SOP Schedule Layer defaults new/bootstrap rows to `Daily` recurrence for action-driven workflows.
- Changed: SOP Schedule Layer time columns now follow `Showing: Local/UTC` and convert display/edit values while preserving UTC storage.
- Changed: Removed `VFO` from SOP Schedule Layer UI (VFO resolution now follows Operating Group data/scheduler behavior).
- Changed: SOP Schedule Layer table now uses stretch sizing to better fill available width for easier review.
- Added: SOP Builder now includes `Populate Layer from Actions` for assisted schedule-layer bootstrap from existing SOP action rows.
- Added: Guided SOP layer bootstrap review flow with `Append`, `Replace Existing Layer`, and `Cancel`, including unmatched-action reporting.
- Added: SOP Builder now supports an optional per-profile `SOP Schedule Layer` table (day/recurrence/time/band/frequency/mode) to define schedule windows that override HF schedule when that SOP profile is active.
- Changed: Scheduler source precedence is now `NET > SOP Layer > HF`, with SOP layer rows evaluated only for active SOP profiles and enabled layer rows.
- Changed: SOP alignment checks now evaluate against Daily/Net/SOP Layer windows so reminder mismatch warnings reflect effective schedule behavior.
- Added: New `Local Operators` tab with roster CRUD/search and CSV import/export for local VHF/UHF/GMRS/MURS/FRS operator tracking, now using `first_name` and `last_name` fields in UI and DB.
- Changed: Local Operators table now includes visible `SitRep` status column and supports sortable column headers.
- Added: New `Local NCS` tab with `Start Net`/`Join Net`/`End Net` session controls, local-operator lookup, single check-in log table, SitRep status (`Green/Yellow/Red`), persistent notes, and periodic autosave.
- Fixed: Local NCS lookup/add keyboard flow now mirrors FLDigi NCS expectations (`Enter` on lookup/completion autofills selected operator, while `Enter/Space` on `Add Check-in` performs the add) with guarded exception handling for stability.
- Fixed: Local NCS lookup `Enter` path no longer triggers direct add/check-in writes from completion handlers, eliminating duplicate re-entrant handling that could terminate the app during lookup.
- Changed: Local NCS check-in table is now session-scoped and clears on `End Net` so each net run starts with an empty session list.
- Added: Shared soft CTA button styling (`eligible_*`) for low-noise, state-aware action highlighting across tabs.
- Changed: Contextual action highlighting pass across ControlFreq, FreqPlanner, SOP, Messages, NCS tabs, Operators tabs, Map toggles, and scheduler status panel to better surface next eligible actions.
- Fixed: ControlFreq and FreqPlanner eligibility highlights now stay in sync through selection/click, refresh/rebuild, and timer update paths.
- Changed: ControlFreq and FreqPlanner highlight contrast increased for clearer active-state visibility in both Light and Dark themes.
- Fixed: Shared button stylesheet generation now emits separate `QPushButton` and `QToolButton` rules, restoring runtime action-highlight rendering where combined selectors failed to apply.
- Changed: Active-net reminder moved to sidebar NCS menu buttons (`FLDigi/SSB NCS`, `JS8 NCS`, `Local NCS`), while in-tab start actions remain muted when a net is active.
- Changed: Sidebar NCS labels/order updated to `NCS-FLDigi/SSB`, `NCS-JS8`, `NCS-Local`, with `NCS-Local` positioned directly under `NCS-JS8`.
- Changed: Main sidebar tab-button labels now use left-aligned text for faster scanability across mixed-length tab names.
- Fixed: Main sidebar tab-button typography/style is now normalized across all tabs for matching active/inactive states, with NCS warning highlight only when a net is active.
- Fixed: Map first-open stability by constructing the embedded `QWebEngineView` with an explicit parent, reducing transient close/reopen-style window flash on initial Map tab load.
- Fixed: Additional Map first-click flash hardening by creating the `Map` tab at startup (hidden) and using early offscreen WebEngine warmup so first user activation no longer triggers widget-swap/first-surface startup paths.
- Changed: HF Schedule and Net Schedule now highlight `Save` only when unsaved edits exist (dirty-state tracking), while row-dependent actions (`Delete`, `Move`, resource actions) highlight only when eligible.
- Changed: Settings label `Operating Groups` is now `HF Operating Groups` for clearer separation from local-net workflows.
- Changed: Sidebar labels updated to `FLDigi/SSB NCS` and `HF Operators`, with `Local Operators` and `Local NCS` added to navigation.
- Added: Net Schedule tab now includes a new read-only `Net Resources` catalog with sortable headers, global search, manual set selector, and persistent last-set selection.
- Added: Built-in seasonal net resource files are now shipped in `config/net_resources` (`sitrepnets-fall.json`, `sitrepnets-summer.json`) with in-app citation text: `Visit SitRep.net for more info.`.
- Added: Net Resources actions to `Add Selected to Net Schedule`, `Add Filtered to Net Schedule`, and `Move Selected to Resources` for active-schedule curation workflows.
- Changed: `Import Net Schedule` now imports JSON rows into `Net Resources` by default (resource catalog flow) instead of replacing active schedule rows.
- Added: One-time migration that backfills existing active Net Schedule rows into `Net Resources` (`Migrated` source) without removing active schedule entries.
- Fixed: Duplicate promotion guard now blocks net-resource adds when an active row already matches `(day + start/end + band + frequency + mode)` and shows conflict details for resolution.
- Added: Active Net Schedule rows now persist `resource_id` linkage so edited rows moved back to resources update the corresponding resource entry.
- Added: VarAC background data foundation in local `freqinout_nets.db` with mirrored event/lookup tables for `vmail_folder`, `vmail_relay_notification`, `broadcast` (including `via_callsign`), `cqframe`/`cqframe_type`, and `qso_snr_report`.
- Added: VarAC ingest run telemetry (`varac_sync_status`, `varac_sync_table_counts`) with per-run success/failure, scanned/written row counts, and per-table watermark visibility for diagnostics.
- Added: VarAC callsign trait tracking (`varac_callsign_traits`) for downstream UI/use-cases such as EmComm/BBS/alert context.
- Added: Operators tab unified SitRep projection support (`sitrep_latest_by_callsign`) with effective status display, source summary chips, recency age, and conflict indicators when sources disagree.
- Added: Map tab unified SitRep projection support (`sitrep_latest_by_callsign`) with source chips/conflict metadata in station detail popups for parity with Operators.
- Added: Messages tab fast display-level SitRep dedupe that suppresses raw Spotter duplicates when equivalent normalized SitRep rows are present (`sitrep_messages_dedupe_enabled`, with optional raw override via `sitrep_messages_show_raw_duplicates`).
- Added: One-time checkpointed local backfill from `spotter_traffic` into unified SitRep staging so pre-existing Spotter forms (`F!104/301/304`) are represented under normalized `SitRep` views.
- Fixed: Peer Schedules overlap computation now evaluates day-aware weekly windows (including overnight ranges) instead of only current-day/current-time clipping, improving overlap accuracy.
- Changed: Peer Schedules overlap column now prioritizes actionable reachability windows (`NOW`, `Today`, or next `Day HH:MM-HH:MM`) for faster operator decisions.
- Added: Background inferred peer schedules from recurring observed traffic (`js8_links`/`varac_links`) with confidence metadata and effective-source precedence so imported schedules override inferred rows per callsign.
- Changed: Peer schedule inference scoring now normalizes recurrence by each callsign's observed active weeks (instead of a fixed global window) so valid recurring peers populate in sparse/partial datasets.
- Changed: Peer schedule inference now canonicalizes portable/mobile callsign suffix variants (for example `/P`, `/M`, `/Z`) into base callsigns for grouping, while leaving raw link records intact for provenance.
- Fixed: Inferred schedule identity split for suffix variants (for example `K0RPG` and `K0RPG/Z`) by merging recurrence scoring to a single base callsign.
- Fixed: ControlFreq `Schedule Intersections` overlap detection now evaluates next-2-hour weekly segments (including overnight/day-wrap) and reads schedule rows from both settings/nets DB locations, restoring peer overlap population.
- Fixed: Map `Peer Sched Now` now computes active schedule-frequency matches using both local schedule sources plus current scheduler frequency, with normalized day/overnight evaluation and practical same-frequency tolerance; matched peers are kept visible on the map even without fresh link traffic.
- Changed: Map `Peer Sched Now` legend is now a compact color-key (Green `NOW`, Blue `Later Today`, Purple `QSY <10m`) with matching marker-ring states, replacing the wide descriptive text block.
- Changed: VarAC ingest pipeline now includes concurrency protection and short cadence throttling to avoid overlapping ingest runs and reduce background churn.
- Changed: VarAC message mirror schema now stores richer metadata (`folder_label`, `urgent`, `has_attachment`, `via_callsign`) to support true folder semantics and badge-ready UI paths.
- Changed: Operators manual SitRep override behavior now coexists with unified status projection and remains active until a newer fused report supersedes it.
- Changed: Map SitRep status resolution now preserves manual overrides from `spotter_station_status` until newer unified fused status is available.
- Changed: DB admin/schema tooling now includes the expanded VarAC mirror/sync tables so status/init commands report them consistently.
- Fixed: Long-run memory stability risks by bounding previously unbounded runtime caches (propagation empirical blend cache and JS8 form-definition caches).
- Fixed: Map HTML reload path now reuses a managed cache file with cleanup on shutdown (instead of creating unbounded temporary HTML files over time).
- Fixed: Added/ensured `js8_links(ts)` indexing for map recency query paths to reduce progressive slowdown as traffic history grows.
- Changed: Operator auto-ingest trust policy hardened so traffic-discovered operators default to `Untrusted` unless explicitly promoted (manual edit or trusted import workflow).
- Changed: Operator auto-upsert paths now normalize callsigns to base form before DB writes to prevent suffix variants creating separate operator identities.
- Changed: ControlFreq `Frequency Control` now uses a primary digital-style `Now` frequency readout with compact schedule-state badge (`On Schedule`/`Off Schedule`/`Blocked`) and condensed scheduled-vs-active metadata.
- Changed: ControlFreq quick-frequency selector labels are now compact frequency-first entries to reduce visual clutter and speed QSY scanning.

## [1.1.7]
- Added: Expanded benchmark/perf instrumentation for `controlfreq`, `digi_ncs`, and `js8_ncs` activation flows.
- Changed: Performance pipeline updates across `Messages`, `Map`, `Operators`, and `ControlFreq` to reduce first-open and warm-switch latency.
- Changed: ControlFreq activation path now defers heavy refresh phases and keeps status probing off the tab-switch hot path.
- Changed: Map first-load/reload rendering now avoids duplicate work and reduces payload/bootstrap overhead.
- Fixed: Map links no longer disappear after layer/theme changes when payload data is unchanged.
- Fixed: Map grid-layer render regression (`grid_color` unbound local) during map visibility refresh.
- Fixed: FLDigi check-in history writes now use unified `operator_checkins` schema fields (`first_seen_utc`/`last_seen_utc`) instead of legacy `date_added`.
- Fixed: JS8 `DIRECTED.TXT`/`ALL.TXT` incremental read offset handling for large production logs (`telling position disabled by next() call`).
- Fixed: ControlFreq monthly/periodic schedule evaluation now honors day-of-month week semantics (for example first-Sunday nets).
- Fixed: Operators tab `Clear Filters` affordance now highlights when filters are active.
- Changed: App/documentation version references updated to 1.1.7.

## [1.1.6]
- Added: Shared offline propagation core service used by ControlFreq and Stations Map for consistent modeled/blended scoring paths.
- Added: Offline empirical propagation blending from local historical outcomes with confidence gating and recency decay.
- Added: Propagation regression coverage for blend runtime behavior (disabled passthrough and sufficient-history weighting).
- Added: Expanded propagation regression suite coverage for ingest idempotency, modeled fallback/parity, and overnight schedule window semantics.
- Added: Map propagation target controls now include `Region = ALL` for lower-48 national targeting.
- Added: Map Layers propagation target controls (Target Type/Value) now mirror ControlFreq target settings.
- Added: Map station pins now support JS8Spotter `MCF304` impact status coloring with persistent per-callsign state (`green/yellow/red`, default `blue` for unknown), including tooltip timestamp and legend entries.
- Added: JS8Spotter SitRep status ingestion expanded to `F!104` and `F!301` (in addition to `F!304`) with per-form mapping and persistent source metadata.
- Added: Latest-signal-wins SitRep resolution now uses UTC timestamp first with ingest-time tie-break for deterministic status updates.
- Added: Operators tab `SitRep` column with color-chip status display (`R/Y/G/?`) and manual per-callsign status update (`MANUAL` source) that can be superseded by newer reports.
- Added: ControlFreq `Message Summary` now includes a `SitRep` row with filter-aware totals and `R:x  Y:y  G:z` detail counts.
- Added: Map `SitRep Status` mode to show only known-status stations (`red/yellow/green`), hide links, and temporarily override standard map link filters.
- Changed: Map controls splitter now includes a clearer chevron icon affordance in drawer mode for show/hide discoverability.
- Updated: `docs/guide.html` and new `docs/tools-and-scripts.md` for comprehensive v1.1.6 tab/control and maintenance-script guidance.
- Added: ControlFreq Operating Status strip equivalent to Settings (JS8, FLRig, FLDigi, FLMsg, FLAmp, VarAC, JS8Spotter, CommStat).
- Added: Shared software status probe service for consistent process/API status behavior across Settings and ControlFreq.
- Added: Settings IA Phase 2 split of `Radio Software` into `Fast Light Settings` and `VarAC Settings` sections.
- Added: `JS8Call Settings` now include install/launch path fields for JS8Call, JS8Spotter, and CommStat.
- Added: VarAC BBS configuration fields (`BBS Directory`, `BBS Archive`, `Auto-Archive BBS Files`, archive days) with save-time safety validation.
- Added: Messages tab support for VarAC BBS inbox files (top-level scan of configured `BBS Directory`) with `MSG Type = BBS`.
- Added: BBS row actions in Messages tab: `View | Archive | Delete` (`Archive` moves files to configured `BBS Archive` with collision-safe timestamp suffixing).
- Added: `Launch Control` section with ordered app list, per-app `Enabled` and `Launch on Startup`, global `Launch All with FreqInOut`, manual `Launch Configured Now`, and `Stop Launch Sequence`.
- Added: Shared launch orchestration core service with serial startup launch, 30s per-app readiness timeout, and continue-on-failure behavior.
- Added: ControlFreq `BBS Files` summary section with total file count and `Aging Out` filenames (next 24h to archive threshold).
- Changed: ControlFreq now renders `Message Summary` and `BBS Files` as one sectioned table to improve space usage and scanning.
- Changed: ControlFreq layout rebalanced to give more room to `Today` and `7 Days` planning views.
- Changed: ControlFreq layout redesigned into a two-column top stack (`Activity` + `Schedule Intersections` + `Message Summary` on left, `Frequency Control` + `Schedule Outlook` on right) with full-width `Propagation Forecast` along the bottom.
- Changed: `Today` and `7 Days` are now unified into sectioned `Schedule Outlook` in ControlFreq.
- Changed: ControlFreq `Message Summary` now uses direct rows (no section separator rows) and the third column header is `Details / BBS Aging Out` to maximize visible rows.
- Fixed: Scheduler VarAC busy gating now reads both VarAC traffic and main logs (`VarAC_traffic.log`, `VarAC.log`/`varalog.log`) to detect active connections and file transfer windows more reliably.
- Changed: Net-sourced schedule changes now bypass VarAC wait/busy gating so scheduled nets can override active VarAC link hold behavior.
- Changed: Net-sourced schedule changes now apply consistent backend deferral logic across JS8Call, FLDigi, and VarAC (NET bypasses backend busy deferrals; non-NET defers when busy).
- Changed: Legacy `Launch Selected` / app selector controls removed from `Fast Light Settings` and replaced by `Launch Control`.
- Changed: Schedule-tab mode auto-launch is bypassed when Launch Control is enabled to avoid duplicate launch triggers.
- Changed: In Settings, `Operating Status` now appears above `FreqInOut Settings`.
- Changed: Settings sections are now collapsible and default to collapsed at startup (including Operating Groups and FreqInOut Settings) to maximize open-panel workspace.
- Changed: ControlFreq `Inbox Summary` renamed to `Message Summary`.
- Changed: Messages tab defaults BBS-only view to oldest-first when using default time sort.
- Changed: Peer Schedules Local time headers now use `(Local)` labels instead of long timezone names, and the table adds sortable columns plus a `Clear Filters` action for consistency.
- Changed: Daily HF Schedule and Net Schedule headers now explicitly label Day/Start/End columns with `(Local)` or `(UTC)` to match active time display mode.
- Changed: Added `Clear Filters` actions to ControlFreq and Operators for consistent filter reset behavior.
- Fixed: Local/UTC toggle buttons now initialize from saved display mode across Daily HF Schedule, Net Schedule, FreqPlanner, Messages, and Peer Schedules.
- Added: Messages tab `Mark All as Read` action (type-scoped to current MSG Type filter) with batch local DB updates and bulk JS8 inbox sync handling.
- Changed: Operator History table is now sortable by column header as part of list-table sorting consistency.
- Changed: Header-row select-all checkbox contrast updated for Messages and Operators to improve Linux visibility/highlight consistency.
- Added: Non-blocking launch progress indicator (popup + status bar updates) while Launch Control sequences run.
- Added: ControlFreq compact scheduler strip with `Now`, `Next change`, and `Suspend` status indicators.
- Added: ControlFreq Schedule Outlook quick actions per row (`QSY`, `Open Net`, `Open SOP`).
- Changed: App/documentation version references updated to 1.1.6.
- Changed: ControlFreq top panels now use resizable splitters with persisted layout and view/filter preferences.
- Changed: ControlFreq now includes a persisted Focus Mode toggle for reduced-distraction operations.
- Changed: ControlFreq table UX consistency pass (stable row heights, stronger empty states, urgency row highlighting, keyboard shortcuts, and richer operating-status LED tooltips).
- Changed: ControlFreq Operating Status now renders as a compact single-row strip near `Last updated`, and Frequency Control remains in the top tri-panel layout.
- Fixed: ControlFreq urgency highlighting now uses theme-aware contrast in dark mode for Schedule Outlook, Schedule Intersections, and Message Summary.
- Fixed: ControlFreq theme-switch styling now reapplies button styles consistently after light/dark changes.
- Changed: ControlFreq scheduler strip text simplified (`Status`, `Next`, `Suspend`) to reduce visual noise.
- Changed: ControlFreq top layout now uses three sections (`Frequency Control` left, `Activity` middle, `Message Summary` right) with `Operating Status` in the upper status strip and expanded lower-left `Schedule Intersections`.
- Changed: ControlFreq Operating Status LED order now matches requested sequence (`FLRig`, `FLDigi`, `FLMsg`, `FLAmp`, `JS8`, `VarAC`, `JS8Spotter`, `CommStat`).
- Changed: ControlFreq Status/Next/Suspend indicators moved into `Frequency Control`; `Operating Status` moved to a single-row strip near `Last updated`; `Message Summary` moved into the top tri-panel area; `Schedule Intersections` expanded to full left-column height.
- Changed: ControlFreq now keeps `Message Summary` visible in Focus Mode (shared top row with `Frequency Control`), keeps `Frequency Control` at a stable compact height in both Focus modes, and defaults Focus Mode to `On` when no prior preference exists.
- Changed: ControlFreq top-row panel heights now align to `Frequency Control` (Activity + Message Summary when Focus Off, Message Summary when Focus On), and the Focus Mode button now highlights with `info` styling when active.
- Changed: UI button role styling now aliases `info` to the same accent palette as `primary` for consistent action coloring across tabs.
- Changed: Logs access moved out of primary sidebar navigation and into a dedicated `Settings > Logging & Diagnostics` section via `Open Logs`.
- Changed: `Logging & Diagnostics` is now its own Settings section (separate from `FreqInOut Settings`) with responsive action-row wrapping on narrow widths.
- Changed: `FreqInOut Settings` now focuses on control/timer configuration without embedded logging controls.
- Changed: Settings now uses a two-pane section navigator (left section list + right active section) so heavy panels like `Operating Groups` can use full available space.
- Changed: Settings section navigator is now more compact (narrower/shorter) with explicit hover highlighting and concise section labels.
- Changed: Settings section order now places `Logging & Diagnostics` at the bottom of the section list.
- Changed: Active Settings sections now expand to use available vertical space in two-pane mode (instead of fit-to-content height clamping), reducing unnecessary scrolling.
- Fixed: Settings layout no longer reserves extra blank vertical space below the active section pane, allowing large sections (for example `Operating Groups`) to use full available height.
- Changed: `Fast Light Settings` layout was reorganized into a cleaner aligned single-column form (app paths + related file paths + check-in copy actions) for better scanability.
- Changed: `JS8Call Settings` layout was cleaned up with consistent label widths, aligned path/browse rows, and a clearer top control row.
- Changed: `VarAC Settings` layout was cleaned up with aligned install/path rows and a clearer auto-archive policy row (`Enable Auto-Archive`, `After <days> days`).
- Fixed: Settings form-row spacing consistency for `Fast Light Settings` and `JS8Call Settings` by removing extra per-row layout margins.
- Fixed: `VarAC Settings` auto-archive hint now remains directly associated with the auto-archive policy controls.
- Changed: SOP Builder profile selector now defaults to hint text (`Select existing or add new...`) while keeping `New SOP` as the first dropdown option followed by existing SOPs.
- Added: Global `Suspend Schedule` quick action in the sidebar `Schedule Status` panel (toggles to `Resume Schedule` while suspended).
- Added: Scheduler engine public suspend helper (`suspend_schedule(minutes)`) for consistent UI-triggered temporary schedule holds.
- Changed: ControlFreq `Schedule Outlook` action UX now uses clearer `Actions` buttons (for example `QSY Now`), plus a contextual row right-click action menu and inline usage hint.
- Added: Settings logging controls (`Logging Level`, `Enable DEBUG For` 15/30/60 min, `Open Log Folder`, `Export Diagnostics`) with caution tooltips.
- Added: Schedule Status panel now shows a warning-highlighted `Logs: LEVEL` quick-open indicator whenever logging is active.
- Added: Timed DEBUG auto-revert logic restores prior logging level when the selected timer expires.

## [1.1.5]
- Added: ControlFreq refinements (activity window filter, intersections summary, frequency control status, and keyword search).
- Added: ControlFreq now includes net check-ins in activity totals to capture voice net traffic.
- Changed: ControlFreq timezone toggle defaults to Local and supports UTC/Local switching.
- Changed: ControlFreq layout polish (spacing, divider, label tweaks, and column sizing).
- Updated: FLDigi NCS label to Digi/SSB NCS with tab title aligned to FLDigi / SSB Net Control.
- Fixed: ControlFreq SOP rows, inbox column sizing/tooltip behavior, and theme refresh on live theme changes.

## [1.1.4]
- Added: Window icon now uses the FreqInOut desktop asset for consistent taskbar display.
- Added: ControlFreq tab (console-style summary of activity, inbox, schedules, and SOPs).
- Added: Propagation overlay with Actual/Blended/Modeled modes and best-band labels for regions/states.
- Added: Propagation window selector (1h/3h/6h/12h/24h/History) for observed-data weighting.
- Added: Propagation legend colors now follow FreqPlanner band color palette.
- Added: Background ingest for JS8 links, JS8 inbox, spotter traffic, and VarAC to improve tab responsiveness.
- Changed: Lazy-load heavy tabs (Messages, FreqPlanner, Map) to reduce startup time and tab-switch stutter.
- Changed: Pause tab-specific timers when their tab is hidden to reduce background work.
- Changed: Message file scanning now runs asynchronously to prevent UI freezes.
- Changed: Map JS8 ingest starts on first Map activation to reduce idle overhead.
- Changed: FreqPlanner rebuild checks are throttled to reduce unnecessary refresh work.
- Changed: Propagation model uses distance + day/night weighting for more realistic band guidance.
- Fixed: Single-instance lock cleanup now supports both PySide6 stale-lock API names.
- Updated: Linux installer icon refresh/overwrite to reliably apply the latest desktop icon asset.
- Updated: Help tab now opens external links in the system browser.

## [1.1.3]
- Added: SOP Builder tab with per-row band/frequency/software/action/interval/contact rules and manual completion reminders.
- Added: SOP import/export and hidden-row preservation for actions tied to software not yet configured.
- Added: SOP role and callsign contact targeting with primary+secondary group filtering.
- Added: SOP due-state styling (warning/success), overdue grace handling, and Schedule Status "SOP Action in" countdown support.
- Changed: SOP time display now supports UTC/Local toggle with matching start-time label behavior.
- Changed: SOP contact display now omits empty role groups and orders by role priority (HUB then HUB-ALT, NCS then ANCS).
- Updated: Linux installer/docs and DB tooling wrappers/documentation refinements.
- Updated: Release hygiene docs, security policy, and version/license metadata consistency.

## [1.1.2]
- Added: FLDigi log folder setting to detect RX activity from latest fldigi*.log.
- Added: FLDigi log-based busy detection to delay schedule frequency changes when recent RX traffic is present.
- Added: FLDigi gibberish detection with timeout to avoid indefinite holds; BUSY RX can show "FLDigi (gibberish)".
- Changed: Peer Schedule timezone toggle now matches Daily HF Schedule "Showing" style and behavior.
- Changed: FLDigi RX busy delay is skipped during scheduled and ad hoc nets; mode/offset auto-apply waits for FLDigi idle.
- Fixed: Daily nets now render on every day in the planner (not just the first day).
- Added: Schedule Status shows upcoming frequency change countdown within 15 minutes.

## [1.1.1]
- Fixed: Scheduled net handling now matches ad hoc net behavior (no forced corrections during active net).
- Fixed: Net end now clears control backoff/pending state before resuming schedule frequency/offsets.
- Changed: Resume Schedule and end-net resume enforce full schedule immediately when allowed.
- Updated: Guide to document net handling behavior and resume rules.

## [1.1.0]
- Added: VarAC database integration for messages, operator/map context, and delete support.
- Added: Messages tab bulk select/delete with confirmation summaries and flagging (red/green) for follow-up.
- Added: Operator History export by group (multi-select) and group editing in modal.
- Added: Schedule Status panel under tabs with Resume Schedule and off-schedule context.
- Added: Scheduler enforcement timers (Frequency / FLDigi Mode / JS8 Offset) with prompt intervals.
- Added: VarAC busy detection from VarAC_traffic.log to gate schedule changes.
- Added: Fldigi file senders and net check-ins tracking for map tooltip modes.
- Added: Overlap display for peer schedules with counts and details.
- Added: FreqPlanner band/frequency toggle and per-band color picker with persistent storage.
- Changed: Net schedule recurrence UI (periodic weeks of month, daily option, validation).
- Changed: Messages filters and search UX (searchable from/to, spotter message type).
- Changed: Scheduler prompts and labels (Resume/Skip/Pause wording) and prompt behavior.
- Fixed: Multiple UI alignment/visibility issues across tabs (headers, dropdowns, buttons).
- Updated: Guide documentation for new UI, scheduler logic, messages, operators, and overlaps.

## [1.0.7]
- Added: JS8 RX hub for live ingest fanout and map updates without queue starvation.
- Added: Map updates via JS API (no full reload) with payload deduplication.
- Added: Operator History export to CSV with selection-based export.
- Added: FLDigi NCS end-net adds group to operator history based on operating group frequency.
- Changed: JS8 log ingest is incremental with saved offsets (no table clear).
- Changed: Operator History backfill and JS8 log parsing stream line-by-line (lower memory use).
- Changed: Operator History UI: Import/Export menu and Manage Operators menu.
- Changed: CSV import merge rules keep longest grid, prefer CSV name/state, preserve first/last seen.
- Changed: Scheduler mode labels to Normal/Loose/Strict with legacy value mapping.
- Changed: JS8 NCS offset prefers RX.DIRECTED and DT ms column removed.
- Fixed: Map cleanup and JS8 socket shutdown to reduce exit errors on Linux.
- Fixed: js8net RX thread handles socket close cleanly.
- Fixed: Help tab export PDF default filename.

## [1.0.6]
- Unrecorded changes (not captured in this changelog).

## [1.0.5]
- Unrecorded changes (not captured in this changelog).

## [1.0.4]
- Unrecorded changes (not captured in this changelog).

## [1.0.3]
- Unrecorded changes (not captured in this changelog).

## [1.0.2]
- Unrecorded changes (not captured in this changelog).

## [1.0.1]
- Unrecorded changes (not captured in this changelog).

## [1.0.0] - Initial developer bundle
- Compose form editors in `Messages -> Compose` now expand to the full width of the form-fields pane so `To`, `Subject`, `Message`, and similar fields resize cleanly with the window.
- Compose setup selectors now size to fit their option labels so `Priority`, `Send Target`, and `VarAC Copy` remain readable across platforms.
- NBEMS compose now interprets reviewed CUSTOM-form metadata more intelligently, including form-specific `To` defaults, callsign/state/DTG smart defaults, select and datalist rendering, stacked field descriptions, and support for suffixed keys like `L01a`.
- Compose now stages VarAC copies to a resolved Outbox directory instead of reusing the VarAC incoming-files path, and the setup label now reads `Report Title`.
