// Exercise page components — registered via Alpine.data (strict CSP, #77).

document.addEventListener('alpine:init', () => {
  Alpine.data('exerciseList', (showCreate = false) => ({
    ...DT.uiHelpers,
    loading: true,
    exercises: [],
    scenarios: [],
    isF: false,
    showCreate,
    newTitle: '',
    newScenarioId: '',
    newLlmEnabled: false,
    creating: false,
    createError: '',

    async init() {
      const me = await apiFetch('/auth/me');
      if (!me) return;
      const meData = await readJson(me);
      if (!meData) return;
      this.isF = meData.role === 'facilitator';

      const [er, sr] = await Promise.all([
        apiFetch('/exercises'),
        this.isF ? apiFetch('/scenarios') : Promise.resolve(null),
      ]);
      if (er && er.ok) this.exercises = await readJson(er, []);
      if (sr && sr.ok) this.scenarios = await readJson(sr, []);
      this.loading = false;
    },

    async load() {
      const r = await apiFetch('/exercises');
      if (r && r.ok) this.exercises = await readJson(r, []);
    },

    async createExercise() {
      if (!this.newTitle || !this.newScenarioId) {
        this.createError = 'Title and scenario are required.';
        return;
      }
      this.creating = true; this.createError = '';
      const r = await apiFetch('/exercises', {
        method: 'POST',
        body: JSON.stringify({
          title: this.newTitle,
          scenario_id: parseInt(this.newScenarioId),
          llm_enabled: this.newLlmEnabled,
        }),
      });
      this.creating = false;
      if (r && r.ok) {
        this.showCreate = false;
        this.newTitle = ''; this.newScenarioId = ''; this.newLlmEnabled = false;
        await this.load();
      } else if (r) {
        const data = await r.json();
        this.createError = data.detail || 'Failed to create exercise.';
      }
    },

    async doTransition(ex, action) {
      const r = await apiFetch(`/exercises/${ex.id}/${action}`, { method: 'POST' });
      if (r && r.ok) {
        const updated = await r.json();
        const idx = this.exercises.findIndex(e => e.id === ex.id);
        if (idx !== -1) this.exercises[idx] = updated;
        // The rail caches the live set and never re-inits on soft-nav (#96).
        document.dispatchEvent(new CustomEvent('dt:exercises-changed'));
      }
    },

    async deleteExercise(id) {
      if (!confirm('Delete this exercise?')) return;
      await apiFetch(`/exercises/${id}`, { method: 'DELETE' });
      await this.load();
    },
  }));

  Alpine.data('participantView', (exerciseId) => ({
    ...DT.uiHelpers,
    loading: true,
    exercise: null,
    role: '',
    injects: [],
    comments: [],
    freeText: {},
    commentDraft: {},
    selectedOption: {},
    submitting: {},
    commentSubmitting: {},
    submitted: {},
    responseErrors: {},
    commentErrors: {},
    ws: null,
    wsConnected: false,
    pingInterval: null,
    reconnectTimeout: null,
    destroyed: false,

    get isObserver() { return this.role === 'observer'; },
    get currentBriefId() {
      const current = this.injects.find(inj => inj.state === 'released' && !this.submitted[inj.id]);
      return current ? current.id : null;
    },

    async init() {
      const [me, er, ir, rr, cr] = await Promise.all([
        apiFetch('/auth/me'),
        apiFetch(`/exercises/${exerciseId}`),
        apiFetch(`/exercises/${exerciseId}/injects`),
        apiFetch(`/exercises/${exerciseId}/responses`),
        apiFetch(`/exercises/${exerciseId}/inject-comments`),
      ]);
      if (me && me.ok) { const d = await me.json(); this.role = d.role; }
      if (er && er.ok) this.exercise = await er.json();
      if (rr && rr.ok) {
        const responses = await rr.json();
        this.submitted = Object.fromEntries(responses.map(r => [r.inject_id, true]));
      }
      if (cr && cr.ok) this.comments = await cr.json();
      if (ir && ir.ok) {
        const all = await ir.json();
        for (const inj of all.filter(i => i.state === 'released' || i.state === 'resolved')) {
          await this._enrichAndAdd(inj);
        }
      }
      this.loading = false;
      this.connectWs();
    },

    get exId() {
      return this.exercise ? this.padId(this.exercise.id, 3) : '';
    },

    async _enrichAndAdd(inj) {
      inj._options = inj.options || [];
      const idx = this.injects.findIndex(i => i.id === inj.id);
      if (idx === -1) this.injects.push(inj);
      else this.injects[idx] = { ...this.injects[idx], ...inj };
      this.sortInjects();
    },

    sortInjects() {
      this.injects.sort((a, b) => {
        const aReleased = a.released_at ? new Date(a.released_at).getTime() : Number.MAX_SAFE_INTEGER;
        const bReleased = b.released_at ? new Date(b.released_at).getTime() : Number.MAX_SAFE_INTEGER;
        if (aReleased !== bReleased) return aReleased - bReleased;
        if ((a.sequence_order || 0) !== (b.sequence_order || 0)) {
          return (a.sequence_order || 0) - (b.sequence_order || 0);
        }
        return (a.id || 0) - (b.id || 0);
      });
    },

    isCurrentBrief(inj) {
      return inj.id === this.currentBriefId;
    },

    commentsFor(injectId) {
      return this.comments
        .filter(comment => comment.inject_id === injectId)
        .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime());
    },

    briefLabel(inj) {
      if (this.isCurrentBrief(inj)) return 'Current brief';
      if (this.submitted[inj.id] || inj.state === 'resolved') return 'Completed brief';
      return 'Later brief';
    },

    requiresFreeText(inj) {
      return inj.free_text_response !== false || !(inj._options && inj._options.length);
    },

    canSubmit(inj) {
      if (this.exercise.state === 'paused' || this.submitting[inj.id]) return false;
      return !this.requiresFreeText(inj) || !!(this.freeText[inj.id] || '').trim();
    },

    canComment(inj) {
      if (this.exercise.state !== 'active' || this.commentSubmitting[inj.id]) return false;
      return !!(this.commentDraft[inj.id] || '').trim();
    },

    connectWs() {
      // Cookie-authenticated socket (#68) — shared lifecycle in app.js.
      DT.connectExerciseWs(exerciseId, this, {
        viewParams: true,
        onMessage: async (msg) => {
          if (msg.type === 'inject_released') {
            await this._enrichAndAdd(msg.payload);
          }
          if (msg.type === 'exercise_state_change') {
            this.exercise = { ...this.exercise, state: msg.payload.state };
          }
          if (msg.type === 'inject_comment_created') {
            this.upsertComment(msg.payload);
          }
        },
      });
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

    async submitResponse(inj) {
      const content = (this.freeText[inj.id] || '').trim();
      if (!content) return;
      this.responseErrors = { ...this.responseErrors, [inj.id]: '' };
      this.submitting = { ...this.submitting, [inj.id]: true };
      const r = await apiFetch(`/exercises/${exerciseId}/responses`, {
        method: 'POST',
        body: JSON.stringify({
          inject_id: inj.id,
          content,
          selected_option: this.selectedOption[inj.id] || null,
        }),
      });
      this.submitting = { ...this.submitting, [inj.id]: false };
      if (r && r.ok) {
        this.submitted = { ...this.submitted, [inj.id]: true };
        return;
      }

      const data = await readJson(r, {});
      const message = data.detail || 'Response could not be submitted.';
      if (r && r.status === 409 && message.toLowerCase().includes('already submitted')) {
        this.submitted = { ...this.submitted, [inj.id]: true };
        return;
      }
      this.responseErrors = { ...this.responseErrors, [inj.id]: message };
    },

    upsertComment(comment) {
      if (!comment || !comment.id) return;
      const idx = this.comments.findIndex(c => c.id === comment.id);
      if (idx === -1) this.comments = [...this.comments, comment];
      else {
        this.comments[idx] = { ...this.comments[idx], ...comment };
        this.comments = [...this.comments];
      }
    },

    async submitComment(inj) {
      const content = (this.commentDraft[inj.id] || '').trim();
      if (!content) return;
      this.commentErrors = { ...this.commentErrors, [inj.id]: '' };
      this.commentSubmitting = { ...this.commentSubmitting, [inj.id]: true };
      const r = await apiFetch(`/exercises/${exerciseId}/inject-comments`, {
        method: 'POST',
        body: JSON.stringify({ inject_id: inj.id, content }),
      });
      this.commentSubmitting = { ...this.commentSubmitting, [inj.id]: false };
      if (r && r.ok) {
        const comment = await r.json();
        this.upsertComment(comment);
        this.commentDraft = { ...this.commentDraft, [inj.id]: '' };
        return;
      }
      const data = await readJson(r, {});
      this.commentErrors = {
        ...this.commentErrors,
        [inj.id]: data.detail || 'Comment could not be posted.',
      };
    },
  }));

  Alpine.data('facilitatorConsole', (exerciseId) => ({
    ...DT.uiHelpers,
    loading: true,
    exercise: null,
    members: [],
    allUsers: [],
    memberSearch: '',
    injects: [],
    responses: [],
    comments: [],
    assessments: {},
    suggestedInjects: [],
    nodeMap: {},
    groupDefs: [],
    newTitle: '',
    newContent: '',
    newGroup: '',
    newAttachment: null,
    showAddInject: false,
    showAddMember: false,
    showExport: false,
    showOpsPanel: false,
    mobilePane: 'injects',
    isMobileConsole: false,
    mobileMediaQuery: null,
    mobileMediaListener: null,
    responseFilter: 'all',
    ws: null,
    wsConnected: false,
    pingInterval: null,
    reconnectTimeout: null,
    destroyed: false,
    elapsedStr: '—',
    elapsedInterval: null,

    // Shell getters — the CSP build cannot evaluate `?.` in directives.
    get exState() { return this.exercise ? this.exercise.state : ''; },
    get exTitle() { return (this.exercise && this.exercise.title) || '…'; },
    get exId() { return this.exercise ? 'EX-' + this.padId(this.exercise.id, 3) : ''; },
    get commsHref() { return this.exercise ? '/exercises/' + this.exercise.id + '/communications' : '#'; },
    get reviewHref() { return '/exercises/' + (this.exercise ? this.exercise.id : 0) + '/review'; },
    get exportHref() { return '/api/exercises/' + (this.exercise ? this.exercise.id : 0) + '/export'; },
    get exportCsvHref() { return '/api/exercises/' + (this.exercise ? this.exercise.id : 0) + '/export.csv'; },
    get liveCountLabel() {
      const live = this.injects.filter(i => i.state !== 'pending').length;
      return live + '/' + this.injects.length + ' live';
    },
    get mobileInjectCountLabel() {
      const live = this.injects.filter(i => i.state !== 'pending').length;
      return live + '/' + this.injects.length;
    },
    get newAttachmentName() { return this.newAttachment ? this.newAttachment.name : ''; },
    get pendingSuggested() {
      return this.suggestedInjects.filter(s => s.status === 'pending_review');
    },
    get hasPendingSuggestions() { return this.pendingSuggested.length > 0; },
    get pendingSuggestedCount() { return this.pendingSuggested.length; },
    get opsPanelVisible() {
      return this.showOpsPanel || (this.isMobileConsole && this.mobilePane === 'ops');
    },

    get filteredResponses() {
      if (this.responseFilter === 'node')
        return this.responses.filter(r => this.injects.find(i => i.id === r.inject_id)?.state === 'released');
      if (this.responseFilter === 'flagged')
        return this.responses.filter(r => this.assessments[r.id]?.decision_quality === 'poor');
      return this.responses;
    },

    get latestComments() {
      return [...this.comments]
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
        .slice(0, 12);
    },

    get injectSections() {
      const groups = [{ id: null, label: 'Shared', injects: [] }];
      for (const group of this.groupDefs) groups.push({ id: group.id, label: group.label || group.id, injects: [] });
      for (const inj of this.injects) {
        const groupId = inj.group_id || null;
        let section = groups.find(g => g.id === groupId);
        if (!section) {
          section = { id: groupId, label: groupId || 'Shared', injects: [] };
          groups.push(section);
        }
        section.injects.push(inj);
      }
      return groups.filter(g => g.injects.length || g.id === null || this.groupDefs.length);
    },

    get memberSections() {
      const sections = this.groupDefs.map(g => ({ id: g.id, label: g.label || g.id, members: [] }));
      const unassigned = { id: null, label: 'Unassigned', members: [] };
      for (const member of this.members) {
        const section = sections.find(g => g.id === member.group_id) || unassigned;
        section.members.push(member);
      }
      return [...sections, unassigned].filter(s => s.members.length || s.id !== null);
    },

    get filteredUsers() {
      const q = this.memberSearch.toLowerCase();
      const enrolled = new Set(this.members.map(m => m.user_id));
      return this.allUsers.filter(u =>
        !enrolled.has(u.id) &&
        (u.display_name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q))
      );
    },

    userFor(userId) {
      return this.allUsers.find(u => u.id === userId) || null;
    },

    userDisplayName(userId) {
      return this.userFor(userId)?.display_name || `User #${userId}`;
    },

    userInitials(userId) {
      const name = this.userDisplayName(userId);
      if (name.startsWith('User #')) return 'Us';
      return name.split(' ').map(part => part[0]).join('').slice(0, 2);
    },

    // Initials for an enrolled member (replaces an inline .map arrow in the template).
    memberInitials(userId) {
      const user = this.userFor(userId);
      if (!user) return '#' + userId;
      return user.display_name.split(' ').map(x => x[0]).join('').slice(0, 2);
    },

    memberGroupLabel(userId) {
      return 'Team for ' + this.userDisplayName(userId);
    },

    injectById(injectId) {
      return this.injects.find(i => i.id === injectId) || null;
    },

    responseInjectLabel(resp) {
      const inject = this.injectById(resp.inject_id);
      if (!inject) return `inject #${resp.inject_id}`;
      return inject.title;
    },

    groupLabel(groupId) {
      if (!groupId) return 'Shared';
      const group = this.groupDefs.find(g => g.id === groupId);
      return group?.label || groupId;
    },

    // Inline style for the AI decision-quality pill (replaces an object-literal
    // :style with nested ternaries in the template).
    qualityStyle(quality) {
      const color = quality === 'good' ? '#3b8e5a' : quality === 'adequate' ? '#c08a26' : '#9a3412';
      const shadow = quality === 'good' ? '#3b8e5a33' : quality === 'adequate' ? '#c08a2633' : '#9a341233';
      const bg = quality === 'good' ? '#3b8e5a10' : quality === 'adequate' ? '#c08a2610' : '#9a341210';
      return `color:${color}; box-shadow: inset 0 0 0 1px ${shadow}; background:${bg}`;
    },

    // Sync a <select>'s DOM value after reactive re-render (replaces an inline
    // x-effect arrow, which the CSP build cannot parse).
    syncSelectValue(el, value) {
      this.$nextTick(() => { el.value = value || ''; });
    },

    setMobilePane(pane) {
      if (!['injects', 'responses', 'ops'].includes(pane)) return;
      this.mobilePane = pane;
    },

    focusMobilePane(pane) {
      this.setMobilePane(pane);
      const refs = {
        injects: 'mobileInjectsTab',
        responses: 'mobileResponsesTab',
        ops: 'mobileOpsTab',
      };
      this.$nextTick(() => {
        const tab = this.$refs[refs[pane]];
        if (tab) tab.focus();
      });
    },

    cycleMobilePane(direction) {
      const panes = ['injects', 'responses', 'ops'];
      const current = panes.indexOf(this.mobilePane);
      const next = (current + direction + panes.length) % panes.length;
      this.focusMobilePane(panes[next]);
    },

    initResponsiveConsole() {
      // The desktop rail consumes 240px, so compact the console before its
      // three-pane minimum width would overflow the remaining workspace.
      const query = window.matchMedia('(max-width: 1120px)');
      this.mobileMediaQuery = query;
      this.isMobileConsole = query.matches;
      this.mobileMediaListener = (event) => {
        this.isMobileConsole = event.matches;
      };
      query.addEventListener('change', this.mobileMediaListener);
    },

    async init() {
      this.initResponsiveConsole();
      await this.load();
      this.connectWs();
    },

    async load() {
      const [er, mr, ir, rr, cr, sir, ur] = await Promise.all([
        apiFetch(`/exercises/${exerciseId}`),
        apiFetch(`/exercises/${exerciseId}/members`),
        apiFetch(`/exercises/${exerciseId}/injects`),
        apiFetch(`/exercises/${exerciseId}/responses`),
        apiFetch(`/exercises/${exerciseId}/inject-comments`),
        apiFetch(`/exercises/${exerciseId}/suggested-injects`),
        apiFetch('/users'),
      ]);
      if (er && er.ok) this.exercise = await er.json();
      if (mr && mr.ok) this.members = await mr.json();
      if (ir && ir.ok) this.injects = await ir.json();
      if (rr && rr.ok) {
        const rows = await rr.json();
        this.responses = rows.map(r => ({
          ...r,
          _next_inject_ids: r.next_inject_ids || [],
          _next_injects: r.next_injects || [],
        }));
      }
      if (cr && cr.ok) this.comments = await cr.json();
      if (sir && sir.ok) this.suggestedInjects = await sir.json();
      if (ur && ur.ok) this.allUsers = await ur.json();
      if (this.exercise?.scenario_id) {
        const sr = await apiFetch(`/scenarios/${this.exercise.scenario_id}`);
        if (sr && sr.ok) {
          const sc = await sr.json();
          this.nodeMap = Object.fromEntries((sc.definition?.injects || []).map(n => [n.id, n]));
          this.groupDefs = sc.definition?.participant_teams || [];
        }
      }
      this.loading = false;
      this._startElapsed();
    },

    _startElapsed() {
      if (this.elapsedInterval) clearInterval(this.elapsedInterval);
      if (this.exercise?.started_at) {
        this.elapsedStr = this.fmtElapsed(this.exercise.started_at);
        this.elapsedInterval = setInterval(() => {
          if (this.exercise?.started_at) this.elapsedStr = this.fmtElapsed(this.exercise.started_at);
        }, 10000);
      }
    },

    connectWs() {
      // Cookie-authenticated socket (#68) — shared lifecycle in app.js. The
      // console is always the real facilitator view, so no view-preview params.
      DT.connectExerciseWs(exerciseId, this, {
        onMessage: (msg) => {
          if (msg.type === 'inject_released') {
            const idx = this.injects.findIndex(i => i.id === msg.payload.id);
            if (idx !== -1) this.injects[idx] = msg.payload;
            else this.injects.push(msg.payload);
          }
          if (msg.type === 'response_submitted') {
            const r = {
              ...msg.payload.response,
              _next_inject_ids: msg.payload.next_inject_ids || [],
              _next_injects: msg.payload.next_injects || [],
            };
            this.responses.unshift(r);
          }
          if (msg.type === 'inject_comment_created') {
            this.upsertComment(msg.payload);
          }
          if (msg.type === 'exercise_state_change') {
            this.exercise = { ...this.exercise, state: msg.payload.state };
          }
          if (msg.type === 'assessment_ready') {
            this.assessments[msg.payload.response_id] = msg.payload.assessment;
            this.assessments = { ...this.assessments };
          }
          if (msg.type === 'inject_suggested') {
            this.suggestedInjects.unshift(msg.payload);
          }
        },
      });
    },

    destroy() {
      this.destroyed = true;
      if (this.mobileMediaQuery && this.mobileMediaListener) {
        this.mobileMediaQuery.removeEventListener('change', this.mobileMediaListener);
      }
      this.mobileMediaQuery = null;
      this.mobileMediaListener = null;
      if (this.elapsedInterval) clearInterval(this.elapsedInterval);
      if (this.pingInterval) clearInterval(this.pingInterval);
      if (this.reconnectTimeout) clearTimeout(this.reconnectTimeout);
      this.elapsedInterval = null;
      this.pingInterval = null;
      this.reconnectTimeout = null;
      if (this.ws) {
        this.ws.onclose = null;
        this.ws.close();
        this.ws = null;
      }
    },

    async transition(action) {
      const r = await apiFetch(`/exercises/${exerciseId}/${action}`, { method: 'POST' });
      if (r && r.ok) {
        this.exercise = await r.json();
        this._startElapsed();
        // The rail caches the live set and never re-inits on soft-nav (#96).
        document.dispatchEvent(new CustomEvent('dt:exercises-changed'));
      }
    },

    async releaseInject(inj) {
      const r = await apiFetch(`/exercises/${exerciseId}/injects/${inj.id}/release`, { method: 'POST' });
      if (r && r.ok) {
        const updated = await r.json();
        const idx = this.injects.findIndex(i => i.id === inj.id);
        if (idx !== -1) this.injects[idx] = updated;
      }
    },

    async releaseInjectById(injectId) {
      const inj = this.injectById(injectId);
      if (inj) await this.releaseInject(inj);
    },

    async addInject() {
      if (!this.newTitle || !this.newContent) return;
      const payload = new FormData();
      payload.append('title', this.newTitle);
      payload.append('content', this.newContent);
      payload.append('group_id', this.newGroup || '');
      payload.append('sequence_order', String(this.injects.length));
      if (this.newAttachment) payload.append('attachment', this.newAttachment);
      const r = await apiFetch(`/exercises/${exerciseId}/injects`, {
        method: 'POST',
        body: payload,
      });
      if (r && r.ok) {
        this.injects.push(await r.json());
        this.newTitle = ''; this.newContent = ''; this.newGroup = ''; this.newAttachment = null;
        if (this.$refs.injectAttachment) this.$refs.injectAttachment.value = '';
      }
    },

    async addMember(userId) {
      const r = await apiFetch(`/exercises/${exerciseId}/members`, {
        method: 'POST',
        body: JSON.stringify({ user_id: userId }),
      });
      if (r && r.ok) {
        this.memberSearch = '';
        const mr = await apiFetch(`/exercises/${exerciseId}/members`);
        if (mr && mr.ok) this.members = await mr.json();
      }
    },

    async removeMember(userId) {
      if (!confirm('Remove this member?')) return;
      await apiFetch(`/exercises/${exerciseId}/members/${userId}`, { method: 'DELETE' });
      const mr = await apiFetch(`/exercises/${exerciseId}/members`);
      if (mr && mr.ok) this.members = await mr.json();
    },

    // Single-call wrapper for the group <select> @change — the strict-CSP Alpine
    // parser (#77) can't evaluate a compound "a = b; f()" directive expression.
    changeMemberGroup(member, value) {
      member.group_id = value || null;
      this.setMemberGroup(member, member.group_id);
    },

    async setMemberGroup(member, groupId) {
      const r = await apiFetch(`/exercises/${exerciseId}/members/${member.user_id}`, {
        method: 'PATCH',
        body: JSON.stringify({ group_id: groupId || null }),
      });
      if (r && r.ok) {
        const updated = await r.json();
        const idx = this.members.findIndex(m => m.user_id === member.user_id);
        if (idx !== -1) this.members[idx] = updated;
      }
    },

    // Clear the pending inject attachment (single-call for the CSP parser, #77).
    clearInjectAttachment() {
      this.newAttachment = null;
      if (this.$refs.injectAttachment) this.$refs.injectAttachment.value = '';
    },

    async approveSuggestion(s) {
      const r = await apiFetch(`/exercises/${exerciseId}/suggested-injects/${s.id}/approve`, { method: 'POST' });
      if (r && r.ok) {
        const newInject = await r.json();
        this.injects.push(newInject);
        const idx = this.suggestedInjects.findIndex(x => x.id === s.id);
        if (idx !== -1) this.suggestedInjects[idx] = { ...s, status: 'approved' };
      }
    },

    async rejectSuggestion(s) {
      const r = await apiFetch(`/exercises/${exerciseId}/suggested-injects/${s.id}/reject`, { method: 'POST' });
      if (r && r.ok) {
        const idx = this.suggestedInjects.findIndex(x => x.id === s.id);
        if (idx !== -1) this.suggestedInjects[idx] = { ...s, status: 'rejected' };
      }
    },

    upsertComment(comment) {
      if (!comment || !comment.id) return;
      const idx = this.comments.findIndex(c => c.id === comment.id);
      if (idx === -1) this.comments.unshift(comment);
      else {
        this.comments[idx] = { ...this.comments[idx], ...comment };
        this.comments = [...this.comments];
      }
    },
  }));
});
