// After-action report — print-friendly view + optional LLM executive summary (#113).
// CSP-safe Alpine: registered under alpine:init, spreads DT.uiHelpers, all compound
// logic in getters/methods.
document.addEventListener('alpine:init', () => {
  const QUALITY_LABEL = { good: 'Good', adequate: 'Adequate', poor: 'Poor' };

  Alpine.data('afterActionReport', (exerciseId) => ({
    ...DT.uiHelpers,
    loading: true,
    error: '',
    report: null,
    // summary state
    available: false,
    summary: null,
    summaryText: '',
    summaryDrafting: false,
    summarySaving: false,
    summarySaved: false,
    // ws lifecycle (managed by DT.connectExerciseWs)
    ws: null,
    wsConnected: false,
    pingInterval: null,
    reconnectTimeout: null,
    destroyed: false,

    async init() {
      await this.load();
      this.connectWs();
    },

    async load() {
      const [rr, sr] = await Promise.all([
        apiFetch(`/exercises/${exerciseId}/report`),
        apiFetch(`/exercises/${exerciseId}/report/summary`),
      ]);
      if (rr && rr.ok) this.report = await rr.json();
      else this.error = 'Could not load the report.';
      if (sr && sr.ok) {
        const s = await sr.json();
        this.available = s.available;
        this.summary = s.summary;
        this.summaryText = s.summary ? s.summary.summary_text : '';
      }
      this.loading = false;
    },

    connectWs() {
      DT.connectExerciseWs(exerciseId, this, {
        onMessage: (msg) => {
          if (msg.type === 'summary_ready') {
            this.summary = msg.payload;
            this.summaryText = msg.payload.summary_text;
            this.summaryDrafting = false;
          }
        },
      });
    },

    // ── Header getters ──
    get ex() { return this.report ? this.report.exercise : null; },
    get exTitle() { return this.report ? this.report.exercise.title : '…'; },
    get exId() { return this.report ? 'EX-' + this.padId(this.report.exercise.id, 3) : ''; },
    get reviewHref() { return '/exercises/' + exerciseId + '/review'; },
    get markdownHref() { return '/api/exercises/' + exerciseId + '/report.md'; },

    // ── Section presence ──
    get hasInjects() { return !!(this.report && this.report.injects.length); },
    get hasComms() { return !!(this.report && this.report.communications.length); },
    get hasDebrief() {
      if (!this.report) return false;
      const d = this.report.debrief;
      return !!(d.scenario_debrief_notes || d.debrief_notes);
    },
    get teamsLabel() {
      if (!this.report) return '—';
      const teams = this.report.teams.map(t => `${t.label} (${t.participant_count})`);
      if (this.report.unassigned_participant_count) {
        teams.push(`Unassigned / other (${this.report.unassigned_participant_count})`);
      }
      return teams.join(', ') || '—';
    },

    // ── Summary ──
    get hasSummary() { return !!this.summary; }, // a drafted/edited summary exists
    get showSummarySection() { return this.available || this.hasSummary; },
    qualityLabel(q) { return QUALITY_LABEL[q] || q; },
    qualityClass(q) {
      const map = { good: 'bg-st-active', adequate: 'bg-st-paused', poor: 'bg-st-completed' };
      return 'pill mono ' + (map[q] || '');
    },
    teamsFor(teams) {
      if (!teams || !teams.length) return 'All teams';
      return teams.join(', ');
    },

    async draftSummary() {
      this.summaryDrafting = true;
      this.error = '';
      const r = await apiFetch(`/exercises/${exerciseId}/report/summary`, { method: 'POST' });
      if (!r || !r.ok) {
        this.summaryDrafting = false;
        this.error = 'Could not start the AI summary.';
      }
      // success arrives over the WebSocket as summary_ready
    },

    async saveSummary() {
      this.summarySaving = true;
      this.summarySaved = false;
      const r = await apiFetch(`/exercises/${exerciseId}/report/summary`, {
        method: 'PATCH',
        body: JSON.stringify({ summary_text: this.summaryText }),
      });
      this.summarySaving = false;
      if (r && r.ok) {
        this.summary = await r.json();
        this.summarySaved = true;
        setTimeout(() => { this.summarySaved = false; }, 2500);
      } else {
        this.error = 'Could not save the summary.';
      }
    },

    printReport() { window.print(); },

    destroy() {
      this.destroyed = true;
      if (this.ws) try { this.ws.close(); } catch { /* noop */ }
      if (this.pingInterval) clearInterval(this.pingInterval);
      if (this.reconnectTimeout) clearTimeout(this.reconnectTimeout);
    },
  }));
});
