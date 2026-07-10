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

  // Admin outbound-proxy page (#97). Credentials are env-only and never round-trip
  // through this component.
  Alpine.data('adminProxy', () => ({
    allModes: ['SYSTEM', 'NONE', 'EXPLICIT'],
    cfg: { mode: 'SYSTEM', proxy_url: '', no_proxy: '' },
    targets: [],
    target: '',
    testResult: '',
    saving: false,
    testing: false,
    message: '',

    async init() {
      await Promise.all([this.loadSettings(), this.loadTargets()]);
    },

    async loadTargets() {
      const resp = await apiFetch('/proxy/targets');
      const data = await readJson(resp, null);
      this.targets = data && data.targets ? data.targets : [];
      if (this.targets.length > 0) this.target = this.targets[0];
    },

    get modeHelp() {
      if (this.cfg.mode === 'NONE') return 'Always connect directly, ignoring environment proxy vars.';
      if (this.cfg.mode === 'EXPLICIT') return 'Route through the proxy URL below, except for no-proxy hosts.';
      return 'Honour the HTTP(S)_PROXY / NO_PROXY environment variables.';
    },

    get testFailed() {
      return this.testResult.indexOf('error') === 0;
    },

    async loadSettings() {
      const resp = await apiFetch('/proxy/settings');
      const data = await readJson(resp, null);
      if (data) {
        this.cfg = {
          mode: data.mode || 'SYSTEM',
          proxy_url: data.proxy_url || '',
          no_proxy: data.no_proxy || '',
        };
      }
    },

    async save() {
      this.saving = true;
      this.message = '';
      const resp = await apiFetch('/proxy/settings', {
        method: 'PUT',
        body: JSON.stringify(this.cfg),
      });
      this.saving = false;
      this.message = resp && resp.ok ? 'Routing saved.' : 'Could not save.';
      if (resp && resp.ok) await this.loadSettings();
    },

    async runTest() {
      this.testing = true;
      this.testResult = '';
      const resp = await apiFetch('/proxy/test', {
        method: 'POST',
        body: JSON.stringify({ target: this.target }),
      });
      const data = await readJson(resp, null);
      this.testing = false;
      this.testResult = data && data.result ? data.result : 'error: request failed';
    },
  }));
});
