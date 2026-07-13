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
  return ['/login', '/register', '/forgot-password', '/reset-password', '/accept-invite'].includes(window.location.pathname);
}

function clearAuthState() {
  localStorage.removeItem('dt_token');
  localStorage.removeItem('dt_view_role');
  localStorage.removeItem('dt_view_team');
  document.cookie = 'access_token=; Max-Age=0; path=/';
  document.cookie = 'dt_view_role=; Max-Age=0; path=/';
  document.cookie = 'dt_view_team=; Max-Age=0; path=/';
  document.cookie = 'dt_current_exercise=; Max-Age=0; path=/';
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

function getCookie(name) {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : null;
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
  // Exercise clock (#116): whole seconds → H:MM:SS (drops the hours field under an hour).
  fmtClock(seconds) {
    if (seconds == null || isNaN(seconds)) return '—';
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const mm = String(m).padStart(2, '0'), ss = String(sec).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  },
  // Scheduled-release countdown (#116): remaining seconds → M:SS, or "due" once elapsed.
  fmtCountdown(seconds) {
    if (seconds == null || isNaN(seconds)) return '—';
    if (seconds <= 0) return 'due';
    const s = Math.floor(seconds);
    const m = Math.floor(s / 60), sec = s % 60;
    return `${m}:${String(sec).padStart(2, '0')}`;
  },
  // Tint modifier layered onto pills and team labels. Keep the established
  // four pixel-identical; hash every other scenario-defined id into the shared
  // accessible palette so its scent is stable across pages and sessions.
  teamColor(id) {
    const map = {
      it_ops: 'team-itops',
      legal:  'team-legal',
      exec:   'team-exec',
      comms:  'team-comms',
    };
    if (map[id]) return map[id];
    if (!id) return '';
    let hash = 2166136261;
    for (const character of String(id)) {
      hash ^= character.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return `team-scent-${(hash >>> 0) % 12}`;
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

const dialogHelpers = {
  dialogTrigger: null,
  dialogBackground: [],
  focusDialog(ref) {
    this.dialogTrigger = document.activeElement;
    document.documentElement.classList.add('dialog-open');
    this.$nextTick(() => {
      const target = this.$refs[ref];
      const dialog = target?.closest('[role="dialog"]');
      const overlay = dialog?.parentElement;
      this.dialogBackground = [];
      let branch = overlay;
      while (branch?.parentElement && branch.parentElement !== document.body) {
        for (const sibling of branch.parentElement.children) {
          if (sibling === branch || sibling.contains(branch) || sibling.hasAttribute('inert')) {
            continue;
          }
          sibling.setAttribute('inert', '');
          this.dialogBackground.push(sibling);
        }
        branch = branch.parentElement;
      }
      target?.focus();
    });
  },
  restoreDialogFocus() {
    document.documentElement.classList.remove('dialog-open');
    for (const node of this.dialogBackground) node.removeAttribute('inert');
    this.dialogBackground = [];
    this.$nextTick(() => this.dialogTrigger?.focus?.());
  },
  trapDialog(event, ref) {
    const dialog = this.$refs[ref];
    if (!dialog) return;
    const controls = [...dialog.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )].filter(node => node.getClientRects().length);
    if (!controls.length) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  },
};

window.DT = {
  getToken,
  isAuthPage,
  clearAuthState,
  apiFetch,
  readJson,
  setPreferenceCookie,
  getCookie,
  resolveTheme,
  applyTheme,
  connectExerciseWs,
  uiHelpers,
  dialogHelpers,
};

// ── Rail nav component ───────────────────────────────────────────────────

document.addEventListener('alpine:init', () => {
  Alpine.data('sidebarNav', () => ({
    ...uiHelpers,
    user: null,
    // Every active exercise the user can see (#96) — more than one may run at a time.
    liveExercises: [],
    // The user's explicit pick, persisted across pages. Validated on read, never trusted.
    selectedId: null,
    scenarioCount: null,
    unread: 0,
    currentPath: window.location.pathname,
    mobileNavOpen: false,

    async init() {
      document.addEventListener('dt:soft-navigated', (event) => {
        this.currentPath = event.detail.path;
        this.refreshUnread();
      });
      // sidebarNav lives outside #app-main, so the soft-nav engine never re-runs init().
      // Pages that change an exercise's lifecycle announce it, and only then do we refetch
      // — navigating alone is not a lifecycle change, and the page is already fetching.
      document.addEventListener('dt:exercises-changed', async () => {
        await this.refreshExercises();
        this.refreshUnread();
      });
      // The inbox announces every comm it receives or reads, so the rail badge
      // stays honest without polling.
      document.addEventListener('dt:comms-changed', () => {
        this.refreshUnread();
      });
      if (isAuthPage()) return;
      // No localStorage-token gate: shell pages are only served to an authenticated
      // request (ui.py redirects anonymous visitors to /login server-side), so the
      // httpOnly cookie is always present here — this also covers cookie-only
      // sessions (SSO, or right after a password change dropped the stale token).
      this.selectedId = Number(getCookie('dt_current_exercise')) || null;
      const [mr] = await Promise.all([
        apiFetch('/auth/me'),
        this.refreshExercises().then(() => this.refreshUnread()),
      ]);
      this.user = await readJson(mr);
      // Temp-password gate (#66): an admin-reset user must set their own password
      // before doing anything else. Enforced UI-side — hold them on /settings.
      if (this.user?.must_change_password && window.location.pathname !== '/settings') {
        window.location.href = '/settings';
        return;
      }
      if (this.user?.role === 'facilitator') {
        const sr = await apiFetch('/scenarios');
        const scenarios = await readJson(sr, []);
        this.scenarioCount = scenarios.length > 0 ? scenarios.length : null;
      } else {
        this.scenarioCount = null;
      }
    },

    async refreshExercises() {
      const er = await apiFetch('/exercises');
      if (!er || !er.ok) return;
      const exs = await readJson(er, []);
      this.liveExercises = exs.filter(e => e.state === 'active');
    },

    // Unread comms for the exercise the rail is currently pointed at. Scoped
    // server-side to what this viewer may actually read.
    async refreshUnread() {
      const id = this.currentExerciseId;
      if (!id) {
        this.unread = 0;
        return;
      }
      const r = await apiFetch('/exercises/' + id + '/communications/unread-count');
      if (!r || !r.ok) return;
      const body = await readJson(r, { unread: 0 });
      this.unread = body.unread || 0;
    },

    selectExercise(id) {
      this.selectedId = id;
      setPreferenceCookie('dt_current_exercise', id);
      this.refreshUnread();
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

    // The single validation point for the persisted selection: a stale id (the exercise
    // completed, or the user lost access, so it isn't in liveExercises) simply isn't found
    // and we fall back to the first — the list is deterministically ordered server-side,
    // so "first" is stable. Links can therefore never point at a dead exercise.
    get currentExercise() {
      return this.liveExercises.find(e => e.id === this.selectedId) || this.liveExercises[0] || null;
    },

    get currentExerciseId() {
      return this.currentExercise ? this.currentExercise.id : null;
    },

    get currentExerciseTitle() {
      return this.currentExercise ? this.currentExercise.title : '';
    },

    get hasLiveExercises() {
      return this.liveExercises.length > 0;
    },

    get hasMultipleLive() {
      return this.liveExercises.length > 1;
    },

    get liveCountLabel() {
      return this.liveExercises.length > 1 ? this.liveExercises.length + ' live' : 'live';
    },

    get commsHref() {
      return this.currentExercise ? '/exercises/' + this.currentExercise.id + '/communications' : '/communications';
    },

    get liveHref() {
      if (!this.currentExercise) return '/exercises';
      return '/exercises/' + this.currentExercise.id + (this.isFacilitator ? '/facilitate' : '/participate');
    },

    get liveSub() {
      return this.currentExercise ? 'Live · EX-' + this.padId(this.currentExercise.id, 3) : '';
    },

    get exercisesActive() {
      return this.currentPath.startsWith('/exercises') &&
        !this.currentPath.includes('/communications');
    },

    get hasUnread() {
      return this.unread > 0;
    },

    // Capped so a long-running exercise can't widen the rail.
    get unreadLabel() {
      return this.unread > 99 ? '99+' : String(this.unread);
    },

    get crumbLabel() {
      const p = this.currentPath;
      if (p.includes('/communications')) return 'Communications';
      if (p === '/' || p.startsWith('/dashboard')) return 'Command center';
      if (p.startsWith('/scenarios')) return 'Scenarios';
      if (p.startsWith('/exercises')) return 'Exercises';
      if (p.startsWith('/admin/users')) return 'Users';
      if (p.startsWith('/admin/audit')) return 'Audit log';
      if (p.startsWith('/admin/proxy')) return 'Outbound proxy';
      if (p.startsWith('/admin/settings')) return 'General settings';
      if (p.startsWith('/admin/llm')) return 'AI provider';
      if (p.startsWith('/admin/oidc')) return 'Single sign-on';
      if (p.startsWith('/help')) return 'Help';
      if (p.startsWith('/settings')) return 'Settings';
      const seg = p.replace(/^\/+/, '').split('/')[0] || 'Home';
      return seg.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    },

    // What the slim topbar carries instead of a static crumb: which exercise the
    // page you're looking at actually belongs to.
    get crumbContext() {
      if (!this.currentExercise) return '';
      return this.currentExerciseTitle + ' · EX-' + this.padId(this.currentExercise.id, 3);
    },

    isActive(path) {
      return this.currentPath === path || this.currentPath.startsWith(path + '/');
    },

    toggleMobileNav() {
      this.mobileNavOpen = !this.mobileNavOpen;
      if (this.mobileNavOpen) {
        document.querySelector('[data-app-content]')?.setAttribute('inert', '');
        document.documentElement.classList.add('dialog-open');
        this.$nextTick(() => this.$refs.mobileNavClose?.focus());
      } else {
        this.restoreMobileNavContext();
      }
    },

    closeMobileNav() {
      if (!this.mobileNavOpen) return;
      this.mobileNavOpen = false;
      this.restoreMobileNavContext();
    },

    restoreMobileNavContext() {
      document.querySelector('[data-app-content]')?.removeAttribute('inert');
      document.documentElement.classList.remove('dialog-open');
      this.$nextTick(() => this.$refs.mobileNavToggle?.focus());
    },

    trapMobileNav(event) {
      const nav = document.getElementById('primary-navigation');
      if (!nav || !this.mobileNavOpen) return;
      const controls = [...nav.querySelectorAll(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )].filter(node => node.getClientRects().length);
      if (!controls.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
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
