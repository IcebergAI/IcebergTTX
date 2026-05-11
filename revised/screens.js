// Deep Thought — Revised Interface, screen bodies.
// Each screen is rendered as a static HTML string injected on first view,
// then Alpine takes over for any interactive bits.

document.addEventListener('alpine:init', () => {
  document.getElementById('screen-console').innerHTML     = consoleScreen();
  document.getElementById('screen-scenarios').innerHTML   = scenariosScreen();
  document.getElementById('screen-scenario').innerHTML    = scenarioDetailScreen();
  document.getElementById('screen-facilitate').innerHTML  = facilitateScreen();
  document.getElementById('screen-participate').innerHTML = participateScreen();
  document.getElementById('screen-comms').innerHTML       = commsScreen();
});

// ─────────────────────────────────────────────────────────────────
// 1. Command center — replaces the generic "dashboard"
// ─────────────────────────────────────────────────────────────────
function consoleScreen() {
  return `
  <div class="px-8 py-6 max-w-[1240px]">
    <div class="flex items-end justify-between mb-6">
      <div>
        <div class="smallcaps text-stone-500">Workspace</div>
        <h1 class="text-[26px] font-semibold tracking-tight leading-tight mt-1">Command center</h1>
        <p class="text-stone-500 text-[14px] mt-1 max-w-prose">
          One live drill, two scheduled. Pick up where you left off, or open a new exercise.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button class="btn btn-ghost h-8 px-3 rounded-md text-[13px]">Import scenario</button>
        <button class="btn btn-primary h-8 px-3 rounded-md text-[13px]">+ New exercise</button>
      </div>
    </div>

    <!-- Live exercise: hero card -->
    <div class="briefing released rounded-[10px] p-5 mb-7">
      <div class="flex items-start gap-6">
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <span class="pill" style="background: oklch(0.74 0.14 70 / .15); color: var(--accent-d); box-shadow: inset 0 0 0 1px oklch(0.74 0.14 70 / .35);">
              <span class="live-dot"></span>Live
            </span>
            <span class="mono text-[11.5px] text-stone-500">EX-12</span>
            <span class="text-stone-300">·</span>
            <span class="mono text-[11.5px] text-stone-500">node inj_03</span>
          </div>
          <h2 class="text-[20px] font-semibold mt-2 tracking-tight">Friday Live — Ransomware Drill</h2>
          <p class="text-stone-500 text-[13.5px] mt-0.5">Ransomware — Q3 Supply Chain · started 14:02 UTC</p>

          <div class="grid grid-cols-4 gap-6 mt-5">
            <div>
              <div class="smallcaps text-stone-500">Elapsed</div>
              <div class="mono text-[22px] mt-0.5">47m</div>
            </div>
            <div>
              <div class="smallcaps text-stone-500">Online</div>
              <div class="mono text-[22px] mt-0.5">9<span class="text-stone-400 text-[15px]"> / 11</span></div>
            </div>
            <div>
              <div class="smallcaps text-stone-500">Injects released</div>
              <div class="mono text-[22px] mt-0.5">3<span class="text-stone-400 text-[15px]"> / 17</span></div>
            </div>
            <div>
              <div class="smallcaps text-stone-500">Pending review</div>
              <div class="mono text-[22px] mt-0.5">1</div>
            </div>
          </div>
        </div>

        <div class="w-px self-stretch rule border-l"></div>

        <div class="w-[260px] shrink-0">
          <div class="smallcaps text-stone-500 mb-2">Last 5 minutes</div>
          <ul class="text-[13px] text-stone-700 space-y-1.5">
            <li class="flex gap-2"><span class="mono text-stone-400 w-12 shrink-0">14:45</span>M. Okafor responded · <span class="mono text-stone-500">opt_notify</span></li>
            <li class="flex gap-2"><span class="mono text-stone-400 w-12 shrink-0">14:46</span>J. Reyes responded</li>
            <li class="flex gap-2"><span class="mono text-stone-400 w-12 shrink-0">14:46</span>AI flagged response #30 · <span style="color:#9a3412">poor</span></li>
            <li class="flex gap-2"><span class="mono text-stone-400 w-12 shrink-0">14:47</span>Reuters inbound — request for comment</li>
            <li class="flex gap-2"><span class="mono text-stone-400 w-12 shrink-0">14:48</span>AI suggested inject <span class="mono">sg_1</span></li>
          </ul>
          <a href="#facilitate" class="inline-flex items-center gap-1 mt-3 text-[13px] font-medium" style="color: var(--accent-d);">
            Open console
            <span class="mono">→</span>
          </a>
        </div>
      </div>
    </div>

    <!-- Two columns: queued exercises + scenario library -->
    <div class="grid grid-cols-5 gap-7">
      <div class="col-span-3">
        <div class="section-title mb-3">
          <h3 class="smallcaps text-stone-500">Exercises</h3>
          <a href="#facilitate" class="smallcaps text-stone-400 hover:text-stone-700">View all</a>
        </div>
        <div class="paper ring-rule rounded-lg divide-y rule">
          ${EXERCISES_LIST.map(ex => `
            <div class="px-4 py-3 flex items-center gap-4">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="text-[14px] font-medium truncate">${ex.title}</span>
                  ${stateBadge(ex.state)}
                </div>
                <div class="mono text-[11.5px] text-stone-500 mt-0.5">
                  EX-${String(ex.id).padStart(3,'0')} · ${ex.scenario}
                </div>
              </div>
              <div class="text-right shrink-0">
                <div class="mono text-[12px] text-stone-700">${ex.started ? fmtElapsed(ex.started) : '—'}</div>
                <div class="mono text-[11px] text-stone-400">${ex.online}/${ex.members} online</div>
              </div>
              <a href="#facilitate" class="btn btn-ghost h-7 px-2.5 rounded-md text-[12px]">Open</a>
            </div>
          `).join('')}
        </div>
      </div>

      <div class="col-span-2">
        <div class="section-title mb-3">
          <h3 class="smallcaps text-stone-500">Scenario library</h3>
          <a href="#scenarios" class="smallcaps text-stone-400 hover:text-stone-700">View all</a>
        </div>
        <div class="paper ring-rule rounded-lg divide-y rule">
          ${SCENARIOS.slice(0,4).map(s => `
            <a href="#scenario" class="block px-4 py-3 hover:bg-stone-50/80">
              <div class="flex items-center gap-2">
                <span class="text-[13.5px] font-medium truncate">${s.title}</span>
              </div>
              <div class="flex items-center gap-2 mt-1">
                <span class="mono text-[11px] text-stone-500">${s.injects} injects · ${s.branches} branches</span>
                <span class="mono text-[11px] text-stone-400">· ${fmtAgo(s.updated)}</span>
              </div>
            </a>
          `).join('')}
        </div>
      </div>
    </div>
  </div>`;
}

