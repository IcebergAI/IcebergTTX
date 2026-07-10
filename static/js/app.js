// IcebergTTX shared runtime — auth helpers, theme helpers, soft navigation, and
// the sidebarNav shell component. Loaded (defer) BEFORE the Alpine CSP build so
// the alpine:init listener below is registered when Alpine fires it. The app
// ships a strict `script-src 'self'` CSP (#77): no inline scripts anywhere, and
// the CSP build resolves directive identifiers against component scope only —
// helpers used from directives must be component members (see DT.uiHelpers).

// ── Shared auth helpers ──────────────────────────────────────────────────

function getToken() {
  return localStorage.getItem('dt_token');
}

function isAuthPage() {
  return ['/login', '/register'].includes(window.location.pathname);
}

function clearAuthState() {
  localStorage.removeItem('dt_token');
  localStorage.removeItem('dt_view_role');
  localStorage.removeItem('dt_view_team');
  document.cookie = 'access_token=; Max-Age=0; path=/';
  document.cookie = 'dt_view_role=; Max-Age=0; path=/';
  document.cookie = 'dt_view_team=; Max-Age=0; path=/';
}

async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const apiPath = path.startsWith('/api/') ? path : '/api' + path;
  let resp;
  try {
    resp = await fetch(apiPath, { ...options, headers });
  } catch {
    return null;
  }
  if (resp.status === 401) {
    clearAuthState();
    if (!isAuthPage()) window.location.href = '/login';
    return null;
  }
  return resp;
}

async function readJson(resp, fallback = null) {
  if (!resp || !resp.ok) return fallback;
  try {
    return await resp.json();
  } catch {
    return fallback;
  }
}

