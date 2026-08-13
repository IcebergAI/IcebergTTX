# ADR 0001: one frozen launch representation for new runs

- Status: proposed by the #315 recovery
- Date: 2026-08-13
- Decision: `SUPERSEDE_AND_REBUILD`
- Supersedes the architecture attempted in PR #316, not its evidence branch

## User outcome

A facilitator can prove the exact user-configured state at the first launch of a new
exercise. Later scenario-library edits and runtime interventions cannot rewrite that
launch evidence. Historical runs created before this capability are labelled honestly;
the migration does not invent their past.

## State ownership

Before launch, the mutable source is the exercise's relational draft aggregate:

- its referenced, validated Scenario revision;
- Exercise title and `llm_enabled` flag;
- materialised seeded and facilitator-authored Inject configuration;
- participant roster and enrolment roles;
- facilitator-prepared inbound communications; and
- attachment bytes identified by a SHA-256 digest.

The first `draft -> active` transition is the only freeze boundary. In the same database
transaction it locks the aggregate, serialises canonical schema `1.0` JSON, inserts the
content-addressed snapshot, links the Exercise to its digest with provenance `exact`,
and records the state transition. Any failure rolls back all of those effects.

After launch, `ExerciseLaunchSnapshot.content` is the authority for launch
configuration. Existing relational rows remain the authority only for mutable runtime
state: inject release/resolution, responses, participant communications, comments,
progression, attendance changes, and facilitator interventions. Static fields on
launch-configured Inject rows are an immutable runtime projection of the snapshot;
they are not reconciled again. Runtime-created injects and communications are marked
`created_during_run=true` and are not retroactively inserted into the snapshot.

The snapshot stores user-configured inputs, not nondeterministic model output or ambient
infrastructure. Each AI assessment already records the model that produced it. Provider
credentials and secrets are never snapshot content.

## Canonical content and digest

The digest is SHA-256 over UTF-8 JSON with sorted object keys, compact separators,
finite numbers only, and schema version `1.0`. Array order is retained only when it is
material. Team audiences are sorted; injects, members, and prepared communications are
put into deterministic domain order.

The content includes:

- the complete validated Scenario definition and declared version;
- exercise title and `llm_enabled`;
- every draft Inject's content, audience, group, order, schedule offset, and attachment
  filename/media type/size/SHA-256;
- every launch-time member's stable user ID, group, and captured role; and
- every prepared communication's direction, sender/audience, content, and timestamp.

Incidental row IDs, repository state, filesystem paths, and the exercise creator are not
hashed. Two otherwise equal configurations may therefore share one immutable snapshot.
A material setting change produces another digest.

`ExerciseMember.group_id` is the participant's exercise audience in the draft and in the
current runtime roster. `User.team` may provide its initial enrolment default, but later
global account edits never grant or revoke access inside a launched run. Freeze copies the
launch-time value into the snapshot. An explicit post-launch roster/group intervention may
change current runtime access without rewriting that launch evidence; those two time-scoped
claims are not reconciled into one another.

Attachment bytes remain in managed storage, but the frozen digest is the immutable
reference. Reads verify the bytes and size against that reference and fail closed on
absence or mismatch; they never serve different bytes under the frozen claim.

## Transactions and lock order

Every draft configuration writer and the launch transition use this order:

```text
Scenario
  -> Exercise
      -> Inject rows by ID
      -> ExerciseMember rows by ID
      -> Communication rows by ID
```

Scenario creation/editing and exercise creation take the Scenario lock before attaching
or materialising an exercise. Launch and configuration edits cannot cross: the writer
that commits first defines the boundary. A writer that wakes after an exact launch may
create an explicitly runtime-scoped object, but cannot mutate launch-configured fields.
Concurrent first-launch requests yield one transition and one linked digest; the loser
receives a conflict.

Database triggers enforce the boundary during a one-release rolling update. A previous
binary may continue creating drafts, but its snapshot-unaware `draft -> active` write is
rejected. Child inserts that omit the new origin column lock the parent and are classified
as draft configuration or runtime data from the serialized parent state. Static exact-run
Exercise, Inject, and prepared-communication projections cannot be rewritten through an
old route or direct ORM write.

## Legacy runs

Exactness is unavailable when launch-time history was never stored. Migration states are:

- `pending`: a legacy or new draft is still authoritative and can be frozen later;
- `exact`: a new run crossed the transactional freeze boundary; and
- `unknown`: the run launched before snapshots and cannot be reconstructed exactly.

Child provenance already present on a migrated launched run remains SQL `NULL` (unknown).
Rows provably inserted after the migration are classified from the locked parent state;
this proves their post-upgrade timing, not their historical launch contents. Current
Inject, member, communication, or attachment rows are not copied into a snapshot. Reports
may show current legacy data, but must label it as current/unknown rather than launch truth.
Legacy clone behaviour belongs to the compatibility issue and must either degrade to a
clearly labelled scenario-template copy or refuse a full-fidelity clone.

## Clone and comparison contract (follow-up)

A modern clone will create a new independent draft from frozen launch configuration:

- copy the Scenario definition, seeded/custom Injects, explicit schedules, prepared
  communications, and verified attachment content;
- do not copy participant roster, responses, runtime communications, comments,
  assessments, progression, release state, or other participant-generated data;
- give the new draft independent rows and managed attachment objects; and
- retain explicit lineage outside the content digest.

Comparison will compare material frozen/draft configuration, not database IDs, runtime
history, or timestamps that do not affect execution. This contract is deliberately not
implemented in the launch-snapshot foundation PR.

## Invariants

1. A new Exercise has `pending` provenance and no digest.
2. The first successful launch has `exact` provenance and exactly one digest link.
3. The digest recomputed from stored content always equals the primary key.
4. Snapshot rows cannot be updated or deleted.
5. Snapshot insertion, digest linkage, and `draft -> active` are atomic.
6. Runtime scenario reads for exact runs use the frozen definition.
7. Launch-configured material fields cannot change after the boundary.
8. Runtime-created content is explicit and cannot masquerade as launch configuration.
9. A material pre-launch change changes the digest; intended canonical equivalents do
   not.
10. No migrated launched run is labelled exact or given a fabricated snapshot.

## Deliberate decomposition

The recovery splits #315 into four bounded capabilities:

1. #317 — exact immutable launch snapshots for new runs (this decision and first PR);
2. #318 — snapshot-backed reports and exports;
3. #320 — modern clone, lineage, version comparison; and
4. #319 — honest legacy compatibility.

The first PR does not implement clone reconciliation, legacy reconstruction, broad report
redesign, or a second mutable configuration representation.
