// ─── State ────────────────────────────────────────────────────────────────────
const S = {
  token:        localStorage.getItem('tt_token'),
  refreshToken: localStorage.getItem('tt_refresh'),
  user:         JSON.parse(localStorage.getItem('tt_user')    || 'null'),
  company:      JSON.parse(localStorage.getItem('tt_company') || 'null'),
  symbol:       localStorage.getItem('tt_symbol') || '$',
};

// ─── Auth helpers ─────────────────────────────────────────────────────────────
function authGuard() {
  if (!S.token || !S.user) {
    window.location.href = '/';
    return false;
  }
  S.symbol = S.company?.currency_symbol || S.user?.currency_symbol || '$';
  return true;
}

function storeAuth(d) {
  S.token        = d.access_token;
  S.refreshToken = d.refresh_token;
  S.user         = d.user;
  localStorage.setItem('tt_token',   d.access_token);
  localStorage.setItem('tt_refresh', d.refresh_token);
  localStorage.setItem('tt_user',    JSON.stringify(d.user));
}

function doLogout() {
  ['tt_token','tt_refresh','tt_user','tt_company','tt_symbol'].forEach(k =>
    localStorage.removeItem(k));
  window.location.href = '/';
}

async function tryRefresh() {
  try {
    const res = await fetch('/api/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: S.refreshToken }),
    });
    if (!res.ok) return false;
    const d = await res.json();
    S.token = d.access_token;
    localStorage.setItem('tt_token', d.access_token);
    return true;
  } catch { return false; }
}

// ─── API helper ───────────────────────────────────────────────────────────────
async function api(method, path, body = null) {
  const isWrite = method !== 'GET';
  const headers = { 'Content-Type': 'application/json' };
  if (S.token) headers['Authorization'] = `Bearer ${S.token}`;
  const opts = { method, headers };
  if (body) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(path, opts);
    if (res.status === 401 && S.refreshToken) {
      const ok = await tryRefresh();
      if (ok) return api(method, path, body);
      doLogout(); return null;
    }
    // Safely parse JSON — server might return HTML on unexpected crashes
    // Clone BEFORE reading so we can fallback to text if JSON parse fails
    const resClone = res.clone();
    let data;
    try {
      data = await res.json();
    } catch {
      const text = await resClone.text().catch(() => '');
      const preview = text.replace(/<[^>]+>/g, ' ').trim().slice(0, 120);
      throw new Error(`Server error ${res.status}${preview ? ': ' + preview : ''}`);
    }
    if (!res.ok) {
      const detail = data.detail;
      const msg = Array.isArray(detail)
        ? detail.map(e => e.msg || JSON.stringify(e)).join('; ')
        : (typeof detail === 'string' ? detail : JSON.stringify(data));
      throw new Error(msg);
    }
    return data;
  } catch (e) {
    if (typeof toast === 'function') toast(e.message || 'Request failed', 'error');
    return null;
  }
}