function stateBadge(state) {
  const map = {
    active:    `<span class="pill bg-st-active"><span class="live-dot"></span>active</span>`,
    paused:    `<span class="pill bg-st-paused"><span class="dot" style="background:#c08a26"></span>paused</span>`,
    draft:     `<span class="pill bg-st-draft"><span class="dot" style="background:#9a958a"></span>draft</span>`,
    completed: `<span class="pill bg-st-completed"><span class="dot" style="background:#5b86b8"></span>completed</span>`,
  };
  return map[state] || '';
}

// ─────────────────────────────────────────────────────────────────
// 2. Scenarios library
// ─────────────────────────────────────────────────────────────────
function scenariosScreen() {
  return `
  <div class="px-8 py-6 max-w-[1240px]">
    <div class="flex items-end justify-between mb-6">
      <div>
        <div class="smallcaps text-stone-500">Library</div>
        <h1 class="text-[26px] font-semibold tracking-tight leading-tight mt-1">Scenarios</h1>
      </div>
      <div class="flex items-center gap-2">
        <label class="btn btn-ghost h-8 px-3 rounded-md text-[13px] cursor-pointer">
          Import JSON
        </label>
        <button class="btn btn-primary h-8 px-3 rounded-md text-[13px]">+ New scenario</button>
      </div>
    </div>

    <div class="paper ring-rule rounded-lg overflow-hidden">
      <div class="grid grid-cols-[1fr_140px_120px_130px_90px] gap-4 px-5 py-2.5 smallcaps text-stone-500 border-b rule">
        <div>Scenario</div>
        <div>Tags</div>
        <div class="text-right">Injects / branches</div>
        <div class="text-right">Updated</div>
        <div class="text-right">Owner</div>
      </div>
      ${SCENARIOS.map(s => `
        <a href="#scenario" class="grid grid-cols-[1fr_140px_120px_130px_90px] gap-4 px-5 py-3.5 items-center border-b rule hover:bg-stone-50/70 last:border-b-0">
          <div class="min-w-0">
            <div class="font-medium text-[14.5px] truncate">${s.title}</div>
            <div class="mono text-[11.5px] text-stone-400 mt-0.5">SC-${String(s.id).padStart(3,'0')}</div>
          </div>
          <div class="flex flex-wrap gap-1">
            ${s.tags.map(t => `<span class="pill mono">${t}</span>`).join('')}
          </div>
          <div class="text-right mono text-[12.5px] text-stone-700">
            ${s.injects}<span class="text-stone-400"> / </span>${s.branches}
          </div>
          <div class="text-right mono text-[12px] text-stone-500">${fmtAgo(s.updated)}</div>
          <div class="text-right text-[12.5px] text-stone-600">${s.owner}</div>
        </a>
      `).join('')}
    </div>
  </div>`;
}

