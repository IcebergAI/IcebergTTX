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

    async init() {
      await this.loadUsers();
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