// ─── Format helpers ───────────────────────────────────────────────────────────
function fmt(val, sym) {
  const n = parseFloat(val) || 0;
  return (sym || S.symbol || '$') + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtSigned(val, sym) {
  const n = parseFloat(val) || 0;
  return (n >= 0 ? '+' : '') + fmt(n, sym);
}
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function dateStr(s) {
  if (!s) return '—';
  try { return new Date(s).toLocaleDateString(undefined, { year:'numeric', month:'short', day:'numeric' }); }
  catch { return s; }
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function toast(msg, type = 'success', ms = 3500) {
  const c = document.getElementById('toast-container');
  if (!c) return;
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ─── Modal helpers ────────────────────────────────────────────────────────────
function openModal(id)  { document.getElementById(id)?.classList.add('open'); }
function closeModal(id) { document.getElementById(id)?.classList.remove('open'); }
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal-overlay')) e.target.classList.remove('open');
});

// ─── Sidebar nav data ─────────────────────────────────────────────────────────
const NAV_ITEMS = [
  { section: 'Main' },
  { label:'Dashboard',       href:'/dashboard',        page:'dashboard',
    icon:'<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>' },

  { section: 'Planning & Forecasting', gold: true },
  { label:'Budgets',         href:'/budgets',           page:'budgets',
    icon:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>' },
  { label:'Budget vs Actual',href:'/budgets#bva',       page:'bva',
    icon:'<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>' },
  { label:'Rolling Forecast',href:'/budgets#forecast',  page:'forecast',
    icon:'<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>' },
  { label:'Scenario Planning',href:'/budgets#scenarios',page:'scenarios',
    icon:'<circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/>' },

  { section: 'Accounting' },
  { label:'Chart of Accounts',href:'/accounts',         page:'accounts',
    icon:'<path d="M3 6h18M3 12h18M3 18h18"/>' },
  { label:'Journal Entries', href:'/journals',          page:'journals',
    icon:'<path d="M4 4h16v16H4z"/><path d="M8 8h8M8 12h8M8 16h4"/>' },
  { label:'General Ledger',  href:'/journals#ledger',   page:'ledger',
    icon:'<path d="M2 3h20M2 9h20M2 15h20M2 21h20M6 3v18M18 3v18"/>' },

  { section: 'Financial Reports' },
  { label:'Trial Balance',   href:'/reports',           page:'reports',
    icon:'<circle cx="12" cy="12" r="10"/><path d="M12 2v10l6 6"/>' },
  { label:'Income Statement',href:'/reports#income',    page:'income',
    icon:'<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>' },
  { label:'Balance Sheet',   href:'/reports#balance',   page:'balance',
    icon:'<path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>' },
  { label:'Cash Flow',       href:'/reports#cashflow',  page:'cashflow',
    icon:'<path d="M12 2v20M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/>' },
  { label:'AR Aging',        href:'/reports#ar-aging',  page:'ar-aging',
    icon:'<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>' },
  { label:'AP Aging',        href:'/reports#ap-aging',  page:'ap-aging',
    icon:'<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 8 14"/>' },

  { section: 'Receivables (AR)' },
  { label:'Customers',       href:'/customers',         page:'customers',
    icon:'<path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/>' },
  { label:'Invoices',        href:'/customers#invoices',page:'invoices',
    icon:'<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/>' },
  { label:'Receipts',        href:'/customers#receipts',page:'receipts',
    icon:'<polyline points="20 12 20 22 4 22 4 12"/><rect x="2" y="7" width="20" height="5"/>' },

  { section: 'Payables (AP)' },
  { label:'Suppliers',       href:'/suppliers',         page:'suppliers',
    icon:'<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v16"/>' },
  { label:'Bills',           href:'/suppliers#bills',   page:'bills',
    icon:'<path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z"/>' },
  { label:'Bill Payments',   href:'/suppliers#payments',page:'payments',
    icon:'<rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/>' },

  { section: 'Banking' },
  { label:'Bank Accounts',   href:'/banking',           page:'banking',
    icon:'<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/>' },
  { label:'Reconciliation',  href:'/reconciliation',    page:'reconciliation',
    icon:'<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>' },

  { section: 'Inventory' },
  { label:'Point of Sale',   href:'/inventory',          page:'inventory',
    icon:'<path d="M6 2L3 6v14a2 2 0 002 2h14a2 2 0 002-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 01-8 0"/>' },
  { label:'Enter Stock',     href:'/inventory#stock',    page:'stock',
    icon:'<path d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"/>' },
  { label:'Products',        href:'/inventory#inventory',page:'inventory',
    icon:'<path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>' },
  { label:'Stock Valuation', href:'/inventory#valuation',page:'valuation',
    icon:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>' },
  { label:'Low Stock Alert', href:'/inventory#lowstock', page:'lowstock',
    icon:'<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>' },

  { section: 'Payroll' },
  { label:'Employees',       href:'/payroll',           page:'payroll',
    icon:'<path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/>' },
  { label:'Payroll Runs',    href:'/payroll#runs',      page:'runs',
    icon:'<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>' },

  { section: 'Fixed Assets' },
  { label:'Asset Register',  href:'/assets',            page:'assets',
    icon:'<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v16"/>' },
  { label:'Asset Report',    href:'/assets#report',     page:'asset-report',
    icon:'<path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>' },

  { section: 'Tax' },
  { label:'Tax Periods',     href:'/reports#vat',       page:'tax',
    icon:'<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>' },

  { section: 'Audit & Compliance' },
  { label:'Audit & Anomalies', href:'/audit',             page:'audit',
    icon:'<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>' },

  { section: 'Enterprise' },
  { label:'Companies',       href:'/settings#companies',page:'companies',
    icon:'<path d="M3 21h18M9 8h1M9 12h1M9 16h1M14 8h1M14 12h1M14 16h1M5 21V5a2 2 0 012-2h10a2 2 0 012 2v16"/>' },
  { label:'API Keys',        href:'/settings#apikeys',  page:'apikeys',
    icon:'<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 11-7.778 7.778 5.5 5.5 0 017.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>' },
];

// ─── Module-level access control ─────────────────────────────────────────────
const PAGE_MODULE_MAP = {
  dashboard:'dashboard',
  budgets:'budgets', bva:'budgets', forecast:'budgets', scenarios:'budgets',
  accounts:'accounts',
  journals:'journals', ledger:'journals',
  reports:'reports', income:'reports', balance:'reports', cashflow:'reports',
  'ar-aging':'reports', 'ap-aging':'reports',
  customers:'customers', invoices:'customers', receipts:'customers',
  suppliers:'suppliers', bills:'suppliers', payments:'suppliers',
  banking:'banking', 'bank-accounts':'banking',
  reconciliation:'reconciliation',
  inventory:'inventory', stock:'inventory', valuation:'inventory', lowstock:'inventory',
  payroll:'payroll', runs:'payroll', employees:'payroll',
  assets:'assets', 'asset-report':'assets',
  tax:'tax',
  audit:'audit',
  settings:'settings', companies:'settings', apikeys:'settings',
};

function canAccess(page) {
  if (!page) return true;
  const user = S.user || {};
  if (user.role === 'admin') return true;
  let perms = user.module_permissions;
  if (!perms) return true;          // null → unrestricted
  if (typeof perms === 'string') {
    try { perms = JSON.parse(perms); } catch { return true; }
  }
  if (!Array.isArray(perms)) return true;
  const mod = PAGE_MODULE_MAP[page];
  if (!mod) return true;            // unknown page → allow
  return perms.includes(mod);
}

// ─── Nav builder (shared by renderLayout + permission refresh) ────────────────
function _buildNav(activePage) {
  let nav = '';
  let pendingSection = null;
  for (const item of NAV_ITEMS) {
    if (item.section !== undefined) {
      pendingSection = item;
    } else {
      if (!canAccess(item.page)) continue;
      if (pendingSection) {
        const style = pendingSection.gold ? 'style="color:rgba(200,150,62,.7)"' : '';
        nav += `<div class="sidebar-section" ${style}>${pendingSection.section}</div>`;
        pendingSection = null;
      }
      const active = item.page === activePage ? ' active' : '';
      nav += `<a class="nav-link${active}" href="${item.href}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${item.icon}</svg>
        ${item.label}
      </a>`;
    }
  }
  return nav;
}

// Fetch fresh user data from server and rebuild nav if permissions changed
function _refreshNavPerms(activePage) {
  if (!S.token) return;
  fetch('/api/auth/me', { headers: { 'Authorization': `Bearer ${S.token}` } })
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d?.user) return;
      const newPerms = d.user.module_permissions;
      const oldPerms = S.user?.module_permissions;
      // Always update S.user with fresh server data
      S.user = Object.assign({}, S.user, d.user);
      localStorage.setItem('tt_user', JSON.stringify(S.user));
      // Rebuild the sidebar nav if permissions changed or were previously missing
      if (newPerms !== oldPerms || oldPerms === undefined) {
        const navEl = document.querySelector('.sidebar-nav');
        if (navEl) navEl.innerHTML = _buildNav(activePage);
      }
    })
    .catch(() => {});
}

