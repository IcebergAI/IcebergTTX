---
title: Scenario authoring
icon: material/file-tree
---

<p class="eyebrow">Authoring</p>

# Scenario authoring

Scenarios are built in the **visual scenario builder** (**Scenarios → New
scenario**) — no JSON editing required. JSON is the **interchange format**:
import a pre-built scenario file with **Import JSON**, and export any scenario
from its detail page to share it or version it.

## The builder

The builder is a three-pane workspace:

- **Outline** (left) — switch between the **Scenario brief** (title, description,
  author, estimated duration, tags, start inject, debrief notes), **Participant
  teams** (the routing groups injects can target), and the list of **injects**,
  each badged as *start*, *branch*, *linear*, or *end*.
- **Editor** (centre) — the selected inject: its ID, title, and content; whether
  participants may submit a **free-text response**; **target teams** (chips —
  leave all unchecked for a shared inject); **progression** (branch options, or a
  linear *next inject* when there are no options); and **expected actions** —
  evaluator cues shown in the facilitator console.
- **Readiness** (right) — inject/branch/targeting counts, a live **validation**
  list (blocking issues disable saving), and a **flow preview** of the path from
  the start inject plus any disconnected nodes.

## JSON format

### Top-level structure

```json title="scenario.json"
{
  "schema_version": "1.0",
  "title": "Ransomware incident",
  "description": "A simulated ransomware attack affecting core infrastructure.",
  "tags": ["cyber", "ransomware"],
  "metadata": { "author": "IcebergTTX", "estimated_duration_minutes": 90 },
  "participant_teams": [
    { "id": "it_ops", "label": "IT Operations" },
    { "id": "legal",  "label": "Legal & Compliance" }
  ],
  "start_inject_id": "inject_01",
  "injects": [ ... ],
  "debrief_notes": "Key learning: notify ICO within 72 hours."
}
```

### An inject

```json
{
  "id": "inject_01",
  "title": "Ransomware detected",
  "content": "SOC has detected encrypted files on 3 servers...",
  "target_teams": ["it_ops"],
  "free_text_response": true,
  "sequence_order": 1,
  "next_inject_id": null,
  "options": [
    { "id": "opt_isolate", "label": "Isolate affected systems immediately", "next_inject_id": "inject_02a" },
    { "id": "opt_monitor", "label": "Monitor and gather more information",  "next_inject_id": "inject_02b" }
  ],
  "expected_actions": ["Notify CISO immediately", "Preserve forensic evidence"],
  "triggers_communications": [
    {
      "direction": "inbound",
      "external_entity": "NCSC",
      "subject": "Ransomware advisory",
      "body": "We have been made aware of a campaign targeting...",
      "delay_after_release_seconds": 120
    }
  ]
}
```

### Fields

`target_teams`
:   IDs from `participant_teams`. A blank array (or omitted) means **all teams**
    receive the inject.

`options`
:   Branch choices shown to participants. Each points at the next inject via
    `next_inject_id`; `null` **ends the branch**.

`next_inject_id` (node-level)
:   Linear continuation for injects **without** branch choices — the participant
    submits a free-text response, then the facilitator releases the next inject.
    This chains injects into a straight-line sequence.

`expected_actions`
:   Evaluator cues shown alongside responses in the facilitator console (and used
    by the LLM assessment when enabled).

`triggers_communications`
:   Messages automatically injected into the comms inbox when this inject is
    released, visible to all teams. `delay_after_release_seconds` staggers
    delivery.

!!! note "Validation"
    The builder's readiness pane validates as you type, and blocking issues
    disable saving; the scenario detail page shows the same validation sidebar.
    Every `next_inject_id` reference must exist, and node-level and per-option
    `next_inject_id` edges are checked for **cycles** — linear chains can't loop.

## Branching model — "pull, not push"

When a participant responds, the service resolves which inject IDs are valid next
steps, but the **facilitator manually reviews and releases** the chosen branch. This
keeps a human in the loop rather than auto-advancing the scenario.

## AI assessment

When enabled on an exercise (and an AI provider is configured via `LLM_PROVIDER` —
Anthropic, Amazon Bedrock, OpenAI, Ollama, or Gemini), the model evaluates each
participant response and produces:

- a **decision-quality rating** — good, adequate, or poor;
- a brief assessment of the reasoning;
- a **suggested follow-up inject** the facilitator can approve and queue.

Assessments appear in the AI-assessment column of each response card, and in the
right pane's AI-suggestions widget. The **Flagged** filter surfaces responses the AI
rated as poor.

## Running an exercise

1. **Create or import a scenario** — build it in the scenario builder, or load a
   JSON file.
2. **Create an exercise** — give it a title, select a scenario, optionally enable
   AI assessment.
3. **Add participants** — search registered users in the Participants panel and
   enrol them; each is assigned a team. Share `/exercises/{id}/participate`.
4. **Start and release injects** — press **Release** to push an inject; participants
   receive it instantly over WebSocket. Review responses and team comments, then
   choose which branch to release next. **Pause** halts new submissions.
5. **Inject communications** — from **Communications**, click *Inject inbound* to
   simulate a message from an external entity (ICO, NCSC, CEO…) targeted at specific
   teams.
6. **Complete and export** — close the exercise, then export the full transcript
   (JSON — injects, responses, comments, members) or the responses table (CSV).

!!! tip "Scenario packs"
    Scenarios can be exported from the detail page and re-imported into a different
    IcebergTTX instance — useful for sharing scenario packs between teams. Two
    sample scenarios (`ransomware_response`, `vendor_outage`) ship bundled and can be
    loaded from **Settings**.
