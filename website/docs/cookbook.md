---
title: Scenario cookbook
icon: material/book-open-variant
---

<p class="eyebrow">Authoring</p>

# Scenario cookbook

[Scenario authoring](scenarios.md) documents the JSON schema; this page is the
recipe book. Each recipe is a **complete, paste-ready scenario** — drop it into
**Scenarios → Import JSON** and it validates against `ScenarioDefinition` with no
errors, so you can run it as-is or lift the pattern into a larger scenario.

Every scenario needs at least `title`, `injects`, and a `start_inject_id` that names
an existing inject. Teams are optional, but if you declare `participant_teams`, every
`target_teams` entry must reference one of their `id`s.

!!! tip "Check before you run"
    The scenario builder's readiness pane validates as you type, and the detail page
    shows the same sidebar. To re-check a scenario you have already saved,
    `GET /api/scenarios/{id}/validate` (facilitator) returns `{"valid": true, "errors": []}`,
    or `valid: false` with the validation error when it fails.

## Recipe: a linear drill

**Goal** — a straight-line sequence with no participant decision. Each inject chains to
the next with a **node-level `next_inject_id`**; the participant submits free-text
reasoning and the facilitator releases the next step.

```json title="linear-drill.json"
{
  "title": "Phishing report drill",
  "description": "A short linear walk from first report to containment.",
  "participant_teams": [{ "id": "soc", "label": "Security Operations" }],
  "start_inject_id": "reported",
  "injects": [
    {
      "id": "reported",
      "title": "Suspicious email reported",
      "content": "A finance user reports an invoice email with an unexpected attachment.",
      "target_teams": ["soc"],
      "next_inject_id": "confirmed"
    },
    {
      "id": "confirmed",
      "title": "Malware confirmed",
      "content": "Sandbox detonation confirms a credential-stealing payload.",
      "target_teams": ["soc"],
      "next_inject_id": "contained"
    },
    {
      "id": "contained",
      "title": "Containment",
      "content": "Affected mailboxes are quarantined and credentials reset.",
      "target_teams": ["soc"]
    }
  ]
}
```

**What the facilitator sees** — three injects badged *start → linear → end*. Release
`reported`, review the response, release `confirmed`, and so on. The final inject has
no `next_inject_id`, so it ends the flow.

## Recipe: a branching decision point

**Goal** — offer participants a choice that steers the scenario. Each option carries
its own `next_inject_id`. Branches may **converge** on a shared inject; they may not
form a **cycle** (the validator rejects loops across both option and node-level edges).

```json title="branching-decision.json"
{
  "title": "Ransom decision point",
  "participant_teams": [{ "id": "exec", "label": "Executive" }],
  "start_inject_id": "decision",
  "injects": [
    {
      "id": "decision",
      "title": "Ransom demand received",
      "content": "The attacker demands payment within 24 hours to release the decryption key.",
      "target_teams": ["exec"],
      "options": [
        { "id": "pay", "label": "Authorise payment to restore service", "next_inject_id": "aftermath" },
        { "id": "refuse", "label": "Refuse and restore from backups", "next_inject_id": "aftermath" }
      ]
    },
    {
      "id": "aftermath",
      "title": "The morning after",
      "content": "Whichever path you chose, regulators and the board now want answers.",
      "target_teams": ["exec"]
    }
  ]
}
```

