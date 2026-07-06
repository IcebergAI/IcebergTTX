// Admin audit page component — registered via Alpine.data (strict CSP, #77).

document.addEventListener('alpine:init', () => {
  Alpine.data('adminAudit', () => ({
    allMethods: ['stdout', 'file', 'syslog', 'http'],
    cfg: {
      enabled: false, methods: [], min_severity: 'info', file_path: '',
      syslog_host: 'localhost', syslog_port: 514, syslog_protocol: 'UDP',
      http_endpoint: '', http_verify_tls: true,
    },
    filters: { action: '', severity: '', result: '', actor: '' },
    events: [],
    loadingEvents: true,
    saving: false,
    testing: false,
    message: '',

    async init() {
      await Promise.all([this.loadSettings(), this.loadEvents()]);
    },

    async loadSettings() {
      const resp = await apiFetch('/audit/settings');
      const data = await readJson(resp, null);
      if (data) {
        this.cfg = {
          enabled: !!data.enabled,
          methods: data.methods || [],
          min_severity: data.min_severity || 'info',
          file_path: data.file_path || '',
          syslog_host: data.syslog_host || 'localhost',
          syslog_port: data.syslog_port || 514,
          syslog_protocol: data.syslog_protocol || 'UDP',
          http_endpoint: data.http_endpoint || '',
          http_verify_tls: data.http_verify_tls !== false,
        };
      }
    },

    async save() {
      this.saving = true;
      this.message = '';
      const resp = await apiFetch('/audit/settings', {
        method: 'PUT',
        body: JSON.stringify(this.cfg),
      });
      this.saving = false;
      this.message = resp && resp.ok ? 'Forwarding saved.' : 'Could not save.';
      if (resp && resp.ok) await this.loadSettings();
    },

    async sendTest() {
      this.testing = true;
      this.message = '';
      const resp = await apiFetch('/audit/test', { method: 'POST' });
      this.testing = false;
      this.message = resp && resp.ok
        ? 'Test event emitted through the enabled sinks.'
        : 'Could not send test event.';
      if (resp && resp.ok) await this.loadEvents();
    },

    async loadEvents() {
      this.loadingEvents = true;
      const params = new URLSearchParams();
      for (const [k, v] of Object.entries(this.filters)) {
        if (v) params.set(k, v);
      }
      const qs = params.toString();
      const resp = await apiFetch('/audit/events' + (qs ? ('?' + qs) : ''));
      this.events = await readJson(resp, []);
      this.loadingEvents = false;
    },

    fmtTime(ts) {
      if (!ts) return '';
      try { return new Date(ts).toLocaleString(); } catch { return ts; }
    },
    sevDot(sev) {
      if (sev === 'critical') return 'dot-crit';
      if (sev === 'warning') return 'dot-warn';
      return 'dot-info';
    },
    resultClass(result) {
      if (result === 'deny') return 'pill-deny';
      if (result === 'fail') return 'pill-fail';
      return '';
    },
  }));
});
