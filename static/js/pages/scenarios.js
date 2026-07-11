// Scenario page components — registered via Alpine.data (strict CSP, #77).

let _scenarioKeyCounter = 0;

function scenarioKey() {
  return _scenarioKeyCounter++;
}

function normalizeId(value, fallback) {
  const normalized = String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return normalized || fallback;
}

function makeOption(id) {
  return {
    _key: scenarioKey(),
    id: id || '',
    label: '',
    next_inject_id: null,
  };
}

function makeInject(id) {
  const injectId = id || 'inject_01';
  return {
    _key: scenarioKey(),
    _editingId: injectId,
    id: injectId,
    title: '',
    content: '',
    target_teams: [],
    sequence_order: 0,
    next_inject_id: null,
    options: [],
    expected_actions: [],
    _actionKeys: [],
    free_text_response: true,
    triggers_communications: [],
  };
}

document.addEventListener('alpine:init', () => {
  Alpine.data('scenarioList', () => ({
    ...DT.uiHelpers,
    loading: true, scenarios: [], isF: false,
    importError: '', importSuccess: '',

    async init() {
      const me = await apiFetch('/auth/me');
      if (!me) return;
      const meData = await readJson(me);
      if (!meData) return;
      this.isF = meData.role === 'facilitator';
      await this.load();
    },

    async load() {
      this.loading = true;
      const r = await apiFetch('/scenarios');
      if (r && r.ok) this.scenarios = await readJson(r, []);
      this.loading = false;
    },

    async importFile(event) {
      this.importError = ''; this.importSuccess = '';
      const file = event.target.files[0];
      if (!file) return;
      const text = await file.text();
      let definition;
      try {
        definition = JSON.parse(text);
      } catch {
        this.importError = 'Invalid JSON file.';
        event.target.value = '';
        return;
      }
      const resp = await apiFetch('/scenarios/import', {
        method: 'POST',
        body: JSON.stringify({ definition }),
      });
      if (resp && resp.ok) {
        this.importSuccess = 'Scenario imported successfully.';
        await this.load();
      } else {
        const data = resp ? await resp.json() : null;
        this.importError = (data && data.detail) || 'Import failed.';
      }
      event.target.value = '';
    },

    async deleteScenario(id) {
      if (!confirm('Delete this scenario?')) return;
      await apiFetch('/scenarios/' + id, { method: 'DELETE' });
      await this.load();
    },
  }));

  Alpine.data('scenarioDetail', (id) => ({
    ...DT.uiHelpers,
    loading: true, scenario: null, isF: false,

    async init() {
      const [me, r] = await Promise.all([
        apiFetch('/auth/me'),
        apiFetch('/scenarios/' + id),
      ]);
      if (me && me.ok) { const d = await me.json(); this.isF = d.role === 'facilitator'; }
      if (r && r.ok) this.scenario = await r.json();
      this.loading = false;
    },

    get injectCount() {
      return (this.scenario && this.scenario.definition && this.scenario.definition.injects)
        ? this.scenario.definition.injects.length : 0;
    },

    get treeRows() {
      if (!this.scenario) return [];
      const def = this.scenario.definition;
      if (!def || !def.injects) return [];
      const byId = Object.fromEntries(def.injects.map(i => [i.id, i]));
      const rows = [];
      const visited = new Set();
      const walk = (id, depth) => {
        if (!id || visited.has(id)) return;
        visited.add(id);
        const node = byId[id];
        if (!node) return;
        rows.push({ node, depth });
        for (const nextId of this.nextIdsForNode(node)) walk(nextId, depth + 1);
      };
      walk(def.start_inject_id, 0);
      for (const inj of def.injects) {
        if (!visited.has(inj.id)) rows.push({ node: inj, depth: 0 });
      }
      return rows;
    },

    get branchCount() {
      if (!this.scenario) return 0;
      return (this.scenario.definition?.injects || []).filter(i => i.options && i.options.length > 1).length;
    },

    get endCount() {
      if (!this.scenario) return 0;
      return (this.scenario.definition?.injects || [])
        .filter(i => (!i.options || i.options.length === 0) && !i.next_inject_id).length;
    },

    get linearCount() {
      if (!this.scenario) return 0;
      return (this.scenario.definition?.injects || [])
        .filter(i => (!i.options || i.options.length === 0) && i.next_inject_id).length;
    },

    get teamsInvolved() {
      if (!this.scenario) return [];
      const teams = new Set();
      for (const inj of (this.scenario.definition?.injects || [])) {
        for (const t of (inj.target_teams || [])) teams.add(t);
      }
      return [...teams];
    },

    get validationIssues() {
      if (!this.scenario) return [];
      const def = this.scenario.definition;
      const issues = [];
      if (!def || !def.injects || def.injects.length === 0) {
        issues.push('No injects defined');
        return issues;
      }
      if (!def.start_inject_id) issues.push('No start inject set');
      const ids = new Set(def.injects.map(i => i.id));
      for (const inj of def.injects) {
        if (inj.next_inject_id && !ids.has(inj.next_inject_id)) {
          issues.push(`Inject "${inj.id}" references unknown next inject "${inj.next_inject_id}"`);
        }
        for (const opt of (inj.options || [])) {
          if (opt.next_inject_id && !ids.has(opt.next_inject_id)) {
            issues.push(`Inject "${inj.id}" references unknown next inject "${opt.next_inject_id}"`);
          }
        }
      }
      return issues;
    },

    nextIdsForNode(node) {
      const ids = (node.options || []).map(opt => opt.next_inject_id).filter(Boolean);
      if (node.next_inject_id) ids.push(node.next_inject_id);
      return ids;
    },
  }));

  Alpine.data('scenarioEditor', (scenarioId) => ({
    ...DT.uiHelpers,
    scenarioId,
    form: {
      schema_version: '1.0',
      title: '',
      description: '',
      metadata: { author: '', estimated_duration_minutes: null },
      tags: [],
      participant_teams: [],
      injects: [],
      start_inject_id: '',
      debrief_notes: '',
    },
    tagsInput: '',
    activePanel: 'scenario',
    activeInjectKey: null,
    saving: false,
    error: '',
    success: '',

    async init() {
      if (this.scenarioId) {
        const r = await apiFetch('/scenarios/' + this.scenarioId);
        if (r && r.ok) {
          const data = await r.json();
          this.applyDefinition(data.definition);
        }
      } else {
        const first = makeInject('inject_01');
        this.form.injects = [first];
        this.form.start_inject_id = first.id;
        this.activeInjectKey = first._key;
      }
      this.ensureActiveInject();
    },

    applyDefinition(def) {
      const normalized = this.normalizeDefinition(def || {});
      this.form = normalized;
      this.tagsInput = (normalized.tags || []).join(', ');
      this.activeInjectKey = normalized.injects[0]?._key || null;
      this.activePanel = 'scenario';
    },

    normalizeDefinition(def) {
      const injects = (def.injects || []).map((inj, index) => this.normalizeInject(inj, index));
      return {
        schema_version: def.schema_version || '1.0',
        title: def.title || '',
        description: def.description || '',
        metadata: {
          author: def.metadata?.author || '',
          estimated_duration_minutes: def.metadata?.estimated_duration_minutes || null,
        },
        tags: def.tags || [],
        participant_teams: (def.participant_teams || []).map(team => this.normalizeTeam(team)),
        injects,
        start_inject_id: def.start_inject_id || injects[0]?.id || '',
        debrief_notes: def.debrief_notes || '',
      };
    },

    normalizeTeam(team) {
      const id = team?.id || this.nextTeamId();
      return {
        _key: scenarioKey(),
        _editingId: id,
        id,
        label: team?.label || '',
      };
    },

    normalizeOption(opt, index) {
      const id = opt?.id || `opt_${String(index + 1).padStart(2, '0')}`;
      return {
        _key: scenarioKey(),
        id,
        label: opt?.label || '',
        next_inject_id: opt?.next_inject_id || null,
      };
    },

    normalizeInject(inj, index) {
      const id = inj?.id || `inject_${String(index + 1).padStart(2, '0')}`;
      const expectedActions = [...(inj?.expected_actions || [])];
      return {
        _key: scenarioKey(),
        _editingId: id,
        id,
        title: inj?.title || '',
        content: inj?.content || '',
        target_teams: inj?.target_teams || [],
        sequence_order: inj?.sequence_order || index + 1,
        next_inject_id: inj?.next_inject_id || null,
        options: (inj?.options || []).map((opt, optIndex) => this.normalizeOption(opt, optIndex)),
        expected_actions: expectedActions,
        _actionKeys: expectedActions.map(() => scenarioKey()),
        free_text_response: inj?.free_text_response !== false,
        triggers_communications: inj?.triggers_communications || [],
      };
    },

    async importFile(event) {
      const file = event.target.files[0];
      if (!file) return;
      const text = await file.text();
      try {
        this.applyDefinition(JSON.parse(text));
        this.error = '';
        this.success = 'Imported JSON into the builder.';
      } catch {
        this.error = 'Invalid JSON file.';
      }
      event.target.value = '';
    },

    get tagsList() {
      return this.tagsInput.split(',').map(t => t.trim()).filter(Boolean);
    },

    get activeInject() {
      if (!this.form.injects.length) return null;
      return this.form.injects.find(inj => inj._key === this.activeInjectKey) || this.form.injects[0];
    },

    get activeIndex() {
      if (!this.activeInject) return -1;
      return this.form.injects.findIndex(inj => inj._key === this.activeInject._key);
    },

    // Injects other than the active one — used by the linear-next select (the
    // CSP build cannot evaluate an inline arrow filter in x-for).
    get otherInjects() {
      if (!this.activeInject) return this.form.injects;
      return this.form.injects.filter(node => node._key !== this.activeInject._key);
    },

    get durationLabel() {
      const mins = Number(this.form.metadata?.estimated_duration_minutes || 0);
      return mins ? `${mins} min exercise` : 'Duration not set';
    },

    get branchCount() {
      return this.form.injects.filter(inj => inj.options && inj.options.length > 1).length;
    },

    get targetedInjectCount() {
      return this.form.injects.filter(inj => inj.target_teams && inj.target_teams.length).length;
    },

    get blockingIssues() {
      return this.validationIssues.filter(issue => issue.type === 'error');
    },

    get readinessState() {
      return this.blockingIssues.length ? 'Needs work' : 'Ready';
    },

    get readinessLabel() {
      if (this.blockingIssues.length) {
        return `${this.blockingIssues.length} blocker${this.blockingIssues.length === 1 ? '' : 's'} before save`;
      }
      const warnings = this.validationIssues.filter(issue => issue.type === 'warn').length;
      return warnings ? `${warnings} advisory note${warnings === 1 ? '' : 's'}` : 'Scenario can be saved';
    },

    get readinessPillStyle() {
      return this.blockingIssues.length
        ? 'background:#fee2e2; color:#b91c1c; box-shadow: inset 0 0 0 1px #fecaca;'
        : 'background:#dcfce7; color:#166534; box-shadow: inset 0 0 0 1px #bbf7d0;';
    },

    get validationIssues() {
      const issues = [];
      const injectIds = this.form.injects.map(inj => inj.id).filter(Boolean);
      const teamIds = this.form.participant_teams.map(team => team.id).filter(Boolean);
      const duplicateInjectIds = injectIds.filter((id, index) => injectIds.indexOf(id) !== index);
      const duplicateTeamIds = teamIds.filter((id, index) => teamIds.indexOf(id) !== index);

      if (!this.form.title.trim()) {
        issues.push({ type: 'error', target: 'scenario', text: 'Add a scenario title.' });
      }
      if (!this.form.injects.length) {
        issues.push({ type: 'error', target: 'injects', text: 'Add at least one inject.' });
      }
      if (duplicateInjectIds.length) {
        issues.push({ type: 'error', target: 'injects', text: `Resolve duplicate inject ID "${duplicateInjectIds[0]}".` });
      }
      if (duplicateTeamIds.length) {
        issues.push({ type: 'error', target: 'teams', text: `Resolve duplicate team ID "${duplicateTeamIds[0]}".` });
      }
      if (!this.form.start_inject_id || !injectIds.includes(this.form.start_inject_id)) {
        issues.push({ type: 'error', target: 'scenario', text: 'Choose a valid start inject.' });
      }

      for (const inj of this.form.injects) {
        if (!inj.id) {
          issues.push({ type: 'error', target: 'injects', key: inj._key, text: 'Every inject needs an ID.' });
        }
        if (!inj.title.trim()) {
          issues.push({ type: 'error', target: 'injects', key: inj._key, text: `${inj.id || 'An inject'} needs a title.` });
        }
        if (!inj.content.trim()) {
          issues.push({ type: 'warn', target: 'injects', key: inj._key, text: `${inj.id || 'An inject'} has no participant content yet.` });
        }
        for (const teamId of (inj.target_teams || [])) {
          if (teamIds.length && !teamIds.includes(teamId)) {
            issues.push({ type: 'error', target: 'injects', key: inj._key, text: `${inj.id || 'An inject'} targets unknown team "${teamId}".` });
          }
        }
        if (inj.next_inject_id && !injectIds.includes(inj.next_inject_id)) {
          issues.push({ type: 'error', target: 'injects', key: inj._key, text: `${inj.id || 'An inject'} points to missing inject "${inj.next_inject_id}".` });
        }
        for (const opt of (inj.options || [])) {
          if (!opt.id) {
            issues.push({ type: 'error', target: 'injects', key: inj._key, text: `${inj.id || 'An inject'} has a branch option without an ID.` });
          }
          if (!opt.label.trim()) {
            issues.push({ type: 'warn', target: 'injects', key: inj._key, text: `${inj.id || 'An inject'} has an unlabeled branch option.` });
          }
          if (opt.next_inject_id && !injectIds.includes(opt.next_inject_id)) {
            issues.push({ type: 'error', target: 'injects', key: inj._key, text: `${inj.id || 'An inject'} points to missing inject "${opt.next_inject_id}".` });
          }
        }
      }

      const cycle = this.detectCycle();
      if (cycle) {
        issues.push({ type: 'error', target: 'injects', text: `Resolve the cycle that reaches "${cycle}".` });
      }
      return issues;
    },

    get flowRows() {
      const byId = Object.fromEntries(this.form.injects.filter(inj => inj.id).map(inj => [inj.id, inj]));
      const rows = [];
      const visited = new Set();
      const walk = (id, depth) => {
        if (!id) return;
        if (visited.has(id)) return;
        const inj = byId[id];
        if (!inj) {
          rows.push({
            key: `missing-${id}-${depth}`,
            id,
            title: 'Missing inject',
            meta: 'Referenced by a branch option',
            kind: 'missing',
            depth,
          });
          return;
        }
        visited.add(id);
        rows.push({
          key: `${inj._key}-${depth}`,
          id: inj.id,
          title: inj.title || 'Untitled inject',
          meta: this.injectMeta(inj),
          kind: 'inject',
          depth,
          disconnected: false,
        });
        for (const nextId of this.nextIdsForInject(inj)) walk(nextId, depth + 1);
      };
      walk(this.form.start_inject_id, 0);
      for (const inj of this.form.injects) {
        if (inj.id && !visited.has(inj.id)) {
          rows.push({
            key: `${inj._key}-disconnected`,
            id: inj.id,
            title: inj.title || 'Untitled inject',
            meta: this.injectMeta(inj),
            kind: 'inject',
            depth: 0,
            disconnected: true,
          });
        }
      }
      return rows;
    },

    injectMeta(inj) {
      const bits = [];
      if ((inj.options || []).length) {
        bits.push(`${inj.options.length} option${inj.options.length === 1 ? '' : 's'}`);
      } else if (inj.next_inject_id) {
        bits.push(`linear to ${inj.next_inject_id}`);
      } else {
        bits.push('terminal');
      }
      bits.push((inj.target_teams || []).length ? `${inj.target_teams.length} targeted` : 'shared');
      if ((inj.expected_actions || []).length) bits.push(`${inj.expected_actions.length} expected`);
      return bits.join(' · ');
    },

    // Sync a <select>'s DOM value after reactive re-render (replaces an inline
    // x-effect arrow, which the CSP build cannot parse).
    syncSelectValue(el, value) {
      this.$nextTick(() => { el.value = value || ''; });
    },

    // IDs must remain tied to Alpine's stable row keys, not mutable user-facing
    // values or array positions. That preserves label associations after edits,
    // deletions, and reordering while keeping every rendered control unique.
    fieldId(prefix, primaryKey, secondaryKey = null) {
      const parts = [prefix, primaryKey];
      if (secondaryKey !== null && secondaryKey !== undefined) parts.push(secondaryKey);
      return parts.map(part => String(part).replace(/[^a-zA-Z0-9_-]/g, '-')).join('-');
    },

    ensureActiveInject() {
      if (!this.activeInjectKey && this.form.injects[0]) this.activeInjectKey = this.form.injects[0]._key;
      if (this.activePanel === 'injects' && !this.activeInject && this.form.injects[0]) {
        this.activeInjectKey = this.form.injects[0]._key;
      }
    },

    setActiveInject(inj) {
      this.activePanel = 'injects';
      this.activeInjectKey = inj._key;
    },

    nextSequentialId(prefix, collection) {
      const used = new Set(collection.map(item => item.id).filter(Boolean));
      let i = 1;
      while (true) {
        const candidate = `${prefix}_${String(i).padStart(2, '0')}`;
        if (!used.has(candidate)) return candidate;
        i += 1;
      }
    },

    nextTeamId() {
      return this.nextSequentialId('team', this.form.participant_teams);
    },

    nextInjectId() {
      return this.nextSequentialId('inject', this.form.injects);
    },

    nextOptionId(inj) {
      return this.nextSequentialId('opt', inj?.options || []);
    },

    addTeam() {
      const id = this.nextTeamId();
      this.form.participant_teams.push({
        _key: scenarioKey(),
        _editingId: id,
        id,
        label: '',
      });
      this.activePanel = 'teams';
    },

    removeTeam(i) {
      const [removed] = this.form.participant_teams.splice(i, 1);
      if (removed?.id) {
        this.form.injects.forEach(inj => {
          inj.target_teams = (inj.target_teams || []).filter(t => t !== removed.id);
        });
      }
    },

    commitTeamId(team) {
      const previous = team._editingId || team.id;
      const next = normalizeId(team.id, this.nextTeamId());
      team.id = next;
      team._editingId = next;
      if (previous && previous !== next) {
        this.form.injects.forEach(inj => {
          inj.target_teams = (inj.target_teams || []).map(t => t === previous ? next : t);
        });
      }
    },

    addInject() {
      const inj = makeInject(this.nextInjectId());
      inj.sequence_order = this.form.injects.length + 1;
      this.form.injects.push(inj);
      if (!this.form.start_inject_id) this.form.start_inject_id = inj.id;
      this.setActiveInject(inj);
    },

    removeActiveInject() {
      const index = this.activeIndex;
      if (index >= 0) this.removeInject(index);
    },

    removeInject(i) {
      const [removed] = this.form.injects.splice(i, 1);
      if (removed?.id) {
        this.form.injects.forEach(inj => {
          if (inj.next_inject_id === removed.id) inj.next_inject_id = null;
          (inj.options || []).forEach(opt => {
            if (opt.next_inject_id === removed.id) opt.next_inject_id = null;
          });
        });
      }
      if (this.form.start_inject_id === removed?.id) {
        this.form.start_inject_id = this.form.injects[0]?.id || '';
      }
      const next = this.form.injects[Math.min(i, this.form.injects.length - 1)];
      this.activeInjectKey = next?._key || null;
      if (!next) this.activePanel = 'scenario';
    },

    moveActiveInject(direction) {
      const from = this.activeIndex;
      const to = from + direction;
      if (from < 0 || to < 0 || to >= this.form.injects.length) return;
      const [inj] = this.form.injects.splice(from, 1);
      this.form.injects.splice(to, 0, inj);
    },

    commitInjectId(inj) {
      const previous = inj._editingId || inj.id;
      const next = normalizeId(inj.id, this.nextInjectId());
      inj.id = next;
      inj._editingId = next;
      if (previous && previous !== next) {
        if (this.form.start_inject_id === previous) this.form.start_inject_id = next;
        this.form.injects.forEach(node => {
          if (node.next_inject_id === previous) node.next_inject_id = next;
          (node.options || []).forEach(opt => {
            if (opt.next_inject_id === previous) opt.next_inject_id = next;
          });
        });
      }
    },

    addOption(inj) {
      inj.options = inj.options || [];
      inj.next_inject_id = null;
      inj.options.push(makeOption(this.nextOptionId(inj)));
    },

    addExpectedAction(inj) {
      inj.expected_actions = inj.expected_actions || [];
      inj._actionKeys = inj._actionKeys || [];
      inj.expected_actions.push('');
      inj._actionKeys.push(scenarioKey());
    },

    removeExpectedAction(inj, index) {
      inj.expected_actions.splice(index, 1);
      inj._actionKeys.splice(index, 1);
    },

    injectTargetsTeam(inj, teamId) {
      return !!teamId && (inj.target_teams || []).includes(teamId);
    },

    toggleInjectTeam(inj, teamId, checked) {
      if (!teamId) return;
      inj.target_teams = inj.target_teams || [];
      if (checked && !inj.target_teams.includes(teamId)) inj.target_teams.push(teamId);
      if (!checked) inj.target_teams = inj.target_teams.filter(t => t !== teamId);
    },

    focusIssue(issue) {
      if (issue.target === 'scenario') this.activePanel = 'scenario';
      if (issue.target === 'teams') this.activePanel = 'teams';
      if (issue.target === 'injects') {
        this.activePanel = 'injects';
        if (issue.key) this.activeInjectKey = issue.key;
      }
    },

    detectCycle() {
      const byId = Object.fromEntries(this.form.injects.filter(inj => inj.id).map(inj => [inj.id, inj]));
      const visited = new Set();
      const stack = new Set();
      const walk = (id) => {
        if (!id || !byId[id]) return null;
        if (stack.has(id)) return id;
        if (visited.has(id)) return null;
        visited.add(id);
        stack.add(id);
        for (const nextId of this.nextIdsForInject(byId[id])) {
          const cycle = walk(nextId);
          if (cycle) return cycle;
        }
        stack.delete(id);
        return null;
      };
      return walk(this.form.start_inject_id);
    },

    nextIdsForInject(inj) {
      const ids = (inj.options || []).map(opt => opt.next_inject_id).filter(Boolean);
      if (inj.next_inject_id) ids.push(inj.next_inject_id);
      return ids;
    },

    buildPayload() {
      const injects = this.form.injects.map((inj, index) => ({
        id: normalizeId(inj.id, `inject_${String(index + 1).padStart(2, '0')}`),
        title: inj.title,
        content: inj.content,
        target_teams: (inj.target_teams || []).filter(Boolean),
        sequence_order: index + 1,
        next_inject_id: (inj.options || []).length ? null : (inj.next_inject_id || null),
        options: (inj.options || []).map((opt, optIndex) => ({
          id: normalizeId(opt.id, `opt_${String(optIndex + 1).padStart(2, '0')}`),
          label: opt.label,
          next_inject_id: opt.next_inject_id || null,
        })),
        free_text_response: inj.free_text_response !== false,
        triggers_communications: inj.triggers_communications || [],
        expected_actions: (inj.expected_actions || []).map(a => a.trim()).filter(Boolean),
      }));
      const validIds = new Set(injects.map(i => i.id).filter(Boolean));
      const start_inject_id = validIds.has(this.form.start_inject_id)
        ? this.form.start_inject_id
        : (injects.find(i => i.id)?.id || '');
      const duration = Number(this.form.metadata?.estimated_duration_minutes || 0);
      return {
        ...this.form,
        metadata: {
          author: this.form.metadata?.author || null,
          estimated_duration_minutes: duration > 0 ? duration : null,
        },
        participant_teams: this.form.participant_teams.map(team => ({
          id: normalizeId(team.id, this.nextTeamId()),
          label: team.label || team.id,
        })),
        start_inject_id,
        injects,
        tags: this.tagsList,
      };
    },

    async save() {
      this.error = '';
      this.success = '';
      if (this.blockingIssues.length) {
        this.error = this.blockingIssues[0].text;
        this.focusIssue(this.blockingIssues[0]);
        return;
      }
      this.saving = true;
      const payload = this.buildPayload();
      const url = this.scenarioId ? '/scenarios/' + this.scenarioId : '/scenarios';
      const method = this.scenarioId ? 'PUT' : 'POST';
      const r = await apiFetch(url, { method, body: JSON.stringify(payload) });
      this.saving = false;
      if (r && r.ok) {
        const data = await r.json();
        if (!this.scenarioId) {
          window.location.href = '/scenarios/' + data.id;
        } else {
          this.success = 'Saved successfully.';
          this.applyDefinition(data.definition);
          this.activePanel = 'scenario';
        }
      } else if (r) {
        const data = await r.json();
        this.error = data.detail || 'Save failed.';
      } else {
        this.error = 'Save failed.';
      }
    },
  }));
});
