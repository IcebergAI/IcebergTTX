// Communications inbox component — registered via Alpine.data (strict CSP, #77).

document.addEventListener('alpine:init', () => {
  Alpine.data('commsInbox', (exerciseId) => ({
    ...DT.uiHelpers,
    comms: [],
    commSearch: '',
    directionFilter: 'all',
    selected: null,
    isF: false,
    showCompose: false,
    showSend: false,
    ws: null,
    wsConnected: false,
    pingInterval: null,
    reconnectTimeout: null,
    destroyed: false,
    teamOptions: [],
    compose: { external_entity: '', subject: '', body: '', visible_to_teams: [] },
    send: {
      recipient_type: 'external',
      recipient_team: '',
      external_entity: '',
      subject: '',
      body: '',
    },

    get filteredComms() {
      const q = this.commSearch.toLowerCase();
      return this.comms.filter(c => {
        const matchesDirection =
          this.directionFilter === 'all' || c.direction === this.directionFilter;
        const matchesSearch =
          !q || c.subject.toLowerCase().includes(q) || c.body.toLowerCase().includes(q);
        return matchesDirection && matchesSearch;
      });
    },

    get selectedId() {
      return this.selected ? this.padId(this.selected.id, 3) : '';
    },

    async init() {
      const me = await apiFetch('/auth/me');
      if (me && me.ok) {
        const d = await me.json();
        this.isF = d.role === 'facilitator';
      }
      await this.loadTeamOptions();
      await this.load();
      this.connectWs();
    },

    async loadTeamOptions() {
      const teams = await apiFetch(`/exercises/${exerciseId}/teams`);
      if (!teams || !teams.ok) return;
      this.teamOptions = await teams.json();
    },

    async load() {
      const r = await apiFetch(`/exercises/${exerciseId}/communications`);
      if (r && r.ok) this.comms = await r.json();
    },

    async open(c) {
      const r = await apiFetch(`/exercises/${exerciseId}/communications/${c.id}`);
      if (r && r.ok) {
        const updated = await r.json();
        this.upsertComm(updated, { preserveOrder: true });
        this.selected = updated;
      }
    },

    upsertComm(comm, { preserveOrder = false } = {}) {
      const idx = this.comms.findIndex(x => x.id === comm.id);
      if (idx !== -1) {
        this.comms[idx] = comm;
        return;
      }
      if (preserveOrder) this.comms.push(comm);
      else this.comms.unshift(comm);
    },

    openSendReply(comm) {
      this.send.recipient_type = 'external';
      this.send.recipient_team = '';
      this.send.external_entity = comm.external_entity || '';
      this.send.subject = this.replySubject(comm.subject);
      this.send.body = '';
      this.showSend = true;
    },

    openInjectInbound(comm = null) {
      const source = comm || null;
      if (!source) {
        this.compose = { external_entity: '', subject: '', body: '', visible_to_teams: [] };
        this.showCompose = true;
        return;
      }
      this.compose.external_entity = source.external_entity || '';
      this.compose.subject = this.replySubject(source.subject);
      this.compose.body = '';
      this.compose.visible_to_teams = this.replyTargetTeams(source);
      this.showCompose = true;
    },

    replySubject(subject) {
      const trimmed = (subject || '').trim();
      if (!trimmed) return '';
      return /^re:\s*/i.test(trimmed) ? trimmed : `Re: ${trimmed}`;
    },

    replyTargetTeams(comm) {
      if (comm.direction === 'outbound' && comm.sender_team) return [comm.sender_team];
      return comm.visible_to_teams ? [...comm.visible_to_teams] : [];
    },

    async sendComm() {
      if (this.send.recipient_type === 'team' && !this.send.recipient_team) return;
      const teams = this.send.recipient_type === 'team' ? [this.send.recipient_team] : null;
      const r = await apiFetch(`/exercises/${exerciseId}/communications`, {
        method: 'POST',
        body: JSON.stringify({
          direction: 'outbound',
          external_entity: teams ? null : this.send.external_entity,
          subject: this.send.subject,
          body: this.send.body,
          visible_to_teams: teams,
        }),
      });
      if (r && r.ok) {
        const created = await r.json();
        this.upsertComm(created);
        this.showSend = false;
        this.send = {
          recipient_type: 'external',
          recipient_team: '',
          external_entity: '',
          subject: '',
          body: '',
        };
      }
    },

    async injectComm() {
      const teams = this.compose.visible_to_teams.length ? this.compose.visible_to_teams : null;
      const r = await apiFetch(`/exercises/${exerciseId}/communications/inject`, {
        method: 'POST',
        body: JSON.stringify({
          external_entity: this.compose.external_entity,
          subject: this.compose.subject,
          body: this.compose.body,
          visible_to_teams: teams,
        }),
      });
      if (r && r.ok) {
        const created = await r.json();
        this.upsertComm(created);
        this.showCompose = false;
        this.compose = { external_entity: '', subject: '', body: '', visible_to_teams: [] };
      }
    },

    connectWs() {
      const token = localStorage.getItem('dt_token');
      if (!token) return;
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      // Auth rides on the httpOnly access_token cookie the browser sends on the
      // upgrade — the JWT is no longer placed in the URL (#68). Only the
      // non-secret role-preview hints go in the query string.
      const params = new URLSearchParams();
      const viewRole = localStorage.getItem('dt_view_role');
      const viewTeam = localStorage.getItem('dt_view_team');
      if (viewRole) params.set('view_role', viewRole);
      if (viewTeam) params.set('view_team', viewTeam);
      const qs = params.toString();
      this.ws = new WebSocket(`${proto}://${location.host}/ws/exercises/${exerciseId}${qs ? '?' + qs : ''}`);

      this.ws.onopen = () => {
        this.wsConnected = true;
        this.pingInterval = setInterval(() => {
          if (this.ws && this.ws.readyState === WebSocket.OPEN)
            this.ws.send(JSON.stringify({ type: 'ping' }));
        }, 30000);
      };

      this.ws.onclose = () => {
        this.wsConnected = false;
        clearInterval(this.pingInterval);
        this.pingInterval = null;
        if (!this.destroyed) this.reconnectTimeout = setTimeout(() => this.connectWs(), 3000);
      };

      this.ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'communication_received') {
          this.upsertComm(msg.payload);
        }
      };
    },

    destroy() {
      this.destroyed = true;
      if (this.pingInterval) clearInterval(this.pingInterval);
      if (this.reconnectTimeout) clearTimeout(this.reconnectTimeout);
      this.pingInterval = null;
      this.reconnectTimeout = null;
      if (this.ws) {
        this.ws.onclose = null;
        this.ws.close();
        this.ws = null;
      }
    },

    commListMeta(comm) {
      if (comm.direction === 'inbound') {
        return `In · ${comm.external_entity || 'Internal'}`;
      }
      const entity = this.commRecipientLabel(comm);
      return comm.sender_team
        ? `Out · ${this.teamDisplay(comm.sender_team)} · ${entity}`
        : `Out · ${entity}`;
    },
    commBadgeLabel(comm) {
      if (comm.direction === 'inbound') {
        return `Inbound · ${comm.external_entity || 'Internal'}`;
      }
      const entity = this.commRecipientLabel(comm);
      return comm.sender_team
        ? `Outbound · ${this.teamDisplay(comm.sender_team)} · ${entity}`
        : `Outbound · ${entity}`;
    },
    hasTeamRecipients(comm) {
      return !!(comm.visible_to_teams && comm.visible_to_teams.length);
    },
    commRecipientLabel(comm) {
      if (this.hasTeamRecipients(comm)) {
        return comm.visible_to_teams.map(t => this.teamDisplay(t)).join(', ');
      }
      return comm.external_entity || 'Internal';
    },
    teamDisplay(id) {
      const team = this.teamOptions.find(t => t.id === id);
      return team?.label || id;
    },
    teamLabel(team) { return team?.label || team?.id || ''; },
  }));
});
