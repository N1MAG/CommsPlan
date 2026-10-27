---
title: Operating Picture
description: Understand CommsPlan's Message Intelligence, Spotter forms, Call Finder, plans, schedules, Messages, Map, and Local Mesh workflows.
---

# The CommsPlan operating picture

<div class="doc-badge-row">
  <span class="doc-badge">Message Intelligence</span>
  <span class="doc-badge">Plans and schedules</span>
  <span class="doc-badge">Messages and Map</span>
</div>

**Where to be. When to be there. What to do when you’re there.** CommsPlan
keeps those three questions connected. Your radio and schedule stay in view
while you review traffic, prepare a message, or explore the map. One radio is
enough, and each additional radio can have its own tools and schedule.

The examples below show CommsPlan 2.0.6. Find available downloads on the
[GitHub Releases page](https://github.com/N1MAG/CommsPlan/releases/latest).

![CommsPlan Ops Center demonstration showing two radios and their operating windows](/ops-center.png)

*Demonstration screenshots use scrambled identities and fictional exercise
messages. Radio readbacks and Mesh connection indicators are simulated; no
live radio or Mesh connection was used. Detail images show individual workspaces.*

## Where, when, and what

- **Where:** Your frequency plan brings frequencies, modes, groups, and radios together.
- **When:** Daily schedules and nets tell you what’s current and what comes next.
- **What:** Messages, forms, and operating procedures help you put the plan into action.

This is Comms Unity in practice: moving between your plan, your tools, and
incoming information in one workspace.

## Station setup and radio services

Start with **Station Setup**, your checklist for station details, radio tools,
connections, and schedules. Each radio keeps its own settings, so adding a
second radio does not mix up the first radio’s tools or plan.

**Launch Control** lets you choose which applications to monitor, which to start
automatically, and which to start yourself. CommsPlan checks each radio’s tools
separately to help you see what is ready.

## Message Intelligence

<a id="message-intelligence"></a>

Message Intelligence helps you see what the traffic is telling you. It brings
supported messages into one inbox and adds summaries, topics, locations, status,
and cues that help you find what needs attention. Open the original message to
check the details and where it came from before deciding what to do next.

Messages can come from JS8Call, CommsPlan Spotter, CommStat, FLMsg, FLAmp,
VarAC, BBS, MeshCore, and Meshtastic. Source labels help you distinguish received
traffic from imported or Internet-assisted information. A summary keeps the
original message available; an imported report does not mean your radio heard it.

![CommsPlan Message Inbox showing source filters, intelligence topics, and mixed station traffic](/message-inbox.png)

*Demonstration inbox: the displayed traffic is fictional exercise data with
scrambled identities and no private operator messages.*

## CommsPlan Spotter and CommStat

<a id="commsplan-spotter-and-commstat"></a>

Use the built-in **CommsPlan Spotter** to watch for information of interest,
manage forms, and set up permitted responses to JS8 traffic. Its Forms Library
is shared with Compose and Net Control. Preview a form before importing it;
you can start with the bundled forms and add your own later.

The optional **JS8Spotter** companion is a separate application. You can launch
it from CommsPlan or import selected data. Imported information stays labeled
so you know where it came from.

**CommStat** is also a separate companion. Supported reports can appear in the
same inbox with their status and source information. An Internet-assisted or
imported report is not evidence that your radio heard it. Use Compose for
supported CommStat radio messages; other CommStat actions stay in CommStat.

## Plans and scheduling

<a id="plans-and-scheduling"></a>

Plan Builder brings your **HF Daily** schedule, **HF Nets**, and **SOP**
(standard operating procedure) layers into a named frequency plan. Assign a
plan to a radio and review its operating windows before enabling scheduling.

Use HF Daily for your regular schedule and HF Nets for net times. SOP layers
let you plan for changing operating conditions. Ops Center shows the current
window and what comes next. Holds, busy checks, and manual controls help you
manage changes while you operate.

![CommsPlan Plan Builder combining Daily, Net, and SOP layers](/plan-builder.png)

*Demonstration plan: frequency, net, and procedure names are sample station data for
illustration.*

![CommsPlan HF Daily schedule with source rows and conflict review](/daily-schedule.png)

*Demonstration schedule: the windows and labels are sample operating data, not
a live station schedule.*

## Messages and Compose

<a id="messages-and-compose"></a>

Use **Messages** to review incoming traffic and **Compose** to prepare an
outgoing message. Filter by source, age, group, or topic, or search for a word
or callsign. Open a message to see its original content and useful context.

Compose offers the actions supported by your selected radio application or
connection. These include supported forms, JS8Call messages, CommStat radio
messages, VarAC files, and Local Mesh messages after setup.

Opening Compose does not send anything. Review the radio, recipient, and
message before choosing the final send action.

## Map and Call Finder

<a id="offline-map"></a>

The map works with bundled geography and local station data, so you can use
it without an online map account. Show stations, paths, schedules, message
locations, and available mesh-node positions. Choose layers and filters to
focus on the information you need, then select an item to check its source
and age.

**Call Finder** searches locally loaded US amateur and GMRS license records,
plus Canadian amateur records. Optional postal data can help estimate a
location. These estimates do not show where an operator is right now.
See [What’s new in CommsPlan 2.0.8](/guide/whats-new) for the latest workflow updates and details.

Use the map to support your decisions. A marker or path does not guarantee
that a station is reachable or that a message will be delivered.

![CommsPlan offline operational map with stations, status, and regional context](/operational-map.png)

*Demonstration map: station markers and regional context are illustrative;
they do not disclose a live operator location or guarantee current reachability.*

## Local Mesh

Connect a supported **MeshCore** or **Meshtastic** device to bring local mesh
text into Messages and available node positions onto the map. Mesh devices
work alongside your HF radios, with their own connection settings.

Sending is off by default. After setting up and connecting a device, enable
**Allow Send**, review the channel policy or choose a known direct node, and
open **Messages → Compose → Local Mesh**. Review the message and destination
before confirming the send.

See [Connect Local Mesh](/integrations/mesh) for pairing and connection help.

## Continue

- [Configure your first radio](/guide/first-radio)
- [Install or upgrade CommsPlan](/install/)
- [Connect Local Mesh](/integrations/mesh)
- [Support and diagnostics](/support)
