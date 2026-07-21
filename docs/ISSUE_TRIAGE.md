# Open-Issue Triage & Roadmap

_Snapshot: 2026-07-21. Covers all 25 open issues. This is a planning artifact — issue
state on GitHub is the source of truth; update or delete this file once the work lands._

The open backlog is dominated by a single full-codebase review pass (`fable-review`): 17 of
the 25 issues carry that label, most tagged `P2`. Left as a flat list they read as "many
medium bugs, unclear order." Below they are **regrouped by subsystem/theme** (so each chunk
is a coherent PR or short series with shared test scaffolding) and then **resequenced into
phases** by risk × effort.

Two rules shaped the grouping:

- **Don't split an issue across chunks.** `#269` is a deliberate grab-bag (house precedent
  `#32`); it stays one small PR even though its nits touch three subsystems. Streams that
  its nits are adjacent to cross-reference it rather than absorb it.
- **Cluster by file-locality where priority allows.** Batching issues that touch the same
  service (audit/SIEM, LLM, inject lifecycle) shares test setup and avoids repeated merge
  conflicts in the same files.

---

## Lens A — Themed workstreams (the regrouping)

### WS-1 · Auth & account-takeover defenses — P2 security
| Issue | What | Size |
|---|---|---|
| #258 | Reset/invite link host derived from client `Host` header (link poisoning → takeover) | M |
| #257 | OIDC `email_verified` computed everywhere, enforced nowhere (email squatting) | M |
| #264 | Client session gaps: WS auth-expiry silently freezes page; JWT persisted to localStorage | S–M |

Everything that guards the front door. #258 and #257 are outright account takeover /
squatting via the unauthenticated and SSO entry points — the sharpest issues in the
backlog. #264 hardens the same boundary client-side (removes the localStorage copy that
turns any future XSS into durable credential theft; adds the missing logged-out redirect).

### WS-2 · Audit / SIEM subsystem — P2 security + performance
| Issue | What | Size |
|---|---|---|
| #260 | `OUTBOX` list grows forever in prod (slow-motion OOM, partly attacker-drivable) | S (one-line `deque(maxlen=…)`) |
| #259 | Admins exfiltrate env-only secrets by re-pointing SIEM/SMTP/proxy hosts + "test" | M |
| #251 | No retention/purge for `AuditEvent` + `AuthToken` (unbounded, PII-bearing) | M (migration + sweep timer) |

All three live in `siem_service` / `audit_service` / the audit router — do them as one
subsystem pass. #260 is a quick win to land first. `#269` nit #1 (general-settings audited
under the audit-settings action name) is adjacent — fix it here or in WS-7, not both.

### WS-3 · Live-exercise runtime reliability — P2 (the "room full of people" risks)
| Issue | What | Size |
|---|---|---|
| #252 | WS fan-out: one stalled client blocks every broadcast + the committing request (**severity: high**) | M |
| #250 | Graceful shutdown abandons in-flight background work and armed timers | M |
| #262 | `ExerciseMember` has no unique constraint → concurrent-enrolment duplicate memberships | S (constraint + migration + `IntegrityError` branch) |

The failures that show up mid-session with people in the room: a wedged socket freezing the
facilitator, a deploy dropping in-flight mail/timers, a double-click creating a phantom
membership that survives "removal." #252 is the only `high`-severity issue open — top of
this stream.

### WS-4 · Query & memory hygiene — P2 performance
| Issue | What | Size |
|---|---|---|
| #263 | N+1 in three hot list endpoints (responses, inject-comments, participant injects) | M |
| #245 | `load_exercise_bundle` loads every user in the DB to name ~8 people | S–M |

Same shape (grows with tenant/activity, invisible on demo data), same fix (batch to one
query), same test pattern (a query-count assertion pinning the ceiling). Natural pair.

### WS-5 · Exercise lifecycle & game-integrity correctness — P3
| Issue | What | Size |
|---|---|---|
| #265 | Released injects deletable mid-exercise (destroys evidence, no event); release into just-paused exercise | S |
| #266 | Released-inject payload leaks `next_inject_id` branch topology to participants | S |

Both in `inject_service` / progression; both protect the integrity of a running branching
exercise (preserve after-action evidence; keep the choice's consequences hidden until
made). Shared inject-service test area.

### WS-6 · LLM subsystem fixes — P2 + P3
| Issue | What | Size |
|---|---|---|
| #261 | Admin "test connection" always fails Anthropic/Bedrock (empty cached-context block → 400) | S |
| _#269 (nits 2–3)_ | provider-cache leaks `httpx.AsyncClient`; concurrent executive-summary double-fires | S |

