---
title: Facilitator guide
icon: material/clipboard-play
---

<p class="eyebrow">Running an exercise</p>

# Facilitator guide

This is the end-to-end walk a facilitator takes to run a tabletop exercise —
**prepare** a scenario, **run** the live session, and **learn** from the after-action
review. It follows the bundled **Ransomware Response Quick Start** sample the whole way,
so you can reproduce every step.

!!! tip "Fastest possible start"
    From **Settings**, the sample loader has a **one-click demo** that seeds an exercise,
    starts it, and releases the first inject — handy for a quick look. This guide walks
    the manual path so you understand each control.

## Prepare

### 1. Load or build a scenario

You need a scenario before you can run anything. Either **build one** in the visual
[scenario builder](scenarios.md) (or **Import JSON**), or **load a bundled sample**.

Open **Settings** and load the **Ransomware Response Quick Start** sample — a 45-minute,
four-team scenario (IT Operations, Legal, Executive, Communications) with branching
decisions and a triggered press enquiry.

![Loading a bundled sample scenario from Settings](assets/settings-samples.png){ .shot }

The [scenario cookbook](cookbook.md) has paste-ready recipes if you want to author your
own patterns — linear drills, branching decisions, team targeting, and triggered comms.

### 2. Create an exercise

A **scenario** is the reusable script; an **exercise** is one run of it. From
**Exercises → New exercise**, give it a title, pick your scenario, and optionally enable
**AI assistance** (decision-quality assessment and suggested follow-up injects — needs an
AI provider configured on the server).

![Creating an exercise from a scenario](assets/exercise-create.png){ .shot }

On creation, every inject is **pre-seeded as pending** — targeted injects once per team,
shared injects once. Nothing is released yet; that is the "pull, not push" model in
action (see [step 5](#5-release-injects-pull-not-push)).

### 3. Enrol participants and teams

Open the exercise's **facilitator console** and use the **Participants** panel to enrol
registered users. Each member is assigned a **team** (group) — for the ransomware sample,
map people onto `it_ops`, `legal`, `exec`, and `comms`. A member's team defaults from
their profile `team` when it matches one of the scenario's teams; you can change it any
time.

Participants join at `/exercises/{id}/participate`; observers get the same view read-only.

## Run

### 4. Start the exercise

Press **Start**. The exercise moves `draft → active` and its start time is recorded.
The lifecycle is a simple state machine — **Pause** halts new submissions
(`active → paused`), **Resume** returns to `active`, and **Complete** ends it
(`→ completed`, terminal).

### 5. Release injects ("pull, not push")

The facilitator console is a three-pane console: the **inject tree** on the left, the
**response feed** in the middle, and **participants / AI suggestions** on the right.

![The facilitator console during a live exercise](assets/facilitator-dark.png){ .shot }

Injects don't fire on a timer — you **Release** each one when the room is ready. Release
is only allowed while the exercise is `active`, and only for a `pending` inject.
Participants on the targeted teams receive it instantly over WebSocket.

When a participant responds, the app resolves which injects are valid **next** steps
(the selected option's `next_inject_id`, or the node-level one) and surfaces them as
**Suggested next** buttons on the response card. You review the response, then release the
branch you want — a human stays in the loop rather than the scenario auto-advancing.

![Reviewing a response and its suggested next branch](assets/inject-release.png){ .shot }

### 6. Monitor responses and AI assessment

Each response card shows the participant's stance and free-text reasoning alongside the
inject's **expected actions** — the evaluator cues authored into the scenario. With AI
assistance enabled, the model adds a **decision-quality** rating (good / adequate / poor)
and a short assessment, and can propose an **ad-hoc follow-up inject** you approve or
dismiss from the right pane. The **Flagged** filter surfaces responses the AI rated poor.

### 7. Simulate communications

The **Communications** inbox is a two-pane inbox/outbox for regulator, press, and
executive messages. Click **Inject inbound** to send a message from an external entity to
specific teams during the exercise.

![The simulated communications inbox](assets/communications.png){ .shot }

Scenarios can also **trigger** comms automatically: releasing an inject with a
`triggers_communications` entry drops the message into the inbox after its configured
delay. In the ransomware sample, releasing the **Notification Decision** inject brings a
press "Request for comment" a couple of seconds later. See the
[triggered-comms recipe](cookbook.md#recipe-triggered-communications-delayed-pressregulator-comms).

### 8. Complete the exercise

When the scenario has played out, press **Complete**. This records the end time and
unlocks the after-action review.

## Learn: review, report, and export

Completing an exercise opens up three after-action outputs.

### Review and replay

The **Review** page (`/exercises/{id}/review`) reconstructs the exercise as a single
chronological **timeline** — inject releases, responses (with decision-quality),
communications, comments, and state changes, merged in order. Beside it, the scenario
author's `debrief_notes` show read-only next to an **editable facilitator-notes** box for
your own observations.

![The after-action review timeline and debrief notes](assets/review-timeline.png){ .shot }

### Generated report

The **Report** page (`/exercises/{id}/report`) assembles a shareable after-action report:

- **Overview** — scenario, state, start/end, duration, teams, participant count
- **Executive summary** — optional, AI-drafted (needs an AI provider and AI assistance
  enabled on the exercise) and facilitator-editable
- **Decision summary** — released injects in order, with responses and assessments
- **Communications log** and the **debrief** (scenario prompts + your notes)

Use **Print / PDF** to hand it out, or download the Markdown via **`/report.md`**.

![The generated after-action report](assets/report.png){ .shot }

### Export the raw record

For analysis or archival, export the full transcript as **JSON** (`/export` — injects,
responses, comments, members) or the responses table as **CSV** (`/export.csv`). All
after-action outputs are owner-only and audit-logged.

---

Next: the [scenario cookbook](cookbook.md) for authoring patterns, or
[scenario authoring](scenarios.md) for the full JSON schema.