function setPreferenceCookie(name, value) {
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=31536000; samesite=lax`;
}

function resolveTheme(theme) {
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  return theme === 'system' ? (prefersDark ? 'dark' : 'light') : theme;
}

function applyTheme(theme) {
  localStorage.setItem('dt_theme', theme);
  const resolvedTheme = resolveTheme(theme);
  const resolvedBg = resolvedTheme === 'dark' ? 'oklch(0.185 0.02 256)' : 'oklch(0.984 0.006 240)';
  document.documentElement.dataset.theme = resolvedTheme;
  document.documentElement.style.backgroundColor = resolvedBg;
  document.body.style.backgroundColor = resolvedBg;
  document.documentElement.style.colorScheme = resolvedTheme;
  document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', resolvedTheme);
  setPreferenceCookie('dt_theme', theme);
  setPreferenceCookie('dt_resolved_theme', resolvedTheme);
}

// ── Soft navigation ──────────────────────────────────────────────────────
// Every Alpine component is pre-registered via Alpine.data() from files loaded
// in base.html, so swapping a page is destroyTree → innerHTML → initTree. The
// old inline-script re-injection is gone (incompatible with script-src 'self').

function shouldSoftNavigate(event, anchor) {
  if (!anchor || event.defaultPrevented || anchor.hasAttribute('download')) return false;
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
  if (anchor.target && anchor.target !== '_self') return false;
  const url = new URL(anchor.href, window.location.href);
  if (!['http:', 'https:'].includes(url.protocol)) return false;
  if (url.origin !== window.location.origin) return false;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/static/')) return false;
  if (url.pathname === window.location.pathname && url.search === window.location.search) return false;
  return true;
}

async function swapMainContent(nextDoc, nextUrl, replaceState = false) {
  const main = document.getElementById('app-main');
  const nextMain = nextDoc.getElementById('app-main') || nextDoc.querySelector('main');
  if (!main || !nextMain) return false;

  window.Alpine?.destroyTree?.(main);
  main.innerHTML = nextMain.innerHTML;
  document.title = nextDoc.title || document.title;
  window.Alpine?.initTree?.(main);

  if (replaceState) {
    window.history.replaceState({ icebergTtxSoftNavigation: true }, '', nextUrl.href);
  } else {
    window.history.pushState({ icebergTtxSoftNavigation: true }, '', nextUrl.href);
  }
  document.dispatchEvent(new CustomEvent('dt:soft-navigated', {
    detail: { path: nextUrl.pathname, url: nextUrl.href },
  }));
  window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
  return true;
}

async function softNavigate(href, options = {}) {
  const url = new URL(href, window.location.href);
  const main = document.getElementById('app-main');
  if (!main) {
    window.location.href = url.href;
    return;
  }

  let html;
  let finalUrl = url;
  try {
    const resp = await fetch(url.href, {
      credentials: 'same-origin',
      headers: { Accept: 'text/html, application/xhtml+xml' },
    });
    if (!resp.ok || !resp.headers.get('content-type')?.includes('text/html')) {
      throw new Error('Soft navigation received a non-HTML response.');
    }
    html = await resp.text();
    finalUrl = new URL(resp.url || url.href);
  } catch {
    window.location.href = url.href;
    return;
  }

  const nextDoc = new DOMParser().parseFromString(html, 'text/html');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const update = async () => {
    main.classList.remove('is-leaving');
    const swapped = await swapMainContent(nextDoc, finalUrl, options.replaceState);
    if (!swapped) throw new Error('Soft navigation response did not include a main region.');
  };

  try {
    if (document.startViewTransition && !reducedMotion) {
      await document.startViewTransition(update).finished;
    } else {
      main.classList.add('is-leaving');
      main.classList.add('is-entering');
      await update();
      requestAnimationFrame(() => main.classList.remove('is-entering'));
    }
  } catch {
    window.location.href = finalUrl.href;
  }
}

document.addEventListener('click', (event) => {
  const anchor = event.target.closest?.('a[href]');
  if (!shouldSoftNavigate(event, anchor)) return;
  event.preventDefault();
  softNavigate(anchor.href);
});
window.addEventListener('popstate', () => {
  softNavigate(window.location.href, { replaceState: true });
});

// ── Format helpers (component mixin) ─────────────────────────────────────
// Spread `...DT.uiHelpers` into any Alpine.data factory whose template calls
// these from directive expressions — the CSP build cannot reach globals there.

const uiHelpers = {
  fmtElapsed(isoStr) {
    if (!isoStr) return '—';
    const s = Math.max(0, Math.floor((Date.now() - new Date(isoStr)) / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
  },
  fmtAgo(isoStr) {
    if (!isoStr) return '—';
    const diff = (Date.now() - new Date(isoStr)) / 1000;
    if (diff < 60)    return `${Math.floor(diff)}s ago`;
    if (diff < 3600)  return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  },
  fmtTime(isoStr) {
    if (!isoStr) return '—';
    return new Date(isoStr).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  },
  teamColor(id) {
    const map = {
      it_ops: 'bg-sky-100/70 text-sky-800 ring-sky-200',
      legal:  'bg-violet-100/70 text-violet-800 ring-violet-200',
      exec:   'bg-amber-100/70 text-amber-900 ring-amber-200',
      comms:  'bg-emerald-100/70 text-emerald-800 ring-emerald-200',
    };
    return map[id] || 'pill mono';
  },
  padId(id, width = 2) {
    return String(id).padStart(width, '0');
  },
  flowIndent(depth) {
    return 'margin-left:' + Math.min(depth * 14, 42) + 'px';
  },
  stateBadgeClass(state) {
    const map = {
      active:    'bg-st-active',
      paused:    'bg-st-paused',
      draft:     'bg-st-draft',
      completed: 'bg-st-completed',
    };
    return map[state] || '';
  },
  stateDotClass(state) {
    const map = {
      paused:    'dot--warn',
      draft:     'dot--muted',
      completed: 'dot--accent',
    };
    return map[state] || 'dot--muted';
  },
};

// ── Shared exercise WebSocket (#68) ──────────────────────────────────────
// Auth rides on the httpOnly access_token cookie the browser sends on the
// upgrade — no token in the URL, and no localStorage gate (SSO sessions have
// the cookie but never a dt_token). Manages the component's ws / wsConnected /
// pingInterval / reconnectTimeout fields so each page's destroy() teardown
// works unchanged. Auth-refused closes (4001 invalid/expired/revoked token,
// 4003 origin/access denied) are terminal: retrying can never succeed, and
// each retry would emit a server-side audit event — so no reconnect loop.
const WS_NO_RETRY_CODES = [4001, 4003];

function connectExerciseWs(exerciseId, component, { viewParams = false, onMessage = null } = {}) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const params = new URLSearchParams();
  if (viewParams) {
    const viewRole = localStorage.getItem('dt_view_role');
    const viewTeam = localStorage.getItem('dt_view_team');
    if (viewRole) params.set('view_role', viewRole);
    if (viewTeam) params.set('view_team', viewTeam);
  }
  const qs = params.toString();
  const ws = new WebSocket(`${proto}://${location.host}/ws/exercises/${exerciseId}${qs ? '?' + qs : ''}`);
  component.ws = ws;

  ws.onopen = () => {
    component.wsConnected = true;
    component.pingInterval = setInterval(() => {
      if (component.ws && component.ws.readyState === WebSocket.OPEN)
        component.ws.send(JSON.stringify({ type: 'ping' }));
    }, 30000);
  };

  ws.onclose = (ev) => {
    component.wsConnected = false;
    clearInterval(component.pingInterval);
    component.pingInterval = null;
    if (component.destroyed || WS_NO_RETRY_CODES.includes(ev.code)) return;
    component.reconnectTimeout = setTimeout(
      () => connectExerciseWs(exerciseId, component, { viewParams, onMessage }),
      3000,
    );
  };

  if (onMessage) {
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      onMessage(msg);
    };
  }
}

