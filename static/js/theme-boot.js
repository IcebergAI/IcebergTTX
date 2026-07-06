// Theme/FOUC bootstrap — loaded as a synchronous <script> at the top of <head>
// so it runs before first paint (strict CSP forbids inline scripts, #77).
(() => {
  const maxAge = 60 * 60 * 24 * 365;
  const readCookie = (name) => {
    const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : null;
  };
  const writeCookie = (name, value) => {
    document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${maxAge}; samesite=lax`;
  };
  const readLocal = (key) => {
    try { return localStorage.getItem(key); } catch { return null; }
  };
  const writeLocal = (key, value) => {
    try { localStorage.setItem(key, value); } catch {}
  };
  const storedTheme = readLocal('dt_theme');
  const cookieTheme = readCookie('dt_theme');
  const theme = storedTheme || (['system', 'light', 'dark'].includes(cookieTheme) ? cookieTheme : 'system');
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const resolvedTheme = theme === 'system' ? (prefersDark ? 'dark' : 'light') : theme;
  const resolvedBg = resolvedTheme === 'dark' ? 'oklch(0.185 0.02 256)' : 'oklch(0.984 0.006 240)';
  document.documentElement.dataset.theme = resolvedTheme;
  document.documentElement.style.backgroundColor = resolvedBg;
  document.documentElement.style.colorScheme = resolvedTheme;
  document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', resolvedTheme);
  document.addEventListener('DOMContentLoaded', () => {
    document.body.style.backgroundColor = resolvedBg;
  }, { once: true });
  writeCookie('dt_theme', theme);
  writeCookie('dt_resolved_theme', resolvedTheme);
  if (!storedTheme) writeLocal('dt_theme', theme);
})();