// ─────────────────────────────────────────────────────────────────
// 3. Scenario detail — inject tree (real tree, not flat list)
// ─────────────────────────────────────────────────────────────────
function scenarioDetailScreen() {
  // Walk the tree depth-first from start
  const byId = Object.fromEntries(SCENARIO.injects.map(i => [i.id, i]));
  const rows = [];
  (function walk(id, depth, isLast, prefix) {
    const node = byId[id]; if (!node) return;
    rows.push({ node, depth, isLast, prefix });
    node.children.forEach((cid, idx) => {
      const last = idx === node.children.length - 1;
      walk(cid, depth + 1, last, [...prefix, isLast]);
    });
  })(SCENARIO.start, 0, true, []);

  return `
  <div class="px-8 py-6 max-w-[1240px]">
    <a href="#scenarios" class="smallcaps text-stone-500 hover:text-stone-800 inline-flex items-center gap-1.5 mb-4">
      <span class="mono">←</span> Scenarios
    </a>

    <div class="flex items-start justify-between gap-6 mb-6">
      <div class="min-w-0">
        <div class="mono text-[11.5px] text-stone-500">SC-${String(SCENARIO.id).padStart(3,'0')}</div>
        <h1 class="text-[26px] font-semibold tracking-tight leading-tight mt-1">${SCENARIO.title}</h1>
        <p class="text-stone-600 text-[14px] mt-2 max-w-prose" style="text-wrap: pretty;">${SCENARIO.description}</p>
        <div class="flex flex-wrap gap-1.5 mt-3">
          ${SCENARIO.tags.map(t => `<span class="pill mono">${t}</span>`).join('')}
        </div>
      </div>
      <div class="flex flex-col items-end gap-2 shrink-0">
        <div class="flex gap-2">
          <button class="btn btn-ghost h-8 px-3 rounded-md text-[13px]">Export</button>
          <button class="btn btn-ghost h-8 px-3 rounded-md text-[13px]">Edit</button>
          <button class="btn btn-primary h-8 px-3 rounded-md text-[13px]">Launch exercise</button>
        </div>
        <div class="mono text-[11.5px] text-stone-500">${SCENARIO.injects.length} injects · 5 branches</div>
      </div>
    </div>

    <div class="grid grid-cols-[1fr_280px] gap-7">
      <!-- Tree -->
      <div>
        <div class="section-title mb-3">
          <h3 class="smallcaps text-stone-500">Inject tree</h3>
          <span class="smallcaps text-stone-400">${rows.length} nodes</span>
        </div>
        <div class="paper ring-rule rounded-lg p-4">
          ${rows.map((r, i) => renderTreeRow(r, i, rows)).join('')}
        </div>
      </div>

      <!-- Teams & meta -->
      <aside class="space-y-4">
        <div class="paper ring-rule rounded-lg p-4">
          <div class="smallcaps text-stone-500 mb-3">Participant teams</div>
          <div class="space-y-2">
            ${SCENARIO.teams.map(t => `
              <div class="flex items-center gap-2 text-[13px]">
                <span class="pill mono ${teamColor(t.id)} ring-1 ring-inset">${t.id}</span>
                <span class="text-stone-700">${t.label}</span>
              </div>
            `).join('')}
          </div>
        </div>

        <div class="paper ring-rule rounded-lg p-4">
          <div class="smallcaps text-stone-500 mb-3">Validation</div>
          <ul class="text-[13px] space-y-1.5">
            <li class="flex items-center gap-2"><span class="dot" style="background:#3b8e5a"></span>All branches reach a terminal</li>
            <li class="flex items-center gap-2"><span class="dot" style="background:#3b8e5a"></span>No cycles detected</li>
            <li class="flex items-center gap-2"><span class="dot" style="background:#3b8e5a"></span>Teams referenced are defined</li>
            <li class="flex items-center gap-2"><span class="dot" style="background:#c08a26"></span>2 injects have no team target <span class="text-stone-400">(broadcast)</span></li>
          </ul>
        </div>
      </aside>
    </div>
  </div>`;
}

