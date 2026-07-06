// Auth page components — registered via Alpine.data (strict CSP, #77).

function formatApiError(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (typeof item === 'string') return item;
      const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : null;
      return field ? `${field}: ${item.msg}` : item.msg;
    }).filter(Boolean).join(' ');
  }
  if (detail && typeof detail === 'object') return detail.msg || JSON.stringify(detail);
  return '';
}

document.addEventListener('alpine:init', () => {
  Alpine.data('loginForm', () => ({
    email: '', password: '', loading: false, error: '',
    async submit() {
      this.loading = true; this.error = '';
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: this.email, password: this.password }),
      });
      this.loading = false;
      if (resp.ok) {
        const data = await resp.json();
        localStorage.setItem('dt_token', data.access_token);
        window.location.href = '/dashboard';
      } else {
        this.error = 'Invalid email or password.';
      }
    },
  }));

  Alpine.data('registerForm', () => ({
    email: '', display_name: '', password: '', team: '',
    loading: false, error: '',
    async submit() {
      this.loading = true; this.error = '';
      const body = { email: this.email, display_name: this.display_name, password: this.password };
      if (this.team) body.team = this.team;
      const resp = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      this.loading = false;
      if (resp.ok) {
        window.location.href = '/login';
      } else {
        const data = await resp.json();
        this.error = formatApiError(data.detail) || 'Registration failed.';
      }
    },
  }));
});
