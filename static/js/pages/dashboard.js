// Dashboard component — registered via Alpine.data (strict CSP, #77).

document.addEventListener('alpine:init', () => {
  Alpine.data('dashboard', () => ({
    ...DT.uiHelpers,
    loading: true,
    scenarios: [],
    exercises: [],
    isF: false,
    canManageScenarios: false,
    liveElapsedInterval: null,
    // Bumped every 10s. One interval drives every card's elapsed value — see elapsedFor().
    liveTick: 0,

    // More than one exercise can be active at a time (#96) — render them all.
    get liveExercises() {
      return this.exercises.filter(e => e.state === 'active');
    },

    elapsedFor(ex) {
      // The bare read of liveTick is load-bearing: it registers liveTick as an Alpine
      // dependency of this call, so bumping it re-evaluates every card's elapsed value.
      // fmtElapsed reads Date.now() internally. Removing this line freezes all timers.
      this.liveTick;
      return this.fmtElapsed(ex.started_at);
    },

    async init() {
      const [mr, er] = await Promise.all([
        apiFetch('/auth/me'),
        apiFetch('/exercises'),
      ]);
      if (mr && mr.ok) {
        const me = await readJson(mr);
        this.isF = me?.role === 'facilitator';
        this.canManageScenarios = this.isF;
      }
      if (er && er.ok) this.exercises = await readJson(er, []);
      if (this.canManageScenarios) {
        const sr = await apiFetch('/scenarios');
        if (sr && sr.ok) this.scenarios = await readJson(sr, []);
      }
      this.loading = false;

      this.liveElapsedInterval = setInterval(() => { this.liveTick++; }, 10000);
    },

    destroy() {
      if (this.liveElapsedInterval) clearInterval(this.liveElapsedInterval);
    },
  }));
});