function renderTreeRow(r, idx, rows) {
  const { node, depth, prefix } = r;
  // Indent and connector
  const indent = depth * 22;
  return `
    <div class="flex items-stretch gap-0 ${idx === 0 ? '' : 'mt-1.5'}">
      <div style="width:${indent}px" class="shrink-0 relative">
        ${depth > 0 ? `<div class="absolute top-0 bottom-0 left-[${(depth - 1) * 22 + 10}px] w-px" style="background: var(--rule-2)"></div>` : ''}
      </div>
      <div class="flex-1 min-w-0">
        <div class="node ${node.id === EXERCISE.current_node_id ? 'current' : ''} flex items-start gap-3">
          <div class="mono text-[11px] text-stone-500 shrink-0 w-[60px] pt-0.5">${node.id}</div>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <span class="text-[13.5px] font-medium">${node.title}</span>
              ${node.id === SCENARIO.start ? '<span class="pill mono" style="color:#3b8e5a; background:#e6f1ea; box-shadow: inset 0 0 0 1px #c5e0cf">start</span>' : ''}
              ${node.branch ? `<span class="pill mono">via ${node.branch}</span>` : ''}
            </div>
            <p class="text-[13px] text-stone-600 mt-0.5" style="text-wrap: pretty;">${node.summary}</p>
            <div class="flex flex-wrap gap-1 mt-1.5">
              ${node.teams.map(t => `<span class="pill mono ${teamColor(t)} ring-1 ring-inset">${t}</span>`).join('')}
            </div>
          </div>
          <div class="shrink-0 mono text-[11px] text-stone-400 pt-0.5">
            ${node.children.length === 0 ? 'end' : `→ ${node.children.length}`}
          </div>
        </div>
      </div>
    </div>`;
}