#261 is a user-visible "working config looks broken" bug and small. The two LLM nits from
#269 are adjacent (land them with #269 as one PR — see WS-7 — but review them together with
#261).

### WS-7 · Correctness grab-bag — P3
| Issue | What | Size |
|---|---|---|
| #269 | Five verified nits: audit action collision, leaked httpx clients, summary double-fire, demo seeding, blank participate shell | S (single PR) |

Keep as the one small PR the issue intends. Nits overlap WS-2 (audit naming) and WS-6 (LLM);
whoever takes #269 should coordinate so a fix isn't done twice.

### WS-8 · Release & backup safety — P3 ops / supply-chain
| Issue | What | Size |
|---|---|---|
| #267 | Release workflow signs/publishes images with no CI gate on the tagged commit | S–M |
| #268 | k8s backup CronJob dumps Postgres only — inject attachments on `app-uploads` PVC unbackuped | S (docs) → M (extend CronJob) |

No app-code changes; both live in `.github/workflows` and `k8s/`. Can be owned by whoever
holds infra and run as an independent track.

### WS-9 · Repo hygiene / CI parity — tech-debt (batchable quick wins)
| Issue | What | Size |
|---|---|---|
| #276 | Pre-commit config mirroring the CI static gate (IcebergCTI parity) | S |
| #277 | CODEOWNERS with security-sensitive path owners (IcebergCTI parity) | S |
| #278 | Lint Jinja + hand-authored CSS/JS (djlint + Biome, IcebergCTI parity) | M (may surface lint fixes) |
| #208 | `scripts/screenshots.py` refactor (dedupe seeding, self-describing `Shot`) | S |

Tooling/config, no runtime risk. Good parallel track or warm-up work. #276/#277/#278 are the
IcebergCTI-parity set and pair naturally.

### WS-10 · Scenario-design features — enhancement (sequence after stabilization)
| Issue | What | Size |
|---|---|---|
| #253 | Exercise objectives as a first-class scenario input (schema → author → run → debrief) | L |
| #206 | Visual scenario DAG with path-taken debrief overlay | L |
| #205 | Simulated correspondents: participants reply, LLM drafts persona counter-reply (facilitator-approved) | L |

Product features. **Dependency: do #253 first** — it lands the objectives schema that #206's
debrief overlay is designed to surface (#253 explicitly calls this out). #205 is the largest
(new LLM pipeline + comm threading + schema) and independent — schedule last.

### WS-11 · Multi-replica epic — P2 enhancement (largest, own track)
| Issue | What | Size |
|---|---|---|
| #213 | Five in-memory subsystems cap the app at one replica (WS, rate limits, schedules, comms timers, caches) | XL (epic) |

Track separately. Note the interplay: it depends on the event-seam work (#212, already
closed) and would eventually subsume some in-memory state — **but** #252's per-socket send
hazard survives any transport, and #250's shutdown drain is needed regardless, so those land
first independent of this epic. Don't schedule #213 until the reliability streams are done.

---

## Lens B — Phased sequence (the reprioritization)

### Phase 0 — Quick wins (leverage per hour of work)
`#260` (one-line bounded buffer) · `#261` (visible admin bug) · `#262` (constraint +
migration) — plus the **WS-9 hygiene set** (`#276`, `#277`, `#278`, `#208`) as a parallel
track anyone can pick up. Small, low-risk, high signal.

### Phase 1 — Security & live-session risk (the P2 core)
The issues that cause account takeover or fail with a room full of people:
- **Auth:** `#258`, `#257`, `#264` (WS-1)
- **Runtime:** `#252` (high-sev), `#250` (WS-3, minus #262 already in Phase 0)
- **Audit/SIEM boundary:** `#259`, `#251` (WS-2, minus #260 in Phase 0)

### Phase 2 — Performance & correctness (P2/P3 cleanup)
`#263`, `#245` (WS-4) · `#265`, `#266` (WS-5) · `#269` (WS-7) · `#267`, `#268` (WS-8).

### Phase 3 — Features & scaling
`#253` → `#206` → `#205` (WS-10, in that order) · `#213` epic (WS-11), only after Phase 1
reliability work has landed.

---

## At-a-glance

| Phase | Issues | Theme |
|---|---|---|
| 0 · Quick wins | #260, #261, #262, #276, #277, #278, #208 | leverage |
| 1 · Security & live-session | #258, #257, #264, #252, #250, #259, #251 | P2 core risk |
| 2 · Perf & correctness | #263, #245, #265, #266, #269, #267, #268 | cleanup |
| 3 · Features & scaling | #253, #206, #205, #213 | roadmap |

All 25 open issues are accounted for above.
