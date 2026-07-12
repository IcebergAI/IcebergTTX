// Settings page component — registered via Alpine.data (strict CSP, #77).

document.addEventListener('alpine:init', () => {
  Alpine.data('settingsPage', () => ({
    me: null,
    displayName: '',
    savingProfile: false,
    profileMessage: '',
    newPassword: '',
    confirmPassword: '',
    savingPassword: false,
    passwordMessage: '',
    theme: localStorage.getItem('dt_theme') || 'system',
    viewRole: localStorage.getItem('dt_view_role') || 'facilitator',
    viewTeam: localStorage.getItem('dt_view_team') || 'it_ops',
    previewTeams: [],
    samples: [],
    loadingSamples: true,
    sampleMessage: '',
    // Client-only: which sub-nav section is showing. Nothing persists it — a
    // reload lands you back on Account, which is where the password gate lives.
    activeSection: 'account',

    sections: [
      { id: 'account', label: 'Account' },
      { id: 'appearance', label: 'Appearance' },
      { id: 'role', label: 'Role preview' },
      { id: 'samples', label: 'Sample data' },
    ],

    // Role preview and sample data are facilitator-only, so the sub-nav has to
    // hide them too — not just the panes.
    get visibleSections() {
      return this.canSwitch
        ? this.sections
        : this.sections.filter(s => s.id === 'account' || s.id === 'appearance');
    },

    setSection(id) {
      // The temp-password gate (#66) must not be navigable away from.
      if (this.mustChangePassword) return;
      this.activeSection = id;
    },

    isSection(id) { return this.activeSection === id; },

    get canSwitch() { return !!this.me?.can_switch_roles; },
    // Admin set a temporary password (#66) — the user is held on this page until
    // they set their own. app.js redirects them here.
    get mustChangePassword() { return !!this.me?.must_change_password; },
    get previewTeamOptions() {
      const teams = new Map();
      const addTeam = (team) => {
        const id = typeof team === 'string' ? team : team?.id;
        if (!id) return;
        teams.set(id, {
          id,
          label: typeof team === 'string' ? team : (team.label || id),
        });
      };
      for (const team of this.previewTeams) addTeam(team);
      addTeam(this.me?.team);
      addTeam(this.viewTeam);
      if (teams.size === 0) {
        [
          { id: 'it_ops', label: 'IT Operations' },
          { id: 'legal', label: 'Legal' },
          { id: 'exec', label: 'Executive' },
          { id: 'comms', label: 'Communications' },
        ].forEach(addTeam);
      }
      return [...teams.values()];
    },

    async init() {
      const meResp = await apiFetch('/auth/me');
      this.me = await readJson(meResp);
      this.displayName = this.me?.display_name || '';
      if (this.me?.role) this.viewRole = this.me.role;
      if (this.me?.actual_role && this.me.actual_role !== this.me.role && this.me?.team) {
        this.viewTeam = this.me.team;
      }
      if (this.canSwitch) await Promise.all([this.loadSamples(), this.loadPreviewTeams()]);
    },

    async saveProfile() {
      this.savingProfile = true;
      this.profileMessage = '';
      const resp = await apiFetch('/auth/me', {
        method: 'PUT',
        body: JSON.stringify({ display_name: this.displayName }),
      });
      this.savingProfile = false;
      this.profileMessage = resp && resp.ok ? 'Saved.' : 'Could not save name.';
    },

    async changePassword() {
      this.passwordMessage = '';
      if (this.newPassword.length < 12) {
        this.passwordMessage = 'Password must be at least 12 characters.';
        return;
      }
      if (this.newPassword !== this.confirmPassword) {
        this.passwordMessage = 'Passwords do not match.';
        return;
      }
      this.savingPassword = true;
      const resp = await apiFetch('/auth/me', {
        method: 'PUT',
        body: JSON.stringify({ password: this.newPassword }),
      });
      this.savingPassword = false;
      if (resp && resp.ok) {
        this.newPassword = '';
        this.confirmPassword = '';
        // The change revoked our old bearer token, but update_me re-issued a fresh
        // httpOnly cookie. Drop the now-stale localStorage token so apiFetch falls
        // back to that cookie (never expose the token to JS) instead of 401ing.
        localStorage.removeItem('dt_token');
        const wasForced = this.mustChangePassword;
        if (this.me) this.me.must_change_password = false;
        this.passwordMessage = 'Password changed.';
        // A forced (temp-password) change unblocks the app — reload so the shell
        // re-bootstraps from the fresh cookie and the /settings gate lifts.
        window.location.href = wasForced ? '/dashboard' : '/settings';
      } else {
        this.passwordMessage = 'Could not change password.';
      }
    },

    setTheme(value) {
      this.theme = value;
      applyTheme(this.theme);
    },

    setPreviewRole(role) {
      this.viewRole = role;
      localStorage.setItem('dt_view_role', role);
      DT.setPreferenceCookie('dt_view_role', role);
      this.setPreviewTeam();
      window.location.reload();
    },

    setPreviewTeam() {
      const team = (this.viewTeam || 'it_ops').trim();
      this.viewTeam = team;
      localStorage.setItem('dt_view_team', team);
      DT.setPreferenceCookie('dt_view_team', team);
    },

    teamLabel(team) {
      if (!team?.label || team.label === team.id) return team?.id || '';
      return `${team.label} (${team.id})`;
    },

    async loadPreviewTeams() {
      const resp = await apiFetch('/scenarios');
      const scenarios = await readJson(resp, []);
      const details = await Promise.all(
        scenarios.map(async (scenario) => {
          const detailResp = await apiFetch(`/scenarios/${scenario.id}`);
          return readJson(detailResp, null);
        })
      );
      const teams = [];
      const seen = new Set();
      for (const scenario of details) {
        for (const team of (scenario?.definition?.participant_teams || [])) {
          if (!team?.id || seen.has(team.id)) continue;
          seen.add(team.id);
          teams.push({ id: team.id, label: team.label || team.id });
        }
      }
      this.previewTeams = teams;
    },

    async loadSamples() {
      this.loadingSamples = true;
      const resp = await apiFetch('/settings/samples/scenarios');
      this.samples = await readJson(resp, []);
      this.loadingSamples = false;
    },

    async loadSample(sample) {
      this.sampleMessage = '';
      const resp = await apiFetch(`/settings/samples/scenarios/${sample.id}/load`, {
        method: 'POST',
      });
      const data = await readJson(resp);
      if (resp && resp.ok) {
        this.sampleMessage = data.created ? 'Scenario loaded.' : 'Scenario already exists.';
      } else {
        this.sampleMessage = 'Could not load sample.';
      }
    },

    async createDemo(sample) {
      this.sampleMessage = '';
      const resp = await apiFetch(`/settings/samples/scenarios/${sample.id}/demo-exercise`, {
        method: 'POST',
      });
      const data = await readJson(resp);
      if (resp && resp.ok) {
        this.sampleMessage = 'Demo exercise created.';
        const path = this.viewRole === 'facilitator' ? 'facilitate' : 'participate';
        window.location.href = `/exercises/${data.exercise.id}/${path}`;
      } else {
        this.sampleMessage = 'Could not create demo exercise.';
      }
    },
  }));
});