// ─────────────────────────────────────────────────────────────────
// 4. Facilitator console — the big one
// ─────────────────────────────────────────────────────────────────
function facilitateScreen() {
  const released = INJECTS.filter(i => i.state !== 'pending').length;
  return `
  <div class="flex flex-col" style="height: calc(100vh - 48px);">

    <!-- Ticker / status bar -->
    <div class="px-6 h-12 flex items-center gap-5 border-b rule" style="background: var(--paper);">
      <div class="flex items-center gap-2">
        <span class="live-dot"></span>
        <span class="smallcaps" style="color: var(--accent-d);">Live · 47:12</span>
      </div>
      <div class="w-px h-5 rule-2 border-l"></div>
      <div class="flex items-center gap-2">
        <span class="smallcaps text-stone-500">Exercise</span>
        <span class="text-[13.5px] font-medium">Friday Live — Ransomware Drill</span>
        <span class="mono text-[11.5px] text-stone-400">EX-012</span>
      </div>
      <div class="w-px h-5 rule-2 border-l"></div>
      <div class="flex items-center gap-2">
        <span class="smallcaps text-stone-500">Current node</span>
        <span class="mono text-[12.5px] font-medium">inj_03</span>
      </div>
      <div class="w-px h-5 rule-2 border-l"></div>
      <div class="flex items-center gap-2">
        <span class="smallcaps text-stone-500">Online</span>
        <span class="mono text-[12.5px]">9 / 11</span>
      </div>

      <div class="ml-auto flex items-center gap-2">
        <button class="btn btn-ghost h-7 px-3 rounded-md text-[12.5px]">Pause</button>
        <button class="btn btn-ghost h-7 px-3 rounded-md text-[12.5px]">Communications</button>
        <button class="btn btn-accent h-7 px-3 rounded-md text-[12.5px]">Complete</button>
      </div>
    </div>

    <!-- 3-pane: tree | response feed | participants/AI -->
    <div class="grid grid-cols-[320px_1fr_320px] flex-1 min-h-0">

      <!-- LEFT: scenario tree + release queue -->
      <div class="border-r rule overflow-y-auto" style="background: var(--paper);">
        <div class="px-4 pt-4 pb-2 flex items-center justify-between">
          <div class="smallcaps text-stone-500">Inject tree</div>
          <span class="mono text-[11px] text-stone-400">${released}/${INJECTS.length} live</span>
        </div>
        <div class="px-3 pb-3">
          ${INJECTS.map(i => `
            <div class="node ${i.state==='released' ? 'current' : ''} ${i.state==='resolved' ? 'done' : ''} mb-2">
              <div class="flex items-start gap-2">
                <span class="mono text-[10.5px] text-stone-500 w-[52px] shrink-0 pt-0.5">${i.node || '—'}</span>
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-1.5">
                    <span class="text-[13px] font-medium truncate">${i.title}</span>
                    ${i.state === 'released' ? '<span class="live-dot"></span>' : ''}
                  </div>
                  <div class="flex flex-wrap gap-1 mt-1">
                    ${i.teams.map(t => `<span class="pill mono ${teamColor(t)} ring-1 ring-inset">${t}</span>`).join('')}
                  </div>
                </div>
              </div>
              ${i.state === 'pending' ? `
                <button class="mt-2 btn btn-primary h-6 px-2.5 rounded-md text-[11.5px] w-full justify-center">
                  Release
                </button>` : ''}
              ${i.state === 'released' ? `
                <div class="mt-2 flex items-center justify-between mono text-[10.5px] text-stone-500">
                  <span>released ${fmtTime(i.released_at)}</span>
                  <a class="hover:text-stone-800" href="#participate">view →</a>
                </div>` : ''}
              ${i.state === 'resolved' ? `
                <div class="mt-2 mono text-[10.5px] text-stone-400">resolved · branch chosen</div>` : ''}
            </div>
          `).join('')}

          <!-- Ad-hoc inject -->
          <div class="mt-3 p-3 rounded-lg stripe border border-dashed rule-2">
            <div class="smallcaps text-stone-500 mb-2">+ Add ad-hoc inject</div>
            <input class="w-full bg-white rounded-md ring-rule px-2 py-1 text-[12.5px] mb-1.5" placeholder="Title…" />
            <textarea rows="2" class="w-full bg-white rounded-md ring-rule px-2 py-1 text-[12.5px] resize-none" placeholder="Content…"></textarea>
            <div class="flex items-center gap-2 mt-2">
              <input class="flex-1 bg-white rounded-md ring-rule px-2 py-1 text-[12px]" placeholder="teams (comma)" />
              <button class="btn btn-primary h-6 px-3 rounded-md text-[11.5px]">Add</button>
            </div>
          </div>
        </div>
      </div>

      <!-- MIDDLE: response feed -->
      <div class="overflow-y-auto">
        <div class="px-6 pt-5 pb-3 flex items-baseline gap-3">
          <h2 class="text-[18px] font-semibold tracking-tight">Responses</h2>
          <span class="mono text-[12px] text-stone-500">${RESPONSES.length} total · 2 on current node</span>
          <div class="ml-auto flex items-center gap-2">
            <button class="smallcaps text-stone-500 hover:text-stone-800">All</button>
            <span class="text-stone-300">·</span>
            <button class="smallcaps text-stone-800">Current node</button>
            <span class="text-stone-300">·</span>
            <button class="smallcaps text-stone-500 hover:text-stone-800">Flagged</button>
          </div>
        </div>

        <div class="px-6 pb-6 space-y-3">
          ${RESPONSES.map(r => responseCard(r)).join('')}
        </div>
      </div>

      <!-- RIGHT: participants + AI -->
      <div class="border-l rule overflow-y-auto" style="background: var(--paper);">

        <!-- AI suggestion card -->
        <div class="m-4 rounded-lg p-3" style="background: oklch(0.74 0.14 70 / .08); box-shadow: inset 0 0 0 1px oklch(0.74 0.14 70 / .35);">
          <div class="flex items-center gap-2 mb-1.5">
            <span class="smallcaps" style="color: var(--accent-d);">AI suggestion</span>
            <span class="ml-auto mono text-[10.5px] text-stone-500">claude-sonnet-4-6</span>
          </div>
          <div class="text-[13.5px] font-medium">${SUGGESTIONS[0].title}</div>
          <p class="text-[12.5px] text-stone-600 mt-1" style="text-wrap: pretty;">${SUGGESTIONS[0].content}</p>
          <div class="flex flex-wrap gap-1 mt-2">
            ${SUGGESTIONS[0].teams.map(t => `<span class="pill mono ${teamColor(t)} ring-1 ring-inset">${t}</span>`).join('')}
          </div>
          <div class="flex gap-2 mt-3">
            <button class="btn btn-accent h-6 px-2.5 rounded-md text-[11.5px]">Approve & queue</button>
            <button class="btn btn-ghost h-6 px-2.5 rounded-md text-[11.5px]">Edit</button>
            <button class="text-[11.5px] text-stone-500 hover:text-stone-800 ml-auto">Dismiss</button>
          </div>
        </div>

        <!-- Participants -->
        <div class="px-4 pt-1 pb-3">
          <div class="flex items-center justify-between mb-2">
            <div class="smallcaps text-stone-500">Participants</div>
            <span class="mono text-[11px] text-stone-400">9/11 online</span>
          </div>
          <ul class="space-y-1.5">
            ${PARTICIPANTS.map(p => `
              <li class="flex items-center gap-2 text-[13px]">
                <div class="relative">
                  <div class="h-6 w-6 rounded-full grid place-items-center text-[10.5px] font-semibold"
                       style="background:#e8e4d9; color:#4a4842;">${p.name.split(' ').map(x=>x[0]).join('')}</div>
                  <span class="absolute -bottom-0.5 -right-0.5 dot ring-2 ring-[color:var(--paper)]"
                        style="background: ${p.online ? '#3b8e5a' : '#bcb6a7'}"></span>
                </div>
                <span class="truncate flex-1">${p.name}</span>
                ${p.team ? `<span class="pill mono ${teamColor(p.team)} ring-1 ring-inset">${p.team}</span>` : `<span class="pill mono">${p.role}</span>`}
              </li>
            `).join('')}
          </ul>
          <input class="mt-3 w-full bg-white rounded-md ring-rule px-2 py-1 text-[12px]" placeholder="Add by name or email…" />
        </div>

        <!-- Export -->
        <div class="px-4 pb-5 border-t rule pt-4">
          <div class="smallcaps text-stone-500 mb-2">Export</div>
          <div class="flex flex-col gap-1.5 text-[13px]">
            <a class="text-stone-700 hover:text-stone-900 inline-flex items-center gap-2">
              <span class="mono text-stone-400">↓</span>Transcript (JSON)
            </a>
            <a class="text-stone-700 hover:text-stone-900 inline-flex items-center gap-2">
              <span class="mono text-stone-400">↓</span>Responses (CSV)
            </a>
            <a class="text-stone-700 hover:text-stone-900 inline-flex items-center gap-2">
              <span class="mono text-stone-400">↓</span>AI assessments (JSON)
            </a>
          </div>
        </div>
      </div>
    </div>
  </div>`;
}

