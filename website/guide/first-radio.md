---
title: Configure Your First Radio
description: Review the saved station checklist and configure one radio through its scoped setup editors.
---

# Configure your first radio

CommsPlan's **Station Setup** page gives you a saved checklist for station-wide
items and a radio-scoped review for each configured radio. The checklist reads
the saved station state; it does not create radio records or change scheduling
by itself.

This page describes the CommsPlan 2.0.6 workflow. Check the
[GitHub Releases page](https://github.com/N1MAG/CommsPlan/releases/latest) for
currently available installation packages and release-specific instructions.

## Open Station Setup

1. Open **Configuration**.
2. Choose **Main → Station setup**.
3. Review the checklist and select the item you want to complete.

Start with operator identity and operating groups. Then select a radio to review
its application assignments, connections, operating behavior, schedule, launch
choices, readiness, and RF Guard settings. Each editor is scoped to the chosen
radio so a change is not accidentally applied to another station profile.

## Review the saved setup

Choose **Review saved setup** for the selected radio to inspect the saved
configuration and understand what is ready or still needs attention. Discovery
or suggested values are not applied simply because they appear in a review.
Save changes through the owning settings editor, then return to the checklist
and review again.

<div class="expectation">
  <strong>Expected result</strong>
  The saved checklist reflects the station configuration. The selected radio's
  summary identifies its software, connections, schedule, and any remaining
  setup items.
</div>

## Scheduling requires an explicit review

Scheduling remains disabled until you choose to enable it. From the selected
radio's schedule actions, use **Review & enable scheduling…** and check the
radio, assigned plan and sources, applications, readiness, and safety state.
Enable only after that summary matches the station you intend to operate. Choose
**Disable scheduling** to stop automatic schedule commands for that radio.
These controls do not bypass transmit/busy protection, endpoint checks, or RF
Guard.

## Continue

- [Understand the operating picture](/guide/operating-picture)
- [See what's new in CommsPlan 2.0.6](/guide/whats-new)
- [Install or upgrade](/install/)
