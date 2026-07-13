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

  Alpine.data('adminEmail', () => ({
    cfg: {
      enabled: false, smtp_host: '', smtp_port: 587, smtp_from: '', smtp_username: '',
      smtp_starttls: true, smtp_tls: false, public_base_url: '',
    },
    saving: false,
    testing: false,
    message: '',
    testResult: '',

    async init() { await this.loadSettings(); },

    chooseStarttls() { if (this.cfg.smtp_starttls) this.cfg.smtp_tls = false; },
    chooseTls() { if (this.cfg.smtp_tls) this.cfg.smtp_starttls = false; },
    get testFailed() { return this.testResult.indexOf('error') === 0; },

    async loadSettings() {
      const resp = await apiFetch('/email/settings');
      const data = await readJson(resp, null);
      if (!data) return;
      this.cfg = {
        enabled: !!data.enabled,
        smtp_host: data.smtp_host || '',
        smtp_port: data.smtp_port || 587,
        smtp_from: data.smtp_from || '',
        smtp_username: data.smtp_username || '',
        smtp_starttls: data.smtp_starttls !== false,
        smtp_tls: !!data.smtp_tls,
        public_base_url: data.public_base_url || '',
      };
    },

    async save() {
      this.saving = true;
      this.message = '';
      const resp = await apiFetch('/email/settings', {
        method: 'PUT', body: JSON.stringify(this.cfg),
      });
      this.saving = false;
      this.message = resp && resp.ok ? 'Email settings saved.' : 'Could not save.';
      if (resp && resp.ok) await this.loadSettings();
    },

    async runTest() {
      this.testing = true;
      this.testResult = '';
      const resp = await apiFetch('/email/test', { method: 'POST' });
      const data = await readJson(resp, null);
      this.testing = false;
      this.testResult = data && data.result ? data.result : 'error: request failed';
    },
  }));

  Alpine.data('adminGeneral', () => ({
    cfg: {
      registration_enabled: true,
      access_token_expire_minutes: 480,
      audit_persist: true,
      login_max_attempts: 5,
      login_lockout_seconds: 300,
      registration_max_attempts: 5,
      registration_lockout_seconds: 3600,
      password_reset_max_attempts: 5,
      password_reset_lockout_seconds: 3600,
    },
    saving: false,
    message: '',

    async init() { await this.loadSettings(); },

    async loadSettings() {
      const resp = await apiFetch('/general/settings');
      const data = await readJson(resp, null);
      if (!data) return;
      for (const key of Object.keys(this.cfg)) {
        if (data[key] !== undefined) this.cfg[key] = data[key];
      }
    },

    async save() {
      this.saving = true;
      this.message = '';
      const resp = await apiFetch('/general/settings', {
        method: 'PUT', body: JSON.stringify(this.cfg),
      });
      this.saving = false;
      this.message = resp && resp.ok ? 'General settings saved.' : 'Could not save.';
      if (resp && resp.ok) await this.loadSettings();
    },
  }));

  Alpine.data('adminLlm', () => ({
    cfg: {
      llm_provider: 'none', llm_max_tokens: 600,
      anthropic_model: '', bedrock_model: '', bedrock_aws_region: '',
      openai_model: '', openai_base_url: '', ollama_model: '', ollama_base_url: '',
      gemini_model: '', gemini_base_url: '',
    },
    keys: { anthropic: false, openai: false, gemini: false },
    saving: false,
    testing: false,
    message: '',

    async init() { await this.loadSettings(); },
    keyStatus(key) { return this.keys[key] ? 'set' : 'not set'; },
    keyClass(key) { return this.keys[key] ? 'text-ok' : 'text-crit'; },

    async loadSettings() {
      const resp = await apiFetch('/llm/settings');
      const data = await readJson(resp, null);
      if (!data) return;
      for (const key of Object.keys(this.cfg)) {
        if (data[key] !== undefined) this.cfg[key] = data[key];
      }
      this.keys = data.api_keys_set || this.keys;
    },

    async save() {
      this.saving = true;
      this.message = '';
      const resp = await apiFetch('/llm/settings', {
        method: 'PUT', body: JSON.stringify(this.cfg),
      });
      const data = await readJson(resp, null);
      this.saving = false;
      if (resp && resp.ok) {
        this.message = 'AI settings saved.';
        await this.loadSettings();
      } else {
        const detail = data && typeof data.detail === 'string' ? data.detail : 'Could not save.';
        this.message = detail;
      }
    },

    async runTest() {
      this.testing = true;
      this.message = '';
      const resp = await apiFetch('/llm/test', { method: 'POST' });
      const data = await readJson(resp, null);
      this.testing = false;
      this.message = data && data.result ? data.result : 'error: request failed';
    },
  }));

  Alpine.data('adminOidc', () => ({
    cfg: {
      auth_mode: 'both', oidc_redirect_base_url: '',
      oidc_entra_enabled: false, oidc_entra_client_id: '', oidc_entra_tenant_id: '',
      oidc_entra_scopes: 'openid email profile', oidc_entra_role_claim: '', oidc_entra_role_map: '',
      oidc_authentik_enabled: false, oidc_authentik_client_id: '', oidc_authentik_base_url: '',
      oidc_authentik_app_slug: '', oidc_authentik_scopes: 'openid email profile',
      oidc_authentik_role_claim: 'groups', oidc_authentik_role_map: '',
      oidc_auth0_enabled: false, oidc_auth0_client_id: '', oidc_auth0_domain: '',
      oidc_auth0_scopes: 'openid email profile', oidc_auth0_role_claim: '', oidc_auth0_role_map: '',
      oidc_okta_enabled: false, oidc_okta_client_id: '', oidc_okta_domain: '',
      oidc_okta_auth_server: '', oidc_okta_scopes: 'openid email profile',
      oidc_okta_role_claim: 'groups', oidc_okta_role_map: '',
    },
    secrets: { entra: false, authentik: false, auth0: false, okta: false },
    saving: false,
    message: '',

    async init() { await this.loadSettings(); },
    secretStatus(key) { return this.secrets[key] ? 'set' : 'not set'; },
    secretClass(key) { return this.secrets[key] ? 'text-ok' : 'text-crit'; },

    async loadSettings() {
      const resp = await apiFetch('/oidc/settings');
      const data = await readJson(resp, null);
      if (!data) return;
      for (const key of Object.keys(this.cfg)) {
        if (data[key] !== undefined) this.cfg[key] = data[key];
      }
      this.secrets = data.client_secrets_set || this.secrets;
    },

    async save() {
      this.saving = true;
      this.message = '';
      const resp = await apiFetch('/oidc/settings', {
        method: 'PUT', body: JSON.stringify(this.cfg),
      });
      const data = await readJson(resp, null);
      this.saving = false;
      if (resp && resp.ok) {
        this.message = 'SSO settings saved.';
        await this.loadSettings();
      } else {
        this.message = data && typeof data.detail === 'string'
          ? data.detail : 'Could not save.';
      }
    },
  }));
});
