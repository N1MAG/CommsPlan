---
title: What's New in CommsPlan 2.0.6
description: An operator overview of CommsPlan 2.0.6 station setup, forms, Call Finder, and map updates.
---

# What's new in CommsPlan 2.0.6

<div class="doc-badge-row">
  <span class="doc-badge">CommsPlan 2.0.6</span>
  <span class="doc-badge">Operator overview</span>
</div>

The [GitHub Releases page](https://github.com/N1MAG/CommsPlan/releases/latest)
lists currently available packages, checksums, and release notes. Package
availability can vary by platform, so use that page to confirm which native
installers have been published.

## Saved Station Setup checklist

Station Setup now gives the operator a saved checklist for station-wide setup
and a review of the selected radio. Use the checklist to see what is configured
and what needs attention. Radio-scoped editors keep application, connection,
operating behavior, schedule, launch, readiness, and RF Guard changes tied to
the radio being reviewed.

Scheduling requires an explicit operator decision. **Review & enable
scheduling…** presents the saved radio and schedule state for review before
enabling automatic schedule actions. **Disable scheduling** stops automatic
schedule actions for that radio. Neither control bypasses readiness, endpoint,
busy, or RF Guard checks.

## Shared forms library

CommsPlan Spotter provides a station-level Forms Library shared by Spotter,
Compose, and Net Control. You can review MCF form files before adding them to
the station library. Existing bundled forms remain available, and a custom
folder is optional. The library is separate from radio-specific JS8Call setup.

## Offline Call Finder and source updates

Call Finder searches locally available US FCC amateur and GMRS license records
and Canadian ISED amateur callsign records. It can also use US and Canadian
postal reference datasets for approximate grid information. Search is offline
after the reference data is loaded. Use **Sources…** to inspect source status,
record counts, and last refresh.

**Update all** downloads and imports the five supported datasets one at a time:
US amateur licenses, US GMRS licenses, US postal grids, Canadian amateur
callsigns, and Canadian postal grids. The batch stops on cancellation or
failure; already completed datasets remain available. Call Finder does not
create an operator roster entry unless you explicitly choose and review
**Add to Operators**.

## Worldwide map and license references

The Map supports an **All countries** geographic scope for worldwide evidence.
The map's Layers, Filters, Display, Paths, and Planning categories organize map
controls. CommsPlan remembers a saved starting camera view. Its License
references panel can filter by licensing country (US or Canada), service
(amateur or GMRS), and region. References from Call Finder can be reviewed on
the map using available location data. **Prepare map references** builds the
map's reference index from loaded Call Finder sources. **Search this area**
uses the settled current map view to find matching references. A plotted
license reference follows its postal-derived estimate when available; the
licensing country does not establish where the operator is currently operating
or prove activity at the estimated point.

In net-control screens, **FCC lookup…** can fill the current operator entry
draft from a selected reference. Review that draft before saving it. Lookup
does not add a check-in or transmit, and it does not replace information
already managed in the operator roster.

## Continue

- [Configure your first radio](/guide/first-radio)
- [Understand the operating picture](/guide/operating-picture)
- [Install or upgrade CommsPlan](/install/)