function responseCard(r) {
  const qual = r.assessment?.quality;
  const qualColor = qual === 'good' ? '#3b8e5a' : qual === 'adequate' ? '#c08a26' : '#9a3412';
  return `
    <div class="paper ring-rule rounded-lg overflow-hidden">
      <div class="px-4 py-3 flex items-center gap-3 border-b rule">
        <div class="h-7 w-7 rounded-full grid place-items-center text-[11px] font-semibold"
             style="background:#e8e4d9; color:#4a4842;">${r.user.split(' ').map(x=>x[0]).join('')}</div>
        <div class="min-w-0">
          <div class="text-[13.5px] font-medium leading-tight">${r.user}</div>
          <div class="mono text-[11px] text-stone-500 leading-tight">
            #${r.id} · ${r.inject_id} · ${fmtAgo(r.submitted_at)}
          </div>
        </div>
        <span class="pill mono ${teamColor(r.team)} ring-1 ring-inset ml-1">${r.team}</span>
        ${r.selected_option ? `<span class="pill mono">${r.selected_option}</span>` : ''}
      </div>
      <div class="grid grid-cols-[1fr_240px]">
        <div class="px-4 py-3 text-[13.5px] text-stone-800" style="text-wrap: pretty;">
          ${r.content}
        </div>
        <div class="px-4 py-3 border-l rule" style="background: #fbf9f2;">
          <div class="flex items-center gap-2 mb-1.5">
            <span class="smallcaps text-stone-500">AI assessment</span>
            ${qual ? `<span class="pill mono" style="color:${qualColor}; box-shadow: inset 0 0 0 1px ${qualColor}33; background:${qualColor}10">${qual}</span>` : ''}
          </div>
          <p class="text-[12.5px] text-stone-700" style="text-wrap: pretty;">${r.assessment?.text || '—'}</p>
        </div>
      </div>
    </div>`;
}

