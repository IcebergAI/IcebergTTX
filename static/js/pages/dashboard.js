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
    liveElapsed: '—',

    get liveExercise() {
      return this.exercises.find(e => e.state === 'active') || null;
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

      if (this.liveExercise) {
        this.liveElapsed = this.fmtElapsed(this.liveExercise.started_at);
        this.liveElapsedInterval = setInterval(() => {
          if (this.liveExercise) this.liveElapsed = this.fmtElapsed(this.liveExercise.started_at);
        }, 10000);
      }
    },

    destroy() {
      if (this.liveElapsedInterval) clearInterval(this.liveElapsedInterval);
    },
  }));
});
