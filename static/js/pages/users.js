// Admin users page component — registered via Alpine.data (strict CSP, #77).
// Lists accounts and drives the admin password reset (#66).

document.addEventListener('alpine:init', () => {
  Alpine.data('adminUsers', () => ({
    ...DT.dialogHelpers,
    users: [],
    loading: true,
    loadError: '',
    // Reset dialog state — resetTarget is the user being reset (null = closed).
    resetTarget: null,
    resetPassword: '',
    resetMustChange: true,
    resetError: '',
    resetting: false,
    message: '',
    // Invite dialog state (#117).
    inviteOpen: false,
    inviteEmail: '',
    inviteTeam: '',
    inviteExerciseId: '',
    inviteError: '',
    inviteSent: false,
    inviting: false,
    exercises: [],

    async init() {
      await this.loadUsers();
      const er = await apiFetch('/exercises');
      if (er && er.ok) this.exercises = await readJson(er, []);
    },

    async loadUsers() {
      this.loading = true;
      this.loadError = '';
      const resp = await apiFetch('/users');
      if (resp && resp.ok) {
        this.users = await readJson(resp, []);
      } else {
        this.loadError = 'Could not load users.';
      }
      this.loading = false;
    },

    openInvite() {
      this.inviteOpen = true;
      this.inviteEmail = '';
      this.inviteTeam = '';
      this.inviteExerciseId = '';
      this.inviteError = '';
      this.inviteSent = false;
    },

    closeInvite() {
      this.inviteOpen = false;
    },

    async submitInvite() {
      this.inviteError = '';
      this.inviting = true;
      const body = { email: this.inviteEmail };
      if (this.inviteTeam) body.team = this.inviteTeam;
      if (this.inviteExerciseId) body.exercise_id = parseInt(this.inviteExerciseId, 10);
      const resp = await apiFetch('/users/invite', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      this.inviting = false;
      if (resp && resp.ok) {
        this.inviteSent = true;
      } else {
        const data = await readJson(resp, null);
        const detail = data && data.detail;
        this.inviteError = (typeof detail === 'string' ? detail : null) || 'Could not send invite.';
      }
    },

    openReset(user) {
      this.resetTarget = user;
      this.resetPassword = '';
      this.resetMustChange = true;
      this.resetError = '';
      this.focusDialog('resetPassword');
    },

    closeReset() {
      this.resetTarget = null;
      this.restoreDialogFocus();
    },

    get resetTargetLabel() {
      const u = this.resetTarget;
      if (!u) return '';
      return u.display_name ? `${u.display_name} (${u.email})` : u.email;
    },

    async submitReset() {
      if (!this.resetTarget) return;
      this.resetError = '';
      if (this.resetPassword.length < 12) {
        this.resetError = 'Password must be at least 12 characters.';
        return;
      }
      this.resetting = true;
      const resp = await apiFetch(`/users/${this.resetTarget.id}/reset-password`, {
        method: 'POST',
        body: JSON.stringify({
          password: this.resetPassword,
          must_change_password: this.resetMustChange,
        }),
      });
      this.resetting = false;
      if (resp && resp.ok) {
        this.message = `Password reset for ${this.resetTargetLabel}.`;
        this.closeReset();
        await this.loadUsers();
      } else {
        const data = await readJson(resp, null);
        this.resetError = (data && data.detail) || 'Could not reset password.';
      }
    },
  }));
});
