---
title: Operating Picture
description: Understand CommsPlan's Message Intelligence, FIO Spotter, CommStat, plans, schedules, Messages, Map, and Local Mesh workflows.
---

# The CommsPlan operating picture

<div class="doc-badge-row">
  <span class="doc-badge">Message Intelligence</span>
  <span class="doc-badge">Plans and schedules</span>
  <span class="doc-badge">Messages and Map</span>
</div>

CommsPlan connects station setup to live operating decisions. The multi-radio
architecture is the base application, while a one-radio station remains a
normal supported setup. The station control context stays visible while the
active workspace explains what is happening and why.

![CommsPlan Ops Center showing radio profiles, schedules, traffic, and operational awareness](/ops-center.png)

*Demonstration station: identities are scrambled and exercise messages are
fictional. Radio readbacks and the green Mesh connection status are simulated
for illustration; no live radio or Mesh connection was used.*

## Main, Radios, and station services

**Main** holds station identity, operating groups, defaults, and station-wide
policies. **Radios** gives each radio its own application identity, profile,
ports, paths, message locations, Frequency Plan assignment, and launch policy.
Guided Add Radio and Software Administration use the same saved identities, so
the review shown before saving matches the runtime state afterward.

Launch Control keeps three decisions separate:

- whether CommsPlan monitors an application;
- whether it starts that application automatically; and
- whether the operator starts it manually now.

CommsPlan checks process and endpoint readiness in the context of the selected
radio before treating an application as running. This prevents one radio's
software, schedule, or hold state from being mistaken for another's.

## Message Intelligence

<a id="message-intelligence"></a>

Message Intelligence is the shared interpretation layer for Inbox, FIO Spotter,
Ops Center, and Map. Supported source records are projected into one canonical
station message library with stable source identity and external references.
The projection can add bounded summaries, topics, location cues, status,
actionability, and routing signals while retaining the original message and
its source, radio, endpoint, receive path, or import provenance.

The source label remains meaningful. JS8Call, FIO Spotter, CommStat, FLMsg,
FLAmp, VarAC, BBS, MeshCore, and Meshtastic records can share the station
library without pretending that their transports have the same capabilities.
Historical or imported data remains identifiable as imported, and a derived
summary does not replace the underlying evidence.

![CommsPlan Message Inbox showing source filters, intelligence topics, and mixed station traffic](/message-inbox.png)

*Demonstration inbox: the displayed traffic is fictional exercise data with
scrambled identities and no private operator messages.*

## FIO Spotter and CommStat

<a id="fiospotter-and-commstat"></a>

The built-in **FIO Spotter** service is branded **CommsPlan Spotter** in the
CommsPlan interface. It owns station-wide watches, JS8 Expect responses, access
policies, forms, and reviewed imports. Messages remains the complete traffic
triage surface; FIO Spotter provides focused administration and automation.

The optional external **JS8Spotter** application is a separate companion with
its own installation, profile, and launch identity. CommsPlan can be configured
to launch it or import selected data explicitly. It does not rename the
external companion to CommsPlan Spotter, and external imports retain their
provenance.

CommStat is also a separate companion integration. CommStat artifacts and
JS8-delivered CommStat reports can enter the shared message projection with
status, brevity, source, and transport context when available. A CommStat
record that is Internet-assisted or imported is not presented as proof that a
selected radio heard it locally. Compose stages supported CommStat RF traffic
through the existing guarded path; native CommStat actions remain owned by
CommStat where that is the source application's capability.

## Plans and scheduling

<a id="plans-and-scheduling"></a>

Plan Builder combines saved **HF Daily**, **HF Nets**, and **SOP** condition
layers into a named Frequency Plan. Assign a plan to a radio only after
reviewing its effective windows and RF Guard implications. The plan context
shows which radio and schedule own each window.

HF Daily provides the ordinary schedule. HF Nets contributes named net
windows and their operating expectations. SOP layers describe condition-driven
operating postures. Ops Center then shows the live effective context and the
next meaningful transition. Scheduler actions remain subject to readiness,
busy deferral, holds, manual control, and shared-resource safety boundaries.

![CommsPlan Plan Builder combining Daily, Net, and SOP layers](/plan-builder.png)

*Demonstration plan: frequency, net, and SOP names are sample station data for
illustration.*

![CommsPlan HF Daily schedule with source rows and conflict review](/daily-schedule.png)

*Demonstration schedule: the windows and labels are sample operating data, not
a live station schedule.*

## Messages and Compose

<a id="messages-and-compose"></a>

Messages is the station's multi-source inbox and outbound staging area. Use
source, age, group, topic, and search filters to narrow the bounded projection;
open a row to review its original evidence and derived intelligence. Compose
stages only the fields and actions supported by the selected application or
transport, including FLMsg/FLAmp, JS8Call, Spotter forms, CommStat RF, VarAC
files, and qualified Local Mesh sends.

Opening Compose does not send a message. The selected radio, target, policy,
payload, and final action remain visible before a guarded send. Native source
records, read state, archive state, and source references remain separate from
station-level watches and publication services.

## Offline Map

<a id="offline-map"></a>

Map uses bundled geography and local station data, so it remains useful without
an online tile account, API key, or tile service. Layers can show stations,
paths, traffic, regional intelligence, RF planning, schedule context, saved
pins, peer schedules, and MeshCore/Meshtastic nodes or positions. Select an item
to inspect its source, age, status, and related inbox evidence.

Map is decision support. A path, peer schedule, message, or mesh position is
evidence with a source and age; it is not a guarantee of current propagation,
reachability, or delivery.

![CommsPlan offline operational map with stations, status, and regional context](/operational-map.png)

*Demonstration map: station markers and regional context are illustrative;
they do not disclose a live operator location or guarantee current reachability.*

## Local Mesh

Local Mesh connects supported MeshCore and Meshtastic devices at the station
level. Received text can enter Messages and available node positions can
contribute to Map without turning a mesh device into an HF radio profile.

Outbound use is off by default. After a saved connection is qualified and
connected, enable **Allow Send**, accept the required channel policy or select
a known direct node, and use **Messages → Compose → Local Mesh**. The final
confirmation and adapter serialization protect against overlapping sends.

See [Connect Local Mesh](/integrations/mesh) for pairing, connection, and
offline recovery steps.

## Continue

- [Configure your first radio](/guide/first-radio)
- [Install or upgrade CommsPlan](/install/)
- [Connect Local Mesh](/integrations/mesh)
- [Support and diagnostics](/support)