**What the facilitator sees** — when a participant selects an option, the console resolves
the matching `next_inject_id` and surfaces it as a **Suggested next** button on the response
card. The team's choice settles *which* branch follows — releasing the one they did not pick
is rejected with `409 Inject is not the current branch for its group`. What the facilitator
controls is *whether and when* that branch is released — by hand, or on a countdown if the
node carries `release_at_minutes` (see [scheduled
release](#recipe-scheduled-release-put-an-inject-on-a-clock)). No branch is ever selected
automatically. Set an option's `next_inject_id` to `null` to make it a dead-end.

## Recipe: team-targeted injects

**Goal** — route injects to specific teams. `target_teams` lists `participant_teams`
`id`s; an **empty or omitted** `target_teams` makes the inject **shared** with every
team. A targeted inject is seeded once per team, so each team's release and responses
stay separate.

```json title="team-targeted.json"
{
  "title": "Cross-team incident",
  "participant_teams": [
    { "id": "it_ops", "label": "IT Operations" },
    { "id": "legal", "label": "Legal" },
    { "id": "comms", "label": "Communications" }
  ],
  "start_inject_id": "all_hands",
  "injects": [
    {
      "id": "all_hands",
      "title": "Incident declared",
      "content": "A confirmed breach has been declared. Stand up your response.",
      "next_inject_id": "ops_task"
    },
    {
      "id": "ops_task",
      "title": "Contain the intrusion",
      "content": "Isolate affected hosts and preserve forensic evidence.",
      "target_teams": ["it_ops"]
    },
    {
      "id": "legal_task",
      "title": "Assess notification duty",
      "content": "Determine whether the breach meets the regulator notification threshold.",
      "target_teams": ["legal", "comms"]
    }
  ]
}
```

**What the facilitator sees** — `all_hands` (no `target_teams`) reaches everyone;
`ops_task` reaches only IT Operations; `legal_task` reaches Legal and Communications.
Participants only ever see injects assigned to their own team.

!!! note "Reachability is not required — but an orphan has a release window"
    `legal_task` above is valid even though nothing links to it: the validator only rejects
    **dangling references** and **cycles**, not unreached nodes.

    It is releasable by hand, but **only until the first response lands anywhere in the
    exercise**. After that, every team has a progression cursor sitting on a real node, and
    an inject no cursor points at is refused with `409 Inject is not the current branch for
    its group`. So use an unlinked node as an *opening* inject you release up front, not as
    something to hold in reserve for later.

## Recipe: triggered communications (delayed press/regulator comms)

**Goal** — fire a simulated inbound message into the comms inbox automatically when an
inject is released. Use `triggers_communications` on the inject.

```json title="triggered-comms.json"
{
  "title": "Media pressure",
  "participant_teams": [{ "id": "comms", "label": "Communications" }],
  "start_inject_id": "leak",
  "injects": [
    {
      "id": "leak",
      "title": "Story is breaking",
      "content": "A journalist has posted about the incident on social media.",
      "target_teams": ["comms"],
      "triggers_communications": [
        {
          "external_entity": "Media desk",
          "direction": "inbound",
          "subject": "Request for comment",
          "body": "We are hearing reports of a cyber incident. Can you confirm before we publish?",
          "delay_after_release_seconds": 120
        }
      ]
    }
  ]
}
```

**What the facilitator sees** — releasing `leak` schedules the message; 120 seconds
later it lands in the communications inbox for all teams and broadcasts over WebSocket.
`delay_after_release_seconds: 0` delivers immediately.

!!! warning "Authored fields differ from the inbox record"
    On a trigger you set only `external_entity`, `direction` (**exactly `"inbound"` or
    `"outbound"`**), `subject`, `body`, and `delay_after_release_seconds`. You do **not**
    set `sender` or `visible_to_teams` — the server fills those in, and triggered comms
    default to **all-team** visibility. To send a team-scoped or same-day message during
    a live exercise, use **Inject inbound** in the Communications panel instead.

## Recipe: scheduled release (put an inject on a clock)

**Goal** — have an inject fire on its own, so the room feels time pressure without the
facilitator having to watch a stopwatch. Set `release_at_minutes` on the inject: it
auto-releases that many minutes after the exercise **starts**.

```json title="scheduled-release.json"
{
  "title": "Pressure builds",
  "participant_teams": [{ "id": "it_ops", "label": "IT Operations" }],
  "start_inject_id": "detect",
  "injects": [
    {
      "id": "detect",
      "title": "Anomaly detected",
      "content": "Your monitoring stack has flagged unusual outbound traffic.",
      "target_teams": ["it_ops"],
      "next_inject_id": "escalate"
    },
    {
      "id": "escalate",
      "title": "It is getting worse",
      "content": "Thirty minutes in, a second business unit reports the same symptoms.",
      "target_teams": ["it_ops"],
      "release_at_minutes": 30
    }
  ]
}
```

**What the facilitator sees** — `detect` is released by hand as usual. `escalate` shows a
live **countdown** in the inject tree and, **provided the team has reached it**, releases
itself 30 minutes after the exercise started. The facilitator can still hit **Release** to
bring it forward, or cancel the schedule to make it manual again.

!!! warning "A timer only fires on an inject the team has actually reached"
    This is the part that will catch you out. `release_at_minutes` does not exempt an inject
    from the progression cursor: an inject can only be released when it is the **current
    branch for its group**. In the scenario above, the team reaches `escalate` only by
    responding to `detect`.

    So if nobody has responded to `detect` when the 30-minute mark arrives, the scheduled
    release is **rejected and silently skipped** — and the timer is **one-shot**, so it does
    not re-arm when the team catches up later. The inject stays `pending` and you must
    release it by hand.

    Two practical consequences:

    - **Leave slack.** Pick an offset the team will comfortably have reached by, not the
      earliest moment the inject *could* make sense.
    - **Don't rely on it for the critical path.** Treat a schedule as a convenience that
      saves you watching a stopwatch, not as a guarantee the inject will appear.

    The **start inject** is the one node whose schedule can never be skipped: a cursor points
    at it from `t=0` and cannot move off it until it has been released and answered.

    An *unreferenced* node (like `legal_task` above) is releasable at the start too — but
    only until the **first response anywhere in the exercise**, after which it is refused
    like any other off-cursor node. So it is not a safe thing to schedule.

!!! note "The countdown is pause-aware"
    The offset is measured in *elapsed exercise time*, not wall-clock time. Pausing the
    exercise defers the timer; resuming re-arms it with the remaining offset. An inject set
    to 30 minutes, in an exercise paused for 5, fires 35 minutes after the start.

!!! tip "Scheduling adds no edge to the graph"
    `release_at_minutes` only controls *when* an inject may release. It adds **no** edge to
    the scenario graph and takes no part in cycle detection. It also does not change who
    chooses the branch — that is still settled by the team's response. Omit the field (the
    default) for manual-only release.

## Recipe: free-text vs option responses

**Goal** — choose how participants respond. `free_text_response` (default `true`)
controls whether the free-text box is offered; `options` control whether stances are
offered. The two are independent, so an inject can have either, both, or neither.

```json title="response-modes.json"
{
  "title": "Response modes",
  "participant_teams": [{ "id": "team", "label": "Response team" }],
  "start_inject_id": "choose",
  "injects": [
    {
      "id": "choose",
      "title": "Pick a containment stance (options only)",
      "content": "Choose one containment posture. No free-text needed.",
      "target_teams": ["team"],
      "free_text_response": false,
      "options": [
        { "id": "aggressive", "label": "Isolate everything now", "next_inject_id": "reflect" },
        { "id": "measured", "label": "Isolate only confirmed hosts", "next_inject_id": "reflect" }
      ]
    },
    {
      "id": "reflect",
      "title": "Explain your reasoning (free-text only)",
      "content": "In your own words, justify the stance your team took.",
      "target_teams": ["team"],
      "free_text_response": true
    }
  ]
}
```

**What the facilitator sees** — `choose` shows only the two stance buttons;
`reflect` shows only the free-text box. Set both `free_text_response: true` and
`options` when you want a stance **and** a rationale on the same inject.

## Recipe: debrief notes

**Goal** — seed the after-action debrief with the author's talking points.
`debrief_notes` is a **scenario-level** field; it appears read-only on the exercise
**Review** page beside the facilitator's own editable notes.

```json title="debrief-notes.json"
{
  "title": "Backup failure tabletop",
  "participant_teams": [{ "id": "it_ops", "label": "IT Operations" }],
  "start_inject_id": "outage",
  "debrief_notes": "Focus the debrief on: time-to-detect, whether the offline backup was trusted, and the decision to fail over vs. restore.",
  "injects": [
    {
      "id": "outage",
      "title": "Primary storage offline",
      "content": "The primary storage array is unreachable and the last backup job failed silently.",
      "target_teams": ["it_ops"]
    }
  ]
}
```

**What the facilitator sees** — these notes surface on the exercise
[Review page](facilitator-guide.md#review-and-replay) as author guidance, next to a
separate editable box for the facilitator's own observations captured during the
exercise.

!!! note "No per-inject debrief field"
    `debrief_notes` lives only at the top level of the scenario. There is no per-inject
    debrief field — use `expected_actions` on an inject for evaluator cues shown against
    each response in the facilitator console.

---

Next: the [Scenario authoring](scenarios.md) reference for the full schema, or the
[Facilitator guide](facilitator-guide.md) to run one of these scenarios end-to-end.
