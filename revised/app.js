// Deep Thought — Revised Interface, shared Alpine state + seed data.
// Built to mirror the real backend shape (scenarios, exercises, injects, responses,
// communications, suggested injects) so screens look truthful.

function shell() {
  return {
    screen: location.hash.replace('#', '') || 'console',
    setScreen(s) {
      this.screen = s;
      history.replaceState(null, '', '#' + s);
      window.scrollTo({ top: 0 });
    },
    init() {
      window.addEventListener('hashchange', () => {
        this.screen = location.hash.replace('#', '') || 'console';
      });
    },
  };
}

// ── Seed data ────────────────────────────────────────────────────────────
const SCENARIO = {
  id: 4,
  title: 'Ransomware — Q3 Supply Chain',
  description: 'Adversary establishes foothold via a compromised MSP. Targets payroll on a Friday afternoon.',
  tags: ['cyber', 'ransomware', 'msp'],
  start: 'inj_01',
  teams: [
    { id: 'it_ops', label: 'IT Operations' },
    { id: 'legal',  label: 'Legal & Compliance' },
    { id: 'exec',   label: 'Executive' },
    { id: 'comms',  label: 'Comms & PR' },
  ],
  injects: [
    { id: 'inj_01', title: 'Initial SIEM alert',
      summary: 'Lateral movement flagged on AD-DC02 by anomaly detector.',
      teams: ['it_ops'], children: ['inj_02a', 'inj_02b'] },
    { id: 'inj_02a', title: 'Isolate — chosen', branch: 'opt_isolate',
      summary: 'Domain controller isolated. Payroll system loses sync.',
      teams: ['it_ops','exec'], children: ['inj_03'] },
    { id: 'inj_02b', title: 'Monitor — chosen', branch: 'opt_monitor',
      summary: 'Encryption begins on a finance share three hours later.',
      teams: ['it_ops'], children: ['inj_03'] },
    { id: 'inj_03', title: 'Regulator inquiry (ICO)',
      summary: 'ICO requests a 72-hour breach notification under UK GDPR.',
      teams: ['legal','exec'], children: ['inj_04a','inj_04b'] },
    { id: 'inj_04a', title: 'Press leak', branch: 'opt_notify',
      summary: 'Reuters publishes a story citing an internal source.',
      teams: ['comms','exec'], children: [] },
    { id: 'inj_04b', title: 'Quiet remediation', branch: 'opt_delay',
      summary: 'Threat actor escalates ransom demand to £4.2M in 24h.',
      teams: ['it_ops','exec'], children: [] },
  ],
};

const EXERCISE = {
  id: 12,
  title: 'Friday Live — Ransomware Drill',
  state: 'active',
  llm_enabled: true,
  started_at: Date.now() - 1000 * 60 * 47,
  current_node_id: 'inj_03',
  members_online: 9,
  members_total: 11,
};

const INJECTS = [
  { id: 'inj_01', node: 'inj_01', title: 'Initial SIEM alert',
    content: 'At 14:02 UTC the SIEM anomaly detector flagged unusual SMB traffic between AD-DC02 and a finance file server. No EDR alerts yet. Reporter: SOC L1.',
    teams: ['it_ops'], state: 'resolved', released_at: Date.now() - 1000 * 60 * 47 },
  { id: 'inj_02a', node: 'inj_02a', title: 'Isolation completed',
    content: 'AD-DC02 isolated at 14:11 UTC. Payroll cluster has lost AD sync — replication suspended.',
    teams: ['it_ops','exec'], state: 'resolved', released_at: Date.now() - 1000 * 60 * 32 },
  { id: 'inj_03', node: 'inj_03', title: 'ICO breach notification inquiry',
    content: 'The Information Commissioner has opened an inquiry requesting confirmation of a personal-data breach within 72 hours. Outline of categories affected required.',
    teams: ['legal','exec'], state: 'released', released_at: Date.now() - 1000 * 60 * 6 },
  { id: 'inj_pending', node: null, title: 'Internal staff townhall request',
    content: 'CEO office has asked for a five-line statement to staff before 17:00.',
    teams: ['comms','exec'], state: 'pending', released_at: null },
];

const RESPONSES = [
  { id: 31, inject_id: 'inj_03', user: 'M. Okafor', team: 'legal',
    selected_option: 'opt_notify',
    content: 'Recommend submitting holding notification today. Confirm categories: employee PII, partial payroll records. Will work with DPO on detail.',
    submitted_at: Date.now() - 1000 * 60 * 4,
    assessment: { quality: 'good',
      text: 'Strong: anchors to 72h obligation, identifies a DPO touchpoint, scopes data categories. Could go further on the press posture.' } },
  { id: 30, inject_id: 'inj_03', user: 'J. Reyes', team: 'exec',
    selected_option: null,
    content: 'Hold off until we have the forensic timeline. We do not want to over-notify.',
    submitted_at: Date.now() - 1000 * 60 * 3,
    assessment: { quality: 'poor',
      text: 'Misreads the 72h clock — it starts at *awareness*, not at forensic certainty. Recommend coaching on Art. 33 timing.' } },
  { id: 29, inject_id: 'inj_02a', user: 'A. Doyle', team: 'it_ops',
    selected_option: 'opt_isolate',
    content: 'Confirming AD-DC02 isolated; secondary DC promoted; payroll team alerted to expected sync lag.',
    submitted_at: Date.now() - 1000 * 60 * 31,
    assessment: { quality: 'good',
      text: 'Decisive containment, surfaces operational impact proactively.' } },
];

const SUGGESTIONS = [
  { id: 'sg_1', title: 'Board notification window',
    content: 'Board member asks: do we have a defensible reason to delay public disclosure 24h while we coordinate with NCSC?',
    teams: ['exec','legal'], status: 'pending_review' },
];

