# PR #316 finding ledger

This ledger is reconstructed from all 17 reviews published by
`icebergai-review-bot[bot]`. `First` and `latest` mean first and latest **proven affected
reviewed heads**. The preserved head `3257555` was not reviewed; no finding is declared
resolved merely because a later commit message says so. All findings are superseded by
the rebuild disposition.

Validation key: every item below was statically inferred from the exact reviewed source
and accompanied by a realistic path or PostgreSQL transaction argument. No bot review
claimed an independent runtime reproduction. Exact-head CI was reported green.

## Semantic roots

| ID | First / latest | Sev. | Subsystem and violated invariant | Evidence and reproduction | Root cause, attempted fix, induced effects | Final status |
| --- | --- | --- | --- | --- | --- | --- |
| F001 | `f7bd74f` / `07b55d4` | high | Clone ownership: edits must affect the draft that will launch | Clone, edit returned Scenario, compare/launch old binding; later share/fork/rebind paths could target or snapshot the wrong Exercise | Attaching the clone made generic Scenario editing fork away. Private-clone exceptions and rebinding added authorization and race paths (F005/F016) | superseded; unverified at preserved head |
| F002 | `f7bd74f` / `a42730b` | high | Freeze boundary: runtime LLM enablement must equal snapshot | Start with LLM disabled, race or perform active update, submit response while snapshot says false | Runtime read mutable Exercise state. A route guard without a shared lock left a launch race; later locked rejection addressed the symptom | superseded; no code salvaged |
| F003 | `a42730b` / `8b14891` | high | One draft truth: Scenario edits and runnable Injects must agree | Clone, edit Scenario content/schedule/metadata, then list or launch stale Inject rows | Scenario JSON and materialised rows were competing authorities. Reconciliation spawned F006/F009/F010/F018 | superseded; architecture removed |
| F004 | `a42730b` / `a42730b` | high | Legacy provenance: unknown launch-time LLM state cannot be asserted | Upgrade an already launched run after its mutable flag changed | Migration copied upgrade-time Exercise state into launch evidence. Later nullable/unknown state was added | superseded; replacement uses `unknown` and no snapshot |
| F005 | `34230ac` / `3ce41fd` | high | Atomic ownership: launch and every config writer use one lock order | Interleave clone edit/rebind/team write with launch; observed orders included Exercise→Scenario and Scenario→Exercise | Locks were added locally around symptoms. Subsequent retry/rebind paths retained cycles or stale contexts | superseded; replacement order is Scenario→Exercise→children |
| F006 | `34230ac` / `fbf5e75` | high | Authored state: Scenario edits cannot destroy custom draft Injects | Add facilitator Inject, edit cloned Scenario, observe delete/reseed or snapshot omission | Destructive reconciliation could not distinguish seeded vs authored rows. Provenance flags led to F018 | superseded; replacement performs no post-launch reconciliation |
| F007 | `34230ac` / `34230ac` | high | Team integrity: every member group must exist in the draft definition | Remove a team on a clone with enrolled members, then launch/use roster | Scenario edit validated one representation but not dependent members | superseded; clone work is deferred and must decide roster semantics first |
| F008 | `484d7e7` / `8b14891` | high | Audience intent: explicit and all-team audiences must remain distinguishable and repairable | Remove/add team around prepared comms or legacy rows and observe stranded/expanded audiences | Reconciliation inferred intent from current lists. `audience_explicit` migration still guessed legacy intent | superseded; legacy intent stays unknown |
| F009 | `245cd6e` / `753c762` | high | Identity: one logical scenario Inject becomes one runnable row | Edit a clone after seed/promotion; old and new rows both survive | Preserve-custom and reseed fixes operated on incomplete provenance and produced duplicates | superseded; clone materialisation deferred |
| F010 | `2a3d387` / `3ce41fd` | high | Schedule authority: explicit overrides survive edits and enter the digest | Set an override, edit Scenario schedule or metadata, then inspect runnable row/snapshot | Default vs explicit schedule was not represented. A new provenance flag and reconciliation introduced races and legacy erasure | superseded; replacement hashes the one draft offset and freezes it |
| F011 | `e8e4701` / `e8e4701` | medium | Comparison: material ordering differences must be reported | Reorder Injects without changing their values and compare versions | Comparison normalised away list order | superseded; comparison is a separate issue |
| F012 | `280bb2d` / `280bb2d` | high | Scenario consumer atomicity: creation cannot attach during an edit decision | Race Exercise creation against clone Scenario edit | Editor locked/checks consumers without serialising new attachment | superseded; replacement creation takes Scenario lock |
| F013 | `fbf5e75` / `9ab65ad` | high | Schema truth: one schema version cannot claim incompatible/incomplete content | Upgrade legacy run, inspect `v1` content missing runnable fields | Migration labelled guessed partial data like an exact new snapshot | superseded; no legacy snapshot is created |
| F014 | `8cf042c` / `8cf042c` | high | Clone completeness: modern frozen custom Injects and overrides cannot disappear | Add custom/override before launch, clone completed run | Clone reconstructed mostly from Scenario rather than frozen material configuration | superseded; explicit clone contract deferred |
| F015 | `9ab65ad` / `9ab65ad` | high | Migration ordering: a revision may query only columns that already exist | Apply migration chain from prior head; snapshot query referenced later-revision columns | Backfill evolved in place while provenance columns moved into later revisions | superseded; one additive replacement migration is tested from prior head |
| F016 | `9ab65ad` / `9ab65ad` | high | Authorization: one facilitator cannot rebind another's active Exercise | Share clone Scenario, submit target Exercise ID owned by someone else | Rebinding was introduced to repair F001 without an initially explicit ownership contract | superseded; no rebind path in replacement |
| F017 | `9ab65ad` / `a17b898` | high | Attachment evidence: clone/reference cannot alias or depend on mutable source path | Clone then delete/change source attachment; clone fails or serves same file | Snapshot stored metadata/path rather than immutable bytes/reference. Embedding/backfill fixes expanded migration scope | superseded; replacement records and verifies SHA-256; clone copying is deferred |
| F018 | `9ab65ad` / `a17b898` | high | Inject provenance: legacy seeded/custom origin cannot be guessed | Upgrade a run with a facilitator row matching Scenario shape, promote/edit clone | Heuristics converted unknown rows into seeded/custom truth. Fixes moved unknown through booleans and changed reconciliation | superseded; replacement leaves legacy child origin NULL |
| F019 | `07b55d4` / `3ce41fd` | high | Frozen selection: empty/removed/draft data must not trigger mutable fallback | Clone an empty snapshot, deliberately removed Inject set, or a concurrently edited draft | Truthiness fallbacks and Scenario reseeding silently replaced authoritative absence | superseded; modern clone is deferred; draft clone not promised |
| F020 | `a17b898` / `8b14891` | high | Communication provenance: prepared launch input and runtime output must not mix | Upgrade/clone after participant, triggered, or facilitator runtime messages; or clone a draft prepared message | Snapshot initially omitted comms; broad backfill then copied all current rows. Filters could not recover timing | superseded; new runs mark origin, legacy stays unknown |
| F021 | `8b14891` / `8b14891` | high | Legacy Inject provenance: current rows are not launch evidence | Modify/add/delete Inject after launch, then upgrade and inspect snapshot | Backfill equated upgrade-time materialised state with launch-time configuration | superseded; replacement creates zero snapshots for legacy runs |