// ─── Render layout (sidebar + header) ─────────────────────────────────────────
function renderLayout(activePage, pageTitle) {
  const user    = S.user    || {};
  const company = S.company || {};
  S.symbol = company.currency_symbol || user.currency_symbol || '$';

  const nav = _buildNav(activePage);

  const sidebarHTML = `
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-logo">
        <div class="logo-text">T-Tech</div>
        <div class="logo-sub">Accountant</div>
      </div>
      <div class="sidebar-company">
        <div class="company-name" id="sb-company">${esc(company.name || user.company_name || 'Company')}</div>
        <div class="company-role" id="sb-role">${esc(user.role || 'accountant')}</div>
      </div>
      <nav class="sidebar-nav">${nav}</nav>
      <div class="sidebar-footer">
        <a class="nav-link${activePage === 'settings' ? ' active' : ''}" href="/settings">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>
          Settings
        </a>
        <button class="nav-link danger" onclick="doLogout()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>
          Sign Out
        </button>
      </div>
    </aside>
    <div class="sidebar-overlay" id="sidebar-overlay"></div>`;

  const headerHTML = `
    <header class="site-header" id="site-header">
      <div class="header-left">
        <button class="menu-btn" onclick="toggleSidebar()">☰</button>
        <div class="page-title">${esc(pageTitle)}</div>
      </div>
      <div class="header-right">
        <div class="sync-badge online" id="sync-badge" onclick="processSyncQueue()" title="Click to sync">
          <div class="sync-dot"></div><span id="sync-text">Online</span>
        </div>
        <button id="install-btn" style="display:none" class="btn btn-sm btn-install" onclick="installPWA()">⬇ Install</button>
        <div class="user-chip">
          <div class="avatar" id="user-avatar">${(user.full_name || 'U')[0].toUpperCase()}</div>
          <span id="user-name">${esc((user.full_name || 'User').split(' ')[0])}</span>
        </div>
      </div>
    </header>
    <div class="offline-banner" id="offline-banner">
      <span>📡 Offline — changes queued for sync on reconnect.</span>
      <span id="offline-pending"></span>
    </div>`;

  const bottomNavHTML = `
    <nav class="bottom-nav">
      <a class="bnav-btn${activePage==='dashboard'?' active':''}" href="/dashboard">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
        Home
      </a>
      <a class="bnav-btn${activePage==='invoices'?' active':''}" href="/customers#invoices">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        Invoices
      </a>
      <a class="bnav-btn${activePage==='budgets'?' active':''}" href="/budgets">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        Budgets
      </a>
      <a class="bnav-btn${activePage==='journals'?' active':''}" href="/journals">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M4 4h16v16H4z"/><path d="M8 8h8M8 12h8M8 16h4"/></svg>
        Journal
      </a>
      <a class="bnav-btn${activePage==='settings'?' active':''}" href="/settings">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09"/></svg>
        Settings
      </a>
    </nav>`;

  // Inject into page
  const root = document.getElementById('layout-root');
  if (root) root.innerHTML = sidebarHTML + headerHTML + bottomNavHTML;

  // Sidebar toggle
  document.getElementById('sidebar-overlay')?.addEventListener('click', closeSidebar);
  updateOnlineStatus();

  // Async: refresh permissions from server and rebuild nav if stale
  _refreshNavPerms(activePage);
}