// ─────────────────────────────────────────────────────────────────
// 5. Participant — briefing card view
// ─────────────────────────────────────────────────────────────────
function participateScreen() {
  return `
  <div class="max-w-[760px] mx-auto px-8 py-8">

    <div class="flex items-center gap-3 mb-1">
      <span class="pill bg-st-active"><span class="live-dot"></span>Live</span>
      <span class="mono text-[11.5px] text-stone-500">EX-012 · you are on team</span>
      <span class="pill mono ${teamColor('legal')} ring-1 ring-inset">legal</span>
    </div>
    <h1 class="text-[24px] font-semibold tracking-tight">Friday Live — Ransomware Drill</h1>
    <p class="text-stone-500 text-[14px] mt-1">Updates appear here as the facilitator releases them. Take your time.</p>

    <div class="mt-7 space-y-5">

      <!-- Released (active) inject -->
      <article class="briefing released p-5">
        <div class="flex items-center gap-2">
          <span class="smallcaps" style="color: var(--accent-d);">New brief</span>
          <span class="mono text-[11px] text-stone-400">inj_03 · 14:48</span>
        </div>
        <h2 class="text-[18px] font-semibold mt-1.5 tracking-tight">ICO breach notification inquiry</h2>
        <p class="text-stone-800 text-[14.5px] mt-2 leading-relaxed" style="text-wrap: pretty;">
          The Information Commissioner has opened an inquiry requesting confirmation of a
          personal-data breach within 72 hours. An outline of categories affected is required.
        </p>

        <div class="mt-5 rounded-md p-4 ring-rule" style="background: #fbf9f2;">
          <div class="smallcaps text-stone-500 mb-2">Choose a stance</div>
          <div class="space-y-1.5">
            <label class="flex items-center gap-2.5 text-[13.5px] cursor-pointer">
              <input type="radio" name="opt" class="accent-stone-700" checked />
              Submit holding notification today (recommended scope)
            </label>
            <label class="flex items-center gap-2.5 text-[13.5px] cursor-pointer">
              <input type="radio" name="opt" class="accent-stone-700" />
              Request 24h extension citing forensic timeline
            </label>
            <label class="flex items-center gap-2.5 text-[13.5px] cursor-pointer">
              <input type="radio" name="opt" class="accent-stone-700" />
              Decline to notify pending board sign-off
            </label>
          </div>

          <div class="smallcaps text-stone-500 mt-4 mb-1.5">Your reasoning</div>
          <textarea rows="4" class="w-full bg-white rounded-md ring-rule px-3 py-2 text-[13.5px] resize-none"
            placeholder="Brief the facilitator on how you intend to handle this…"></textarea>

          <div class="flex items-center gap-3 mt-3">
            <button class="btn btn-primary h-8 px-3.5 rounded-md text-[13px]">Submit response</button>
            <span class="mono text-[11.5px] text-stone-400">Draft autosaved 12s ago</span>
          </div>
        </div>
      </article>

      <!-- Resolved earlier inject -->
      <article class="briefing p-5">
        <div class="flex items-center gap-2">
          <span class="smallcaps text-stone-500">Earlier · resolved</span>
          <span class="mono text-[11px] text-stone-400">inj_02a · 14:16</span>
        </div>
        <h2 class="text-[16.5px] font-semibold mt-1 tracking-tight">Isolation completed</h2>
        <p class="text-stone-700 text-[13.5px] mt-1.5" style="text-wrap: pretty;">
          AD-DC02 isolated at 14:11 UTC. Payroll cluster has lost AD sync — replication suspended.
        </p>
        <div class="mt-3 flex items-center gap-2 text-[12.5px] text-stone-500">
          <span class="dot" style="background:#3b8e5a"></span>
          You submitted a response · 31m ago
        </div>
      </article>

      <!-- Waiting state -->
      <div class="rounded-md py-6 text-center smallcaps text-stone-400 ring-rule">
        Waiting for the facilitator…
      </div>
    </div>
  </div>`;
}