## Every published observation

Classification is about how the observation relates to the evolving PR, not whether the
underlying consequence was serious. `SEMANTIC_DUPLICATE` includes exact repeats and the
same invariant restated with different wording.

| Observation | Round/head | Root | Classification | Published finding |
| --- | --- | --- | --- | --- |
| O001 | R01 `f7bd74f` | F001 | `ORIGINAL_DEFECT` | Cloned drafts cannot retain scenario edits |
| O002 | R01 `f7bd74f` | F002 | `ORIGINAL_DEFECT` | Runtime LLM configuration diverges from the immutable snapshot |
| O003 | R02 `a42730b` | F003 | `ORIGINAL_DEFECT` | Clone edits leave the runnable inject set stale |
| O004 | R02 `a42730b` | F002 | `INCOMPLETE_ROOT_CAUSE_FIX` | Concurrent start can still diverge LLM runtime state from the snapshot |
| O005 | R02 `a42730b` | F004 | `ORIGINAL_DEFECT` | Legacy backfill fabricates launch-time LLM configuration |
| O006 | R03 `34230ac` | F005 | `FIX_INDUCED_REGRESSION` | A clone edit can commit after launch and replace the active inject set |
| O007 | R03 `34230ac` | F006 | `FIX_INDUCED_REGRESSION` | Editing a cloned scenario deletes facilitator-authored draft injects |
| O008 | R03 `34230ac` | F007 | `NEW_IN_SCOPE` | Clone team edits leave enrolled members assigned to invalid groups |
| O009 | R04 `484d7e7` | F006 | `INCOMPLETE_ROOT_CAUSE_FIX` | Clone edits still discard facilitator-authored inject state |
| O010 | R04 `484d7e7` | F008 | `NEW_IN_SCOPE` | Clone team edits strand pre-seeded communications |
| O011 | R05 `245cd6e` | F003 | `SEMANTIC_DUPLICATE` | Clone edits leave the runnable inject set stale |
| O012 | R05 `245cd6e` | F006 | `SEMANTIC_DUPLICATE` | Clone edits still discard facilitator-authored inject state |
| O013 | R05 `245cd6e` | F009 | `FIX_INDUCED_REGRESSION` | Every clone scenario edit duplicates seeded injects |
| O014 | R06 `2a3d387` | F003 | `SEMANTIC_DUPLICATE` | Clone edits leave the runnable inject set stale |
| O015 | R06 `2a3d387` | F010 | `NEW_IN_SCOPE` | Scenario schedule edits are ignored for existing cloned injects |
| O016 | R06 `2a3d387` | F005 | `NEW_IN_SCOPE` | Concurrent draft writes can bypass clone team reconciliation |
| O017 | R06 `2a3d387` | F008 | `NEW_IN_SCOPE` | Explicit draft communications permanently block team removal |
| O018 | R07 `a9f95f0` | F003 | `SEMANTIC_DUPLICATE` | Clone edits leave the runnable inject set stale |
| O019 | R07 `a9f95f0` | F005 | `SEMANTIC_DUPLICATE` | Concurrent draft writes can bypass clone team reconciliation |
| O020 | R07 `a9f95f0` | F010 | `SEMANTIC_DUPLICATE` | Scenario schedule edits are ignored for existing cloned injects |
| O021 | R07 `a9f95f0` | F005 | `SEMANTIC_DUPLICATE` | Concurrent draft writes still bypass clone team reconciliation |
| O022 | R07 `a9f95f0` | F010 | `SEMANTIC_DUPLICATE` | Scenario schedule edits remain ignored for existing cloned injects |
| O023 | R07 `a9f95f0` | F008 | `INCOMPLETE_ROOT_CAUSE_FIX` | Explicit communication audiences permanently block clone team removal |
| O024 | R08 `e8e4701` | F010 | `FIX_INDUCED_REGRESSION` | Concurrent scenario schedule edits can overwrite explicit inject overrides |
| O025 | R08 `e8e4701` | F011 | `NEW_IN_SCOPE` | Scenario comparison ignores material inject list reordering |
| O026 | R09 `280bb2d` | F001 | `FIX_INDUCED_REGRESSION` | Reusing a clone scenario silently disables editing of the original clone |
| O027 | R09 `280bb2d` | F012 | `NEW_IN_SCOPE` | Concurrent exercise creation escapes clone reconciliation |
| O028 | R09 `280bb2d` | F010 | `INCOMPLETE_ROOT_CAUSE_FIX` | Launch snapshots omit explicit schedule configuration used at runtime |
| O029 | R10 `fbf5e75` | F006 | `INCOMPLETE_ROOT_CAUSE_FIX` | Run snapshots omit facilitator-authored inject definitions |
| O030 | R10 `fbf5e75` | F005 | `NEW_IN_SCOPE` | Launch and clone editing acquire locks in opposite order |
| O031 | R10 `fbf5e75` | F013 | `NEW_IN_SCOPE` | Legacy and new snapshots share a schema version despite incompatible configuration shapes |
| O032 | R11 `8cf042c` | F001 | `SEMANTIC_DUPLICATE` | Reusing a clone scenario silently disables editing of the original clone |
| O033 | R11 `8cf042c` | F014 | `INCOMPLETE_ROOT_CAUSE_FIX` | Cloning a run discards its frozen custom injects and schedule overrides |
| O034 | R11 `8cf042c` | F013 | `INCOMPLETE_ROOT_CAUSE_FIX` | Legacy snapshots omit the runnable inject configuration while claiming schema v1 |
| O035 | R11 `8cf042c` | F005 | `SEMANTIC_DUPLICATE` | Concurrent clone editing and launch use an inverted lock order |
| O036 | R12 `9ab65ad` | F013 | `SEMANTIC_DUPLICATE` | Legacy snapshots omit the runnable inject configuration while claiming schema v1 |
| O037 | R12 `9ab65ad` | F010 | `SEMANTIC_DUPLICATE` | Scenario schedule edits remain ignored for existing cloned injects |
| O038 | R12 `9ab65ad` | F010 | `SEMANTIC_DUPLICATE` | Scenario schedule edits are ignored for existing cloned injects |
| O039 | R12 `9ab65ad` | F015 | `FIX_INDUCED_REGRESSION` | Snapshot backfill queries columns added by later migrations |
| O040 | R12 `9ab65ad` | F016 | `FIX_INDUCED_REGRESSION` | Shared-clone forking can modify another facilitator's active exercise |
| O041 | R12 `9ab65ad` | F017 | `FIX_INDUCED_REGRESSION` | Cloned injects share the source attachment file |
| O042 | R12 `9ab65ad` | F018 | `INCOMPLETE_ROOT_CAUSE_FIX` | Migration misclassifies facilitator injects as scenario-seeded |
| O043 | R12 `9ab65ad` | F005 | `SEMANTIC_DUPLICATE` | Launch and scenario editing acquire locks in opposite order |
| O044 | R13 `07b55d4` | F003 | `SEMANTIC_DUPLICATE` | Clone edits leave the runnable inject set stale |
| O045 | R13 `07b55d4` | F009 | `SEMANTIC_DUPLICATE` | Every clone scenario edit duplicates seeded injects |
| O046 | R13 `07b55d4` | F010 | `SEMANTIC_DUPLICATE` | Scenario schedule edits remain ignored for existing cloned injects |
| O047 | R13 `07b55d4` | F010 | `SEMANTIC_DUPLICATE` | Scenario schedule edits are ignored for existing cloned injects |
| O048 | R13 `07b55d4` | F001 | `FIX_INDUCED_REGRESSION` | A shared-clone edit can launch with the pre-fork scenario snapshot |
| O049 | R13 `07b55d4` | F005 | `SEMANTIC_DUPLICATE` | Launch and scenario editing acquire locks in opposite order |
| O050 | R13 `07b55d4` | F003 | `INCOMPLETE_ROOT_CAUSE_FIX` | Legacy run clones cannot reconcile scenario edits |
| O051 | R13 `07b55d4` | F019 | `FIX_INDUCED_REGRESSION` | Cloning restores scenario injects deliberately removed from the source run |
| O052 | R13 `07b55d4` | F017 | `NEW_IN_SCOPE` | Deleting a snapshotted attachment makes later cloning fail |
| O053 | R14 `3ce41fd` | F017 | `SEMANTIC_DUPLICATE` | Deleting a snapshotted attachment makes later cloning fail |
| O054 | R14 `3ce41fd` | F017 | `INCOMPLETE_ROOT_CAUSE_FIX` | Backfilled snapshots still lose attachments after source inject deletion |
| O055 | R14 `3ce41fd` | F010 | `FIX_INDUCED_REGRESSION` | An unrelated legacy clone edit erases frozen schedule overrides |
| O056 | R14 `3ce41fd` | F018 | `INCOMPLETE_ROOT_CAUSE_FIX` | Legacy promotion still misclassifies a sole custom inject as scenario-seeded |
| O057 | R14 `3ce41fd` | F019 | `FIX_INDUCED_REGRESSION` | A valid empty snapshot is replaced with mutable post-launch injects during cloning |
| O058 | R14 `3ce41fd` | F019 | `NEW_IN_SCOPE` | Cloning a mutable draft can mix different scenario and inject revisions |
| O059 | R14 `3ce41fd` | F005 | `INCOMPLETE_ROOT_CAUSE_FIX` | A launch of a concurrently rebound clone can still deadlock with scenario editing |
| O060 | R15 `a17b898` | F017 | `SEMANTIC_DUPLICATE` | Backfilled snapshots still lose attachments after source inject deletion |
| O061 | R15 `a17b898` | F018 | `SEMANTIC_DUPLICATE` | Legacy promotion still misclassifies a sole custom inject as scenario-seeded |
| O062 | R15 `a17b898` | F020 | `INCOMPLETE_ROOT_CAUSE_FIX` | Pre-launch communications are excluded from snapshots and clones |
| O063 | R16 `753c762` | F003 | `SEMANTIC_DUPLICATE` | Legacy run clones cannot reconcile scenario edits |
| O064 | R16 `753c762` | F009 | `FIX_INDUCED_REGRESSION` | Editing a legacy run clone duplicates every scenario inject |
| O065 | R16 `753c762` | F020 | `FIX_INDUCED_REGRESSION` | Legacy backfill clones runtime communications as launch content |
| O066 | R16 `753c762` | F020 | `NEW_IN_SCOPE` | Cloning a draft silently omits its prepared communications |
| O067 | R17 `8b14891` | F003 | `SEMANTIC_DUPLICATE` | Legacy run clones cannot reconcile scenario edits |
| O068 | R17 `8b14891` | F020 | `SEMANTIC_DUPLICATE` | Legacy backfill clones runtime communications as launch content |
| O069 | R17 `8b14891` | F021 | `ORIGINAL_DEFECT` | Legacy backfill records current inject rows as launch configuration |
| O070 | R17 `8b14891` | F020 | `SEMANTIC_DUPLICATE` | Legacy backfill still clones post-launch facilitator communications |
| O071 | R17 `8b14891` | F008 | `FIX_INDUCED_REGRESSION` | Legacy explicit audiences are treated as implicit and expanded to new teams |
| O072 | R17 `8b14891` | F003 | `INCOMPLETE_ROOT_CAUSE_FIX` | A metadata-only legacy clone edit enables later stale runnable content |

No published observation was classified `PRE_EXISTING` or `UNSUPPORTED`: the evidence
paths were tied to #316's change. The medium findings were retained as advisory rather
than promoted to blockers. This does not imply every proposed UX improvement must ship in
the launch foundation; it explains why clone, comparison, reporting, and legacy work are
now separate issues.