function toggleSidebar() {
  document.getElementById('sidebar')?.classList.toggle('open');
  document.getElementById('sidebar-overlay')?.classList.toggle('open');
}
function closeSidebar() {
  document.getElementById('sidebar')?.classList.remove('open');
  document.getElementById('sidebar-overlay')?.classList.remove('open');
}

// ─── Tab switching ────────────────────────────────────────────────────────────
function initTabs(defaultTab) {
  const hash = location.hash.replace('#', '') || defaultTab;
  switchTab(hash);
  window.addEventListener('hashchange', () => switchTab(location.hash.replace('#', '')));
}
function switchTab(id) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${id}`));
}

// ─── Online/Offline ───────────────────────────────────────────────────────────
function updateOnlineStatus() {
  const badge  = document.getElementById('sync-badge');
  const text   = document.getElementById('sync-text');
  const banner = document.getElementById('offline-banner');
  if (!badge) return;
  if (navigator.onLine) {
    badge.className = 'sync-badge online'; if(text) text.textContent = 'Online';
    banner?.classList.remove('visible');
  } else {
    badge.className = 'sync-badge offline'; if(text) text.textContent = 'Offline';
    banner?.classList.add('visible');
  }
}
window.addEventListener('online',  () => { updateOnlineStatus(); });
window.addEventListener('offline', () => { updateOnlineStatus(); });

// ─── Sync (stub — full sync in pages that need it) ────────────────────────────
const syncState = { pending: 0, syncing: false };
async function processSyncQueue() { /* full impl in SPA; stubs here */ }

// ─── PWA Install ──────────────────────────────────────────────────────────────
let _deferredInstall = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault(); _deferredInstall = e;
  const btn = document.getElementById('install-btn');
  if (btn) btn.style.display = 'flex';
});
window.addEventListener('appinstalled', () => {
  const btn = document.getElementById('install-btn');
  if (btn) btn.style.display = 'none';
  _deferredInstall = null;
  toast('T-Tech Accountant installed!', 'success');
});
async function installPWA() {
  if (!_deferredInstall) { toast('Use the browser address bar install button','info'); return; }
  _deferredInstall.prompt();
  const { outcome } = await _deferredInstall.userChoice;
  if (outcome === 'accepted') _deferredInstall = null;
}

// ─── Service Worker ───────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// ─── Year options helper ──────────────────────────────────────────────────────
function yearOptions(selId, selected) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  const cur = new Date().getFullYear();
  sel.innerHTML = '';
  for (let y = cur - 2; y <= cur + 3; y++) {
    const o = document.createElement('option');
    o.value = y; o.textContent = y;
    if (y === (selected || cur)) o.selected = true;
    sel.appendChild(o);
  }
}

// ─── Common account selector populator ───────────────────────────────────────
async function populateAccountSelect(selId, filterTypes, selectedId) {
  const accs = await api('GET', '/api/accounts');
  const sel  = document.getElementById(selId);
  if (!sel || !accs) return;
  const filtered = filterTypes ? accs.filter(a => filterTypes.includes(a.type)) : accs;
  sel.innerHTML = '<option value="">— Select account —</option>' +
    filtered.map(a => `<option value="${a.id}" ${a.id == selectedId ? 'selected' : ''}>${esc(a.code)} — ${esc(a.name)}</option>`).join('');
}