const PARTICIPANTS = [
  { id: 1, name: 'M. Okafor',  role: 'participant', team: 'legal',   online: true },
  { id: 2, name: 'J. Reyes',   role: 'participant', team: 'exec',    online: true },
  { id: 3, name: 'A. Doyle',   role: 'participant', team: 'it_ops',  online: true },
  { id: 4, name: 'S. Patel',   role: 'participant', team: 'comms',   online: true },
  { id: 5, name: 'R. Holm',    role: 'participant', team: 'it_ops',  online: false },
  { id: 6, name: 'L. Chen',    role: 'observer',    team: null,      online: true },
  { id: 7, name: 'T. Aldis',   role: 'facilitator', team: null,      online: true },
];

const COMMS = [
  { id: 91, direction: 'inbound',  entity: 'ICO', subject: 'Notification under Art. 33',
    body: 'We require confirmation of personal-data categories affected and an outline of containment by 17:00 Friday.',
    sent_at: Date.now() - 1000 * 60 * 6, unread: true,  triggered_by: 'inj_03' },
  { id: 90, direction: 'inbound',  entity: 'NCSC', subject: 'TI advisory: TA577 lateral movement',
    body: 'Observed TTPs match Q3 campaign targeting MSP supply chains. Sharing IOCs in attached bundle.',
    sent_at: Date.now() - 1000 * 60 * 38, unread: false, triggered_by: 'inj_01' },
  { id: 89, direction: 'outbound', entity: 'Internal — Staff', subject: 'Brief operational update',
    body: 'A precaution has been taken on a single internal system. Payroll for this cycle is unaffected. We will update again by 18:00.',
    sent_at: Date.now() - 1000 * 60 * 22, unread: false, triggered_by: null },
  { id: 88, direction: 'inbound',  entity: 'Reuters', subject: 'Request for comment',
    body: 'We are preparing a piece referencing an internal incident at your organisation. Can you confirm or deny by 16:30?',
    sent_at: Date.now() - 1000 * 60 * 12, unread: true,  triggered_by: null },
];

const SCENARIOS = [
  { id: 4, title: 'Ransomware — Q3 Supply Chain', tags: ['cyber','ransomware','msp'],
    injects: 17, branches: 5, updated: Date.now() - 1000 * 60 * 60 * 26, owner: 'T. Aldis' },
  { id: 3, title: 'GDPR Data Loss — Marketing Vendor', tags: ['privacy','vendor'],
    injects: 11, branches: 3, updated: Date.now() - 1000 * 60 * 60 * 24 * 9, owner: 'T. Aldis' },
  { id: 2, title: 'DDoS — Customer Portal', tags: ['cyber','availability'],
    injects: 9, branches: 2, updated: Date.now() - 1000 * 60 * 60 * 24 * 22, owner: 'M. Okafor' },
  { id: 1, title: 'Power Outage — Tier 1 DC', tags: ['continuity'],
    injects: 14, branches: 4, updated: Date.now() - 1000 * 60 * 60 * 24 * 41, owner: 'T. Aldis' },
];

const EXERCISES_LIST = [
  { id: 12, title: 'Friday Live — Ransomware Drill', state: 'active',
    scenario: 'Ransomware — Q3 Supply Chain',
    started: Date.now() - 1000 * 60 * 47, members: 11, online: 9 },
  { id: 11, title: 'Q3 Tabletop — Legal & Exec', state: 'paused',
    scenario: 'GDPR Data Loss — Marketing Vendor',
    started: Date.now() - 1000 * 60 * 60 * 2, members: 6, online: 3 },
  { id: 10, title: 'IT Ops walkthrough', state: 'draft',
    scenario: 'DDoS — Customer Portal',
    started: null, members: 8, online: 0 },
  { id: 9,  title: 'Continuity dry-run — Sep', state: 'completed',
    scenario: 'Power Outage — Tier 1 DC',
    started: Date.now() - 1000 * 60 * 60 * 24 * 14, members: 12, online: 0 },
];

// Format helpers exposed to Alpine
function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m.toString().padStart(2, '0')}m`;
  return `${m}m`;
}
function fmtAgo(ms) {
  if (!ms) return '—';
  const diff = (Date.now() - ms) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
function fmtTime(ms) {
  if (!ms) return '—';
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function teamLabel(id) {
  const t = SCENARIO.teams.find(x => x.id === id);
  return t ? t.label : id;
}
function teamColor(id) {
  // Stable per-team tint from a small palette
  const map = {
    it_ops: 'bg-sky-100/70 text-sky-800 ring-sky-200',
    legal:  'bg-violet-100/70 text-violet-800 ring-violet-200',
    exec:   'bg-amber-100/70 text-amber-900 ring-amber-200',
    comms:  'bg-emerald-100/70 text-emerald-800 ring-emerald-200',
  };
  return map[id] || 'bg-stone-100 text-stone-700 ring-stone-200';
}

// Expose
window.shell = shell;
window.SCENARIO = SCENARIO;
window.EXERCISE = EXERCISE;
window.INJECTS = INJECTS;
window.RESPONSES = RESPONSES;
window.SUGGESTIONS = SUGGESTIONS;
window.PARTICIPANTS = PARTICIPANTS;
window.COMMS = COMMS;
window.SCENARIOS = SCENARIOS;
window.EXERCISES_LIST = EXERCISES_LIST;
window.fmtElapsed = fmtElapsed;
window.fmtAgo = fmtAgo;
window.fmtTime = fmtTime;
window.teamLabel = teamLabel;
window.teamColor = teamColor;
