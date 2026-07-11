// After-Action Review — exercise timeline / replay (#111).
// CSP-safe Alpine (strict script-src 'self'): registered under alpine:init, spreads
// DT.uiHelpers, and keeps every `?.`/compound expression in a getter/method since the
// CSP interpreter cannot evaluate them inside directives.
document.addEventListener('alpine:init', () => {
  const STATE_LABELS = {
    'exercise.start': 'Exercise started',
    'exercise.pause': 'Exercise paused',
    'exercise.resume': 'Exercise resumed',
    'exercise.complete': 'Exercise completed',
  };
  const KIND_LABELS = {
    inject_released: 'Inject',
    response: 'Response',
    communication: 'Comms',
    comment: 'Comment',
    state_change: 'Lifecycle',
  };

  Alpine.data('afterActionReview', (exerciseId) => ({
    ...DT.uiHelpers,
    loading: true,
    error: '',
    exercise: null,
    events: [],
    userMap: {},

    async init() {
      await this.load();
    },

    async load() {
      this.loading = true;
      const [er, mr, ur, tr] = await Promise.all([
        apiFetch(`/exercises/${exerciseId}`),
        apiFetch(`/exercises/${exerciseId}/members`),
        apiFetch('/users'),
        apiFetch(`/exercises/${exerciseId}/timeline`),
      ]);
      if (er && er.ok) this.exercise = await er.json();
      // /users is facilitator-visible; build id → display name for attribution.
      if (ur && ur.ok) {
        const users = await ur.json();
        this.userMap = Object.fromEntries(users.map(u => [u.id, u.display_name || u.email]));
      }
      if (mr && mr.ok) { /* members loaded for parity; names come from userMap */ }
      if (tr && tr.ok) this.events = await tr.json();
      else this.error = 'Could not load the timeline.';
      this.loading = false;
    },

    // Shell getters (CSP: no `?.` in directives).
    get exState() { return this.exercise ? this.exercise.state : ''; },
    get exTitle() { return (this.exercise && this.exercise.title) || '…'; },
    get exId() { return this.exercise ? 'EX-' + this.padId(this.exercise.id, 3) : ''; },
    get facilitateHref() { return '/exercises/' + exerciseId + '/facilitate'; },
    get isEmpty() { return !this.loading && this.events.length === 0; },

    userName(id) {
      if (id === null || id === undefined) return 'system';
      return this.userMap[id] || ('User #' + id);
    },
    teamsLabel(teams) {
      if (!teams || teams.length === 0) return 'All teams';
      return teams.join(', ');
    },

    kindLabel(ev) {
      if (ev.kind === 'state_change') return 'Lifecycle';
      return KIND_LABELS[ev.kind] || ev.kind;
    },
    kindDotStyle(ev) {
      const map = {
        inject_released: 'var(--accent)',
        response: 'var(--ink-soft)',
        communication: 'var(--c-warn)',
        comment: 'var(--ink-soft)',
        state_change: 'var(--accent-deep)',
      };
      return 'background:' + (map[ev.kind] || 'var(--ink-soft)');
    },

    eventTitle(ev) {
      switch (ev.kind) {
        case 'inject_released': return 'Inject released — ' + (ev.title || '');
        case 'response': return 'Response from ' + this.userName(ev.user_id);
        case 'communication': return this.commTitle(ev);
        case 'comment': return 'Comment by ' + this.userName(ev.user_id);
        case 'state_change': return STATE_LABELS[ev.action] || ev.action || 'State change';
        default: return ev.kind;
      }
    },
    commTitle(ev) {
      const dir = ev.direction === 'inbound' ? 'Inbound' : 'Outbound';
      return dir + ' comms — ' + (ev.subject || '');
    },
    eventMeta(ev) {
      switch (ev.kind) {
        case 'inject_released':
          return this.teamsLabel(ev.target_teams) + ' · released by ' + this.userName(ev.released_by);
        case 'communication': {
          const who = ev.external_entity ? (' · ' + ev.external_entity) : '';
          return this.teamsLabel(ev.visible_to_teams) + who;
        }
        case 'state_change':
          return 'by ' + this.userName(ev.actor_id);
        case 'response':
          return ev.selected_option ? ('Selected: ' + ev.selected_option) : '';
        default:
          return '';
      }
    },
    // Body text shown for events that carry free text.
    eventBody(ev) {
      if (ev.kind === 'response' || ev.kind === 'comment') return ev.content || '';
      return '';
    },
    hasBody(ev) { return !!this.eventBody(ev); },

    // Decision-quality pill (responses only).
    hasQuality(ev) { return ev.kind === 'response' && !!ev.decision_quality; },
    qualityClass(q) {
      const map = { good: 'bg-st-active', adequate: 'bg-st-paused', poor: 'bg-st-completed' };
      return 'pill mono ' + (map[q] || '');
    },
  }));
});