window.DT = {
  getToken,
  isAuthPage,
  clearAuthState,
  apiFetch,
  readJson,
  setPreferenceCookie,
  resolveTheme,
  applyTheme,
  connectExerciseWs,
  uiHelpers,
};

// ── Rail nav component ───────────────────────────────────────────────────

document.addEventListener('alpine:init', () => {
  Alpine.data('sidebarNav', () => ({
    ...uiHelpers,
    user: null,
    liveExercise: null,
    scenarioCount: null,
    currentPath: window.location.pathname,

    async init() {
      document.addEventListener('dt:soft-navigated', (event) => {
        this.currentPath = event.detail.path;
      });
      if (isAuthPage()) return;
      const token = getToken();
      if (!token) return;
      const [mr, er] = await Promise.all([
        apiFetch('/auth/me'),
        apiFetch('/exercises'),
      ]);
      this.user = await readJson(mr);
      if (er && er.ok) {
        const exs = await readJson(er, []);
        this.liveExercise = exs.find(e => e.state === 'active') || null;
      }
      if (this.user?.role === 'facilitator') {
        const sr = await apiFetch('/scenarios');
        const scenarios = await readJson(sr, []);
        this.scenarioCount = scenarios.length > 0 ? scenarios.length : null;
      } else {
        this.scenarioCount = null;
      }
    },

    get initials() {
      const name = this.user?.display_name || this.user?.email || '';
      return name.split(/[\s@]/).filter(Boolean).map(x => x[0]).join('').slice(0, 2).toUpperCase();
    },

    get isFacilitator() {
      return !!this.user && this.user.role === 'facilitator';
    },

    get isAdmin() {
      return !!this.user && !!this.user.is_admin;
    },

    get displayName() {
      return (this.user && (this.user.display_name || this.user.email)) || '';
    },

    get roleLabel() {
      return this.user ? this.user.role : '';
    },

    get hasPreview() {
      return !!this.user && !!this.user.actual_role && this.user.actual_role !== this.user.role;
    },

    get previewLabel() {
      return this.user ? 'Previewing as ' + this.user.role : '';
    },

    get commsHref() {
      return this.liveExercise ? '/exercises/' + this.liveExercise.id + '/communications' : '/communications';
    },

    get liveHref() {
      if (!this.liveExercise) return '/exercises';
      return '/exercises/' + this.liveExercise.id + (this.isFacilitator ? '/facilitate' : '/participate');
    },

    get liveSub() {
      return this.liveExercise ? 'Live · EX-' + this.padId(this.liveExercise.id, 2) : '';
    },

    get exercisesActive() {
      return this.currentPath.startsWith('/exercises') &&
        !this.currentPath.includes('/communications');
    },

    get crumbLabel() {
      const p = this.currentPath;
      if (p.includes('/communications')) return 'Communications';
      if (p === '/' || p.startsWith('/dashboard')) return 'Command center';
      if (p.startsWith('/scenarios')) return 'Scenarios';
      if (p.startsWith('/exercises')) return 'Exercises';
      if (p.startsWith('/admin/audit')) return 'Audit log';
      if (p.startsWith('/admin/proxy')) return 'Outbound proxy';
      if (p.startsWith('/help')) return 'Help';
      if (p.startsWith('/settings')) return 'Settings';
      const seg = p.replace(/^\/+/, '').split('/')[0] || 'Home';
      return seg.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    },

    isActive(path) {
      return this.currentPath === path || this.currentPath.startsWith(path + '/');
    },

    async logout() {
      try {
        await fetch('/api/auth/logout', { method: 'POST' });
      } catch {}
      clearAuthState();
      window.location.href = '/login';
    },
  }));
});