// ─────────────────────────────────────────────────────────────────
// 6. Communications inbox
// ─────────────────────────────────────────────────────────────────
function commsScreen() {
  return `
  <div class="px-8 py-6 max-w-[1240px]">
    <div class="flex items-end justify-between mb-5">
      <div>
        <div class="smallcaps text-stone-500">EX-012 · Friday Live</div>
        <h1 class="text-[26px] font-semibold tracking-tight leading-tight mt-1">Communications</h1>
      </div>
      <div class="flex items-center gap-2">
        <span class="pill"><span class="live-dot"></span>Live</span>
        <button class="btn btn-ghost h-8 px-3 rounded-md text-[13px]">Inject inbound</button>
        <button class="btn btn-primary h-8 px-3 rounded-md text-[13px]">Compose outbound</button>
      </div>
    </div>

    <div class="grid grid-cols-[340px_1fr] gap-0 paper ring-rule rounded-lg overflow-hidden" style="height: 620px;">
      <!-- List -->
      <div class="border-r rule overflow-y-auto">
        <div class="px-4 pt-3 pb-2 flex items-center gap-2 border-b rule">
          <input class="flex-1 bg-transparent text-[13px] outline-none placeholder:text-stone-400" placeholder="Filter messages…" />
          <span class="smallcaps text-stone-400">${COMMS.length}</span>
        </div>
        ${COMMS.map((c, i) => `
          <div class="px-4 py-3 border-b rule cursor-pointer hover:bg-stone-50 ${i === 0 ? 'bg-amber-50/40' : ''}">
            <div class="flex items-center gap-2 mb-1">
              ${c.direction === 'inbound'
                ? '<span class="dot" style="background: var(--accent-d)"></span>'
                : '<span class="dot" style="background:#5b86b8"></span>'}
              <span class="smallcaps ${c.direction === 'inbound' ? 'text-stone-700' : 'text-stone-500'}">${c.direction === 'inbound' ? 'In · ' + c.entity : 'Out · ' + c.entity}</span>
              <span class="mono text-[10.5px] text-stone-400 ml-auto">${fmtAgo(c.sent_at)}</span>
            </div>
            <div class="text-[13.5px] font-medium truncate ${c.unread ? '' : 'text-stone-600'}">${c.subject}</div>
            <div class="text-[12px] text-stone-500 truncate mt-0.5" style="text-wrap:pretty;">${c.body}</div>
            ${c.triggered_by ? `<div class="mono text-[10.5px] text-stone-400 mt-1">triggered by ${c.triggered_by}</div>` : ''}
          </div>
        `).join('')}
      </div>

      <!-- Reader -->
      <div class="overflow-y-auto p-8">
        <div class="flex items-center gap-2 mb-4">
          <span class="pill" style="background: oklch(0.74 0.14 70 / .15); color: var(--accent-d); box-shadow: inset 0 0 0 1px oklch(0.74 0.14 70 / .35);">
            <span class="dot" style="background: var(--accent-d)"></span>Inbound · ICO
          </span>
          <span class="mono text-[11.5px] text-stone-500">MSG-091 · triggered by inj_03</span>
          <span class="mono text-[11.5px] text-stone-400 ml-auto">${fmtTime(COMMS[0].sent_at)}</span>
        </div>

        <div class="smallcaps text-stone-500 mb-1">Subject</div>
        <h2 class="text-[20px] font-semibold tracking-tight mb-5">${COMMS[0].subject}</h2>

        <div class="grid grid-cols-[80px_1fr] gap-x-4 gap-y-2 text-[13px] mb-6">
          <div class="smallcaps text-stone-500 pt-0.5">From</div>
          <div>Information Commissioner's Office <span class="text-stone-400">&lt;casework@ico.org.uk&gt;</span></div>
          <div class="smallcaps text-stone-500 pt-0.5">To</div>
          <div>
            <span class="pill mono ${teamColor('legal')} ring-1 ring-inset">legal</span>
            <span class="pill mono ${teamColor('exec')} ring-1 ring-inset">exec</span>
            <span class="text-stone-400 ml-1 text-[12px]">visibility</span>
          </div>
          <div class="smallcaps text-stone-500 pt-0.5">Due</div>
          <div class="mono">Fri 17:00 UTC · in 2h 11m</div>
        </div>

        <article class="prose-stone max-w-none text-[14.5px] leading-relaxed text-stone-800" style="text-wrap: pretty;">
          <p>${COMMS[0].body}</p>
          <p class="text-stone-600 text-[13.5px]">— Casework Team, ICO</p>
        </article>

        <div class="mt-7 border-t rule pt-5">
          <div class="smallcaps text-stone-500 mb-2">Quick reply</div>
          <textarea rows="4" class="w-full bg-white rounded-md ring-rule px-3 py-2 text-[13.5px] resize-none"
            placeholder="Draft a response to ICO…"></textarea>
          <div class="flex items-center gap-2 mt-2">
            <button class="btn btn-primary h-8 px-3 rounded-md text-[13px]">Send</button>
            <button class="btn btn-ghost h-8 px-3 rounded-md text-[13px]">Save as template</button>
            <span class="ml-auto smallcaps text-stone-400">Recipient: ICO casework</span>
          </div>
        </div>
      </div>
    </div>
  </div>`;
}
