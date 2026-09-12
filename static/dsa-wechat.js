/**
 * DSA Unified Identity & WeChat ClawBot Integration
 * Enterprise UI Extension: Multi-User Login & User Management & WeChat Assistant
 */
(function() {
  'use strict';

  // Inject Styles
  const style = document.createElement('style');
  style.textContent = `
    /* ⚠️ 颜色一律写成 hsl(var(--xxx))：项目里的 --secondary-text / --primary /
       --foreground 存的是「裸 HSL 三元组」（如 224 18% 28%），
       直接当颜色用是非法值 —— 声明会在计算期失效，且因为变量已定义，
       var() 的 fallback 不会兜底，color 会退化成继承父级。
       这正是注入条目文字比原生条目更亮的原因。 */
    .dsa-sidebar-item {
      display: flex;
      align-items: center;
      gap: 12px;
      width: 100%;
      height: var(--nav-item-height, 44px);
      padding: 0 var(--nav-item-padding-x, 16px);
      border-radius: 1rem;
      border: 1px solid transparent;
      font-size: 14px;
      line-height: 1;
      color: hsl(var(--secondary-text, 224 18% 28%));
      cursor: pointer;
      transition: all 0.15s ease;
      text-decoration: none;
      user-select: none;
      box-sizing: border-box;
    }
    /* ⚠️ 原生条目 hover 时「文字颜色不变」，只有背景高亮 —— 不是我们偷懒，
       而是原生的 hover:text-foreground 本来就失效：它位于 @layer utilities，
       而项目自己的 .text-secondary-text{color:var(--text-secondary-text)} 是无层级规则，
       无层级胜过有层级（与具体性无关）。注入层是无层级 <style>，
       在这里写任何 color 都会真的生效，所以刻意与 idle 保持同色才能对齐。
       另：background 的 fallback 从白色蒙版改为跟随 --primary 的配方，
       白色 fallback 是纯暗色主题假设，浅色主题下等于没有高亮。 */
    .dsa-sidebar-item:hover {
      background: var(--nav-hover-bg, hsl(var(--primary, 193 100% 43%) / 0.05));
      color: hsl(var(--secondary-text, 224 18% 28%));
    }
    /* 实测原生 active 项（Home）渲染出来是 foreground 而不是 primary：
       它的 .text-[hsl(var(--primary))] 同样被无层级的 a{color:inherit} 压过。
       要和原生一致就得用 foreground。 */
    .dsa-sidebar-item.active {
      background: var(--nav-active-bg, hsl(var(--primary, 193 100% 43%) / 0.09));
      border-color: var(--nav-active-border, hsl(var(--primary, 193 100% 43%) / 0.24));
      color: hsl(var(--foreground));
      font-weight: 500;
    }
    aside .dsa-sidebar-item {
      justify-content: center;
      /* 原生 rail 用 gap-2.5 = 10px，原来写成 8px 会差 2px */
      gap: 10px;
      padding: 0 8px;
    }
    [role="dialog"] .dsa-sidebar-item,
    [role="presentation"] .dsa-sidebar-item,
    .max-w-xs .dsa-sidebar-item {
      justify-content: flex-start;
      gap: 12px;
      padding: 0 16px;
    }

    /* 1. Sidebar scrolling: Logo and logout button stay anchored; ONLY the nav buttons scroll */
    /* ⚠️ 不要在这里写 display: flex —— React 的 <aside> 靠 Tailwind 的
       "hidden lg:flex" 做响应式显隐，而 .hidden 不带 !important，
       会被 display: flex !important 压过，导致窗口缩到 lg(1024px) 以下时
       桌面侧边栏不再隐藏。只补 flex-direction（display:none 时该属性无副作用）。
       ⚠️ 本段 CSS 位于 JS 模板字面量内：注释里禁止出现反引号，否则会提前
       终止模板字符串，整个注入脚本直接语法报错。 */
    aside {
      flex-direction: column !important;
    }
    aside > div {
      overflow: hidden !important;
      display: flex !important;
      flex-direction: column !important;
      height: 100% !important;
      max-height: calc(100vh - 2rem) !important;
      min-height: 0 !important;
    }
    aside > div > div:first-child {
      flex-shrink: 0 !important;
    }
    aside nav {
      overflow-y: auto !important;
      overflow-x: hidden !important;
      scrollbar-width: none !important;
      -ms-overflow-style: none !important;
      flex: 1 1 0% !important;
      min-height: 0 !important;
    }
    aside nav::-webkit-scrollbar {
      display: none !important;
    }
    /* ⚠️ 主题/语言下拉是 nav 内部的绝对定位元素，会被 nav 的滚动容器裁掉
       （表现为下拉右侧被切、勾选图标不完整）。
       菜单打开时临时放开裁剪，让下拉能完整溢出侧边栏。 */
    aside:has([role="menu"]) > div,
    aside:has([role="menu"]) nav {
      overflow: visible !important;
    }
    aside > div > button {
      flex-shrink: 0 !important;
      margin-top: 8px !important;
      margin-bottom: 4px !important;
    }

    /* Hide settings link nav-item for non-admin users */
    .dsa-hide-settings a[href="/settings"],
    .dsa-hide-settings a[href="#/settings"] {
      display: none !important;
    }
    .dsa-hide-settings a[href="/settings"] ~ *,
    .dsa-hide-settings a[href="/settings"]:has(~ *) {
      display: none !important;
    }

    /* 2. Full-page overlay panels: Theme-adaptive & positioned over main content area */
    .dsa-modal-backdrop {
      position: fixed;
      z-index: 35; /* Below aside (z-40) and header (z-40) */
      background: hsl(var(--background));
      display: none;
      flex-direction: column;
      overflow: hidden;
      color: hsl(var(--foreground));
      box-sizing: border-box;
    }
    .dsa-modal-backdrop.active {
      display: flex;
    }
    .dsa-modal-box {
      display: flex;
      flex-direction: column;
      flex: 1;
      min-height: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: hsl(var(--background));
    }
    .dsa-modal-header {
      display: flex;
      align-items: center;
      padding: 0 24px;
      height: 56px;
      border-bottom: 1px solid hsl(var(--border));
      flex-shrink: 0;
      background: hsl(var(--background));
    }
    .dsa-modal-title {
      font-size: 16px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 8px;
      color: hsl(var(--foreground));
    }
    .dsa-modal-body {
      padding: 24px;
      overflow-y: auto;
      flex: 1;
      min-height: 0;
      background: hsl(var(--background));
      color: hsl(var(--foreground));
    }

    /* 3. Theme-adaptive components for the 3 pages */
    .dsa-page-card {
      background: hsl(var(--card));
      border: 1px solid hsl(var(--border));
      border-radius: 14px;
      padding: 16px 18px;
      color: hsl(var(--card-foreground));
    }
    .dsa-page-input {
      width: 100%;
      box-sizing: border-box;
      background: hsl(var(--background));
      border: 1px solid hsl(var(--border));
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 13px;
      color: hsl(var(--foreground));
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    .dsa-page-input:focus {
      outline: none;
      border-color: hsl(var(--primary));
      box-shadow: 0 0 0 2px hsl(var(--primary) / 0.2);
    }
    .dsa-page-input::placeholder {
      color: hsl(var(--muted-foreground) / 0.6);
    }
    .dsa-page-select {
      box-sizing: border-box;
      background: hsl(var(--background));
      border: 1px solid hsl(var(--border));
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 13px;
      color: hsl(var(--foreground));
      outline: none;
      cursor: pointer;
    }
    .dsa-page-select:focus {
      border-color: hsl(var(--primary));
    }
    .dsa-page-label {
      display: block;
      font-size: 11px;
      font-weight: 500;
      color: hsl(var(--muted-foreground));
      margin-bottom: 4px;
    }
    .dsa-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      text-align: left;
    }
    .dsa-table th {
      background: hsl(var(--muted) / 0.5);
      color: hsl(var(--muted-foreground));
      font-weight: 600;
      padding: 10px 14px;
      border-bottom: 1px solid hsl(var(--border));
    }
    .dsa-table td {
      padding: 10px 14px;
      border-bottom: 1px solid hsl(var(--border));
      color: hsl(var(--foreground));
    }
    .dsa-table tr:hover {
      background: hsl(var(--muted) / 0.25);
    }
    .dsa-btn-primary {
      background: #10b981;
      color: #ffffff;
      border: none;
      border-radius: 8px;
      padding: 9px 20px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.15s;
    }
    .dsa-btn-primary:hover {
      opacity: 0.9;
    }
    .dsa-btn-secondary {
      background: transparent;
      border: 1px solid hsl(var(--border));
      color: hsl(var(--foreground));
      border-radius: 8px;
      padding: 7px 14px;
      font-size: 12px;
      cursor: pointer;
      transition: background 0.15s;
    }
    .dsa-btn-secondary:hover {
      background: hsl(var(--muted) / 0.4);
    }
    .dsa-status-badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 3px 10px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 500;
    }
    .dsa-status-wait { background: rgba(234, 179, 8, 0.15); color: #eab308; border: 1px solid rgba(234, 179, 8, 0.3); }
    .dsa-status-scaned { background: rgba(59, 130, 246, 0.15); color: #3b82f6; border: 1px solid rgba(59, 130, 246, 0.3); }
    .dsa-status-confirmed { background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); }
    .dsa-status-expired { background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); }
  `;
  document.head.appendChild(style);

  let currentUser = null;
  let qrPollTimer = null;

  async function checkUser(forceRefresh = false) {
    if (currentUser && !forceRefresh) return currentUser;
    try {
      const res = await fetch('/api/v1/tenancy/auth/me');
      if (res.ok) {
        const data = await res.json();
        const userObj = data.user || data;
        const principalObj = data.principal || {};
        currentUser = {
          id: userObj.id || principalObj.user_id || 1,
          username: userObj.username || principalObj.username || 'admin',
          display_name: userObj.display_name || userObj.username || '管理员',
          role: userObj.role || principalObj.role || 'user',
          wechat_bound: Boolean(userObj.wechat_bound || userObj.wechat_id),
          wechat_nickname: userObj.wechat_nickname || '',
          wechat_id: userObj.wechat_id || ''
        };
        return currentUser;
      }
    } catch (e) {
      console.warn('[DSA-Ext] checkUser tenancy failed:', e);
    }

    try {
      const stRes = await fetch('/api/v1/auth/status');
      if (stRes.ok) {
        const st = await stRes.json();
        if (st.loggedIn) {
          currentUser = {
            id: 1,
            username: 'admin',
            display_name: '系统管理员',
            role: 'admin',
            wechat_bound: false,
            wechat_nickname: '',
            wechat_id: ''
          };
          return currentUser;
        }
      }
    } catch (e) {
      console.warn('[DSA-Ext] checkUser auth/status failed:', e);
    }

    currentUser = null;
    return null;
  }

  // Position the overlay exactly over <main> so it never covers <aside>
  function positionCustomOverlay() {
    const main = document.querySelector('main');
    if (!main) return;
    const rect = main.getBoundingClientRect();
    const isDesktop = window.innerWidth >= 1024;
    const modals = document.querySelectorAll('.dsa-modal-backdrop');

    modals.forEach(modal => {
      if (isDesktop) {
        modal.style.position = 'fixed';
        modal.style.left = Math.round(rect.left) + 'px';
        modal.style.top = Math.round(rect.top) + 'px';
        modal.style.width = Math.round(rect.width) + 'px';
        modal.style.height = Math.round(rect.height) + 'px';
        modal.style.right = 'auto';
        modal.style.bottom = 'auto';
        modal.style.zIndex = '35';
      } else {
        modal.style.position = 'fixed';
        modal.style.left = '0';
        modal.style.top = Math.round(rect.top) + 'px';
        modal.style.width = '100vw';
        modal.style.height = 'calc(100vh - ' + Math.round(rect.top) + 'px)';
        modal.style.right = 'auto';
        modal.style.bottom = 'auto';
        modal.style.zIndex = '35';
      }
    });
  }
  window.addEventListener('resize', positionCustomOverlay);

  // Close all custom overlays and allow normal navigation
  function closeAllCustomPages() {
    const modals = document.querySelectorAll('.dsa-modal-backdrop');
    modals.forEach(m => m.classList.remove('active'));
    stopQrPolling();
    document.querySelectorAll('.dsa-sidebar-item').forEach(btn => btn.classList.remove('active'));
  }

  window.dsaCloseAllModals = closeAllCustomPages;
  window.dsaCloseWechatModal = closeAllCustomPages;
  window.dsaCloseUsersModal = closeAllCustomPages;
  window.dsaCloseNotifyModal = closeAllCustomPages;

  // Intercept any click on regular navigation to close custom pages
  document.addEventListener('click', function(e) {
    if (e.target.closest('.dsa-modal-backdrop')) {
      return;
    }
    if (e.target.closest('.dsa-sidebar-item')) {
      return;
    }
    if (e.target.closest('nav a, aside a, aside button, header a, header button, [role="dialog"] a')) {
      closeAllCustomPages();
    }
  }, true);

  window.addEventListener('popstate', closeAllCustomPages);
  window.addEventListener('hashchange', closeAllCustomPages);

  // =========================================================================
  // 1. Multi-User Login Enhancement on /login
  // =========================================================================
  function enhanceLoginPage() {
    if (!window.location.pathname.startsWith('/login')) return;

    const existingUserField = document.getElementById('username');
    if (existingUserField) {
      return;
    }

    const pwdInput = document.getElementById('password');
    if (!pwdInput) return;

    const form = pwdInput.closest('form');
    if (!form || form.dataset.dsaEnhanced) return;
    form.dataset.dsaEnhanced = 'true';

    const cardEl = form.parentElement;
    if (cardEl) {
      const titleEl = cardEl.querySelector('h1 span') || cardEl.querySelector('h1');
      if (titleEl && !titleEl.textContent.includes('用户登录')) {
        titleEl.textContent = '用户登录';
      }
      const descEl = cardEl.querySelector('p');
      if (descEl && !descEl.textContent.includes('系统账号')) {
        descEl.textContent = '请输入您的系统账号与密码以进入量化决策工作台。';
      }
    }

    const pwdContainer = pwdInput.closest('.flex.flex-col') || pwdInput.parentElement;
    const userWrapper = document.createElement('div');
    userWrapper.id = 'dsa-injected-username-container';
    userWrapper.className = 'flex flex-col';
    userWrapper.style.marginBottom = '1rem';
    userWrapper.innerHTML = `
      <label for="username" class="mb-2 text-sm font-medium" style="color: var(--login-label-text, #e5e7eb);">
        用户名 / 账号
      </label>
      <div class="relative flex items-center">
        <div class="absolute left-3.5 z-10 pointer-events-none" style="color: #9ca3af;">
          <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" d="M15.75 6a3.75 3.75 0 11-7 0 3.75 0 017 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" />
          </svg>
        </div>
        <input
          id="username"
          name="username"
          type="text"
          placeholder="管理员请输入 admin，成员请输入账号"
          autocomplete="username"
          class="input-surface input-focus-glow h-11 w-full rounded-xl border bg-transparent pl-10 pr-4 text-sm transition-all focus:outline-none input-appearance-login"
          style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.18); color: #fff; padding-left: 38px; height: 44px; border-radius: 12px;"
        />
      </div>
    `;

    if (pwdContainer && pwdContainer.parentElement) {
      pwdContainer.parentElement.insertBefore(userWrapper, pwdContainer);
    }

    const userInput = document.getElementById('username');
    if (userInput) userInput.focus();

    form.addEventListener('submit', async function(e) {
      const u = userInput ? userInput.value.trim() : '';
      const p = pwdInput ? pwdInput.value : '';

      if (!u && !p) return;

      e.preventDefault();
      e.stopPropagation();

      const submitBtn = form.querySelector('button[type="submit"]');
      const originalText = submitBtn ? submitBtn.textContent : '';
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = '正在登录...';
      }

      try {
        const res = await fetch('/api/v1/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: u || 'admin', password: p })
        });
        const data = await res.json();
        if (res.ok) {
          const params = new URLSearchParams(window.location.search);
          const redirect = params.get('redirect') || '/';
          window.location.assign(redirect);
        } else {
          alert('登录失败: ' + (data.message || data.error || '账号或密码错误'));
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = originalText;
          }
        }
      } catch (err) {
        alert('登录网络异常: ' + err.message);
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = originalText;
        }
      }
    }, true);
  }

  // =========================================================================
  // 2. Full-Page Overlays (WeChat Binding, Admin Users, Notifications)
  // =========================================================================
  
  // 1) WeChat Modal (No back button, theme adaptive)
  const wechatModal = document.createElement('div');
  wechatModal.className = 'dsa-modal-backdrop';
  wechatModal.id = 'dsa-wechat-modal';
  wechatModal.innerHTML = `
    <div class="dsa-modal-box">
      <div class="dsa-modal-header">
        <div class="dsa-modal-title">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="#07c160"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
          <span>绑定微信</span>
        </div>
      </div>
      <div class="dsa-modal-body">
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; align-items: start; max-width: 900px;">
          
          <!-- Left: QR Code Card -->
          <div class="dsa-page-card" style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 24px;">
            <div id="dsa-qr-container" style="position: relative; width: 210px; height: 210px; border-radius: 12px; overflow: hidden; background: #fff; border: 1px solid hsl(var(--border)); display: flex; align-items: center; justify-content: center;">
              <span style="color: #666; font-size: 12px;">正在获取微信二维码...</span>
            </div>
            <div style="margin-top: 14px; display: flex; align-items: center; gap: 8px;">
              <span id="dsa-qr-status" class="dsa-status-badge dsa-status-wait">⏳ 等待微信扫码...</span>
              <button onclick="window.dsaRefreshQr()" class="dsa-btn-secondary">刷新二维码</button>
            </div>
          </div>

          <!-- Right: Instructions Card -->
          <div style="display: flex; flex-direction: column; gap: 14px;">
            <!-- Current Status -->
            <div class="dsa-page-card">
              <div style="margin-bottom: 8px; font-size: 13px; color: hsl(var(--muted-foreground));">当前登录账号：<strong id="dsa-current-username" style="color: hsl(var(--foreground)); font-size: 14px;">-</strong></div>
              <div style="font-size: 13px; color: hsl(var(--muted-foreground));">绑定状态：<span id="dsa-current-bind-status" style="font-weight: 600;">查询中...</span></div>
              <div id="dsa-unbind-box" style="margin-top: 12px; display: none;">
                <button onclick="window.dsaUnbindWechat()" style="background: hsl(var(--destructive) / 0.15); border: 1px solid hsl(var(--destructive) / 0.35); color: hsl(var(--destructive)); border-radius: 8px; padding: 6px 14px; font-size: 12px; cursor: pointer;">解除当前微信绑定</button>
              </div>
            </div>

            <!-- Activation Guide -->
            <div style="background: rgba(7, 193, 96, 0.08); border: 1px solid rgba(7, 193, 96, 0.25); border-radius: 12px; padding: 16px; font-size: 12px; line-height: 1.6;">
              <div style="font-weight: 600; color: #10b981; margin-bottom: 8px; font-size: 13px;">📲 微信端激活步骤：</div>
              <div style="color: hsl(var(--foreground)); margin-bottom: 6px;">1. 使用手机微信扫描左侧二维码，关注并进入量化助手会话；</div>
              <div style="color: hsl(var(--foreground)); margin-bottom: 6px;">
                2. 向微信助手直接发送登录指令：<br>
                <code id="dsa-login-cmd-example" style="display: inline-block; margin-top: 4px; padding: 4px 10px; border-radius: 6px; background: hsl(var(--muted)); border: 1px solid hsl(var(--border)); color: #10b981; font-family: monospace; font-size: 12px;">登录 admin &lt;密码&gt;</code>
              </div>
              <div style="color: hsl(var(--muted-foreground)); font-size: 11px; margin-top: 6px;">绑定后永久生效，直接在微信发送股票代码（如 <code>600519</code>）即可立即触发多智能体量化深度研报！</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(wechatModal);

  // 2) User Management Modal (Admin Only, No back button, theme adaptive)
  const usersModal = document.createElement('div');
  usersModal.className = 'dsa-modal-backdrop';
  usersModal.id = 'dsa-users-modal';
  usersModal.innerHTML = `
    <div class="dsa-modal-box">
      <div class="dsa-modal-header">
        <div class="dsa-modal-title">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          <span>用户管理</span>
        </div>
      </div>
      <div class="dsa-modal-body">
        <div style="max-width: 1000px; display: flex; flex-direction: column; gap: 20px;">
          <!-- Add User Form -->
          <div class="dsa-page-card">
            <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px; color: hsl(var(--foreground)); display: flex; align-items: center; justify-content: space-between;">
              <span>➕ 开通新用户账号</span>
              <span style="font-size: 12px; font-weight: normal; color: hsl(var(--muted-foreground));">开通后将账号密码告知成员，微信直接发送即可激活</span>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px;">
              <div>
                <label class="dsa-page-label">用户名 (必填)</label>
                <input type="text" id="dsa-new-username" placeholder="如: zhangsan" class="dsa-page-input">
              </div>
              <div>
                <label class="dsa-page-label">初始密码 (至少6位)</label>
                <input type="password" id="dsa-new-password" placeholder="如: Pass1234" class="dsa-page-input">
              </div>
              <div>
                <label class="dsa-page-label">姓名 / 昵称</label>
                <input type="text" id="dsa-new-nickname" placeholder="如: 张三" class="dsa-page-input">
              </div>
              <div>
                <label class="dsa-page-label">系统角色</label>
                <select id="dsa-new-role" class="dsa-page-select" style="width: 100%; height: 37px;">
                  <option value="user">普通成员 (量化投研)</option>
                  <option value="admin">系统管理员 (全权管理)</option>
                </select>
              </div>
            </div>
            <div style="margin-top: 14px; display: flex; justify-content: flex-end;">
              <button onclick="window.dsaCreateUser()" class="dsa-btn-primary">确认开通账号</button>
            </div>
          </div>

          <!-- Users Table -->
          <div class="dsa-page-card" style="padding: 0; overflow: hidden;">
            <div style="display: flex; align-items: center; justify-content: space-between; padding: 16px 18px; border-bottom: 1px solid hsl(var(--border));">
              <span style="font-size: 14px; font-weight: 600; color: hsl(var(--foreground));">团队成员账号列表</span>
              <div style="display: flex; gap: 8px;">
                <a href="https://cow.myfi.cc.cd/chat" target="_blank" style="display: inline-flex; align-items: center; gap: 4px; background: rgba(16,185,129,0.12); border: 1px solid rgba(16,185,129,0.35); color: #10b981; border-radius: 6px; padding: 4px 10px; font-size: 12px; text-decoration: none; font-weight: 500;">
                  <span>💬 打开 Cow 消息审计台</span>
                </a>
                <button onclick="window.dsaLoadUsersTable()" class="dsa-btn-secondary" style="padding: 4px 10px;">刷新列表</button>
              </div>
            </div>
            <div style="overflow-x: auto;">
              <table class="dsa-table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>用户名</th>
                    <th>姓名/昵称</th>
                    <th>角色</th>
                    <th>微信绑定</th>
                    <th style="text-align: right;">操作</th>
                  </tr>
                </thead>
                <tbody id="dsa-users-tbody">
                  <tr><td colspan="6" style="padding: 24px; text-align: center; color: hsl(var(--muted-foreground));">正在加载数据...</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(usersModal);

  // 3) Personal Notification Settings Modal (No back button, theme adaptive)
  const notifyModal = document.createElement('div');
  notifyModal.className = 'dsa-modal-backdrop';
  notifyModal.id = 'dsa-notify-modal';
  notifyModal.innerHTML = `
    <div class="dsa-modal-box">
      <div class="dsa-modal-header">
        <div class="dsa-modal-title">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
          <span>个人通知</span>
        </div>
      </div>
      <div class="dsa-modal-body">
        <div style="max-width: 900px; display: flex; flex-direction: column; gap: 16px;">
          <div style="background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 12px; padding: 14px 18px; font-size: 13px; color: hsl(var(--foreground)); line-height: 1.6;">
            <strong style="color: #f59e0b;">💡 个人推送隔离保障：</strong> 在此配置您个人的专属推送通道。系统每日统一定时分析时，将<strong>仅推送您自己自选股的分析结果</strong>到您配置的通道，绝不推送到他人渠道，互不干扰、隐私安全。
          </div>

          <!-- Notification Channels Form -->
          <div style="display: flex; flex-direction: column; gap: 14px;">
            <!-- 1. 企业微信机器人 -->
            <div class="dsa-page-card">
              <div style="font-weight: 600; font-size: 13px; color: hsl(var(--foreground)); margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                <span>💬 企业微信群机器人</span>
              </div>
              <div>
                <label class="dsa-page-label">Webhook URL</label>
                <input type="text" id="dsa-notify-wechat" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." class="dsa-page-input">
              </div>
            </div>

            <!-- 2. 飞书机器人 -->
            <div class="dsa-page-card">
              <div style="font-weight: 600; font-size: 13px; color: hsl(var(--foreground)); margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                <span>🕊️ 飞书群机器人</span>
              </div>
              <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px;">
                <div>
                  <label class="dsa-page-label">Webhook URL</label>
                  <input type="text" id="dsa-notify-feishu" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." class="dsa-page-input">
                </div>
                <div>
                  <label class="dsa-page-label">签名密钥 (选填)</label>
                  <input type="text" id="dsa-notify-feishu-secret" placeholder="飞书安全设置中的签名密钥" class="dsa-page-input">
                </div>
              </div>
            </div>

            <!-- 3. 钉钉机器人 -->
            <div class="dsa-page-card">
              <div style="font-weight: 600; font-size: 13px; color: hsl(var(--foreground)); margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                <span>📌 钉钉群机器人</span>
              </div>
              <div>
                <label class="dsa-page-label">Webhook URL</label>
                <input type="text" id="dsa-notify-dingtalk" placeholder="https://oapi.dingtalk.com/robot/send?access_token=..." class="dsa-page-input">
              </div>
            </div>

            <!-- 4. 邮件推送 -->
            <div class="dsa-page-card">
              <div style="font-weight: 600; font-size: 13px; color: hsl(var(--foreground)); margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                <span>📧 邮件推送</span>
              </div>
              <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px;">
                <div>
                  <label class="dsa-page-label">收件人邮箱 (多个用逗号隔开)</label>
                  <input type="text" id="dsa-notify-email-receivers" placeholder="your_email@domain.com" class="dsa-page-input">
                </div>
                <div>
                  <label class="dsa-page-label">发件邮箱 (选填，留空沿用系统)</label>
                  <input type="text" id="dsa-notify-email-sender" placeholder="sender@domain.com" class="dsa-page-input">
                </div>
                <div>
                  <label class="dsa-page-label">发件邮箱授权码 (选填)</label>
                  <input type="password" id="dsa-notify-email-pass" placeholder="SMTP 授权码" class="dsa-page-input">
                </div>
              </div>
            </div>

            <!-- 5. 移动推送 / 其他 -->
            <div class="dsa-page-card">
              <div style="font-weight: 600; font-size: 13px; color: hsl(var(--foreground)); margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                <span>📱 移动应用推送 (PushPlus / Server酱 / Telegram)</span>
              </div>
              <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px;">
                <div>
                  <label class="dsa-page-label">PushPlus Token</label>
                  <input type="text" id="dsa-notify-pushplus" placeholder="PushPlus 用户 Token" class="dsa-page-input">
                </div>
                <div>
                  <label class="dsa-page-label">Server酱3 SendKey</label>
                  <input type="text" id="dsa-notify-serverchan" placeholder="Server酱 SendKey" class="dsa-page-input">
                </div>
                <div>
                  <label class="dsa-page-label">Telegram Bot Token</label>
                  <input type="text" id="dsa-notify-tg-token" placeholder="Bot Token" class="dsa-page-input">
                </div>
                <div>
                  <label class="dsa-page-label">Telegram Chat ID</label>
                  <input type="text" id="dsa-notify-tg-chat" placeholder="Chat ID" class="dsa-page-input">
                </div>
              </div>
            </div>
          </div>

          <div style="margin-top: 12px; display: flex; align-items: center; justify-content: space-between; flex-wrap: gap; gap: 12px;">
            <div style="display: flex; gap: 8px;">
              <select id="dsa-notify-test-channel" class="dsa-page-select">
                <option value="wechat">测试企业微信</option>
                <option value="feishu">测试飞书</option>
                <option value="dingtalk">测试钉钉</option>
                <option value="email">测试邮件</option>
                <option value="pushplus">测试PushPlus</option>
                <option value="serverchan3">测试Server酱</option>
                <option value="telegram">测试Telegram</option>
              </select>
              <button onclick="window.dsaTestNotifyChannel()" class="dsa-btn-secondary">🧪 发送测试通知</button>
            </div>
            <button onclick="window.dsaSaveNotifySettings()" class="dsa-btn-primary">💾 保存个人通知设置</button>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(notifyModal);

  let cachedNotifySettings = {};

  window.dsaOpenWechatModal = async function() {
    closeAllCustomPages();
    wechatModal.classList.add('active');
    positionCustomOverlay();
    document.querySelectorAll('.dsa-sidebar-item[data-dsa-tab="wechat"]').forEach(el => el.classList.add('active'));
    await updateMeStatus();
    fetchAndRenderQr();
  };

  window.dsaOpenUsersModal = function() {
    closeAllCustomPages();
    usersModal.classList.add('active');
    positionCustomOverlay();
    document.querySelectorAll('.dsa-sidebar-item[data-dsa-tab="users"]').forEach(el => el.classList.add('active'));
    window.dsaLoadUsersTable();
  };

  window.dsaOpenNotifyModal = async function() {
    closeAllCustomPages();
    notifyModal.classList.add('active');
    positionCustomOverlay();
    document.querySelectorAll('.dsa-sidebar-item[data-dsa-tab="notify"]').forEach(el => el.classList.add('active'));
    await window.dsaLoadNotifySettings();
  };

  window.dsaLoadNotifySettings = async function() {
    try {
      const res = await fetch('/api/v1/tenancy/settings');
      if (!res.ok) throw new Error('加载设置失败: ' + res.status);
      const data = await res.json();
      cachedNotifySettings = data.settings || {};

      const setVal = (id, key) => {
        const input = document.getElementById(id);
        if (!input) return;
        const item = cachedNotifySettings[key];
        if (item && item.configured) {
          input.value = item.value || '';
          input.dataset.original = item.value || '';
        } else {
          input.value = '';
          input.dataset.original = '';
        }
      };

      setVal('dsa-notify-wechat', 'WECHAT_WEBHOOK_URL');
      setVal('dsa-notify-feishu', 'FEISHU_WEBHOOK_URL');
      setVal('dsa-notify-feishu-secret', 'FEISHU_WEBHOOK_SECRET');
      setVal('dsa-notify-dingtalk', 'DINGTALK_WEBHOOK_URL');
      setVal('dsa-notify-email-receivers', 'EMAIL_RECEIVERS');
      setVal('dsa-notify-email-sender', 'EMAIL_SENDER');
      setVal('dsa-notify-email-pass', 'EMAIL_PASSWORD');
      setVal('dsa-notify-pushplus', 'PUSHPLUS_TOKEN');
      setVal('dsa-notify-serverchan', 'SERVERCHAN3_SENDKEY');
      setVal('dsa-notify-tg-token', 'TELEGRAM_BOT_TOKEN');
      setVal('dsa-notify-tg-chat', 'TELEGRAM_CHAT_ID');
    } catch (e) {
      alert('无法读取个人通知设置: ' + e);
    }
  };

  window.dsaSaveNotifySettings = async function() {
    const updates = {};
    const checkAndAdd = (id, key) => {
      const input = document.getElementById(id);
      if (!input) return;
      const val = input.value.trim();
      const orig = input.dataset.original || '';
      if (val && val !== orig) {
        updates[key] = val;
      } else if (!val && orig) {
        updates[key] = '';
      }
    };

    checkAndAdd('dsa-notify-wechat', 'WECHAT_WEBHOOK_URL');
    checkAndAdd('dsa-notify-feishu', 'FEISHU_WEBHOOK_URL');
    checkAndAdd('dsa-notify-feishu-secret', 'FEISHU_WEBHOOK_SECRET');
    checkAndAdd('dsa-notify-dingtalk', 'DINGTALK_WEBHOOK_URL');
    checkAndAdd('dsa-notify-email-receivers', 'EMAIL_RECEIVERS');
    checkAndAdd('dsa-notify-email-sender', 'EMAIL_SENDER');
    checkAndAdd('dsa-notify-email-pass', 'EMAIL_PASSWORD');
    checkAndAdd('dsa-notify-pushplus', 'PUSHPLUS_TOKEN');
    checkAndAdd('dsa-notify-serverchan', 'SERVERCHAN3_SENDKEY');
    checkAndAdd('dsa-notify-tg-token', 'TELEGRAM_BOT_TOKEN');
    checkAndAdd('dsa-notify-tg-chat', 'TELEGRAM_CHAT_ID');

    if (Object.keys(updates).length === 0) {
      alert('未检测到变更内容');
      return;
    }

    try {
      const res = await fetch('/api/v1/tenancy/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: updates })
      });
      const data = await res.json();
      if (res.ok) {
        alert('🎉 个人通知设置已成功保存！');
        window.dsaLoadNotifySettings();
      } else {
        alert('保存失败: ' + (data.message || JSON.stringify(data)));
      }
    } catch (e) {
      alert('请求错误: ' + e);
    }
  };

  window.dsaTestNotifyChannel = async function() {
    const channel = document.getElementById('dsa-notify-test-channel').value;
    const channelItems = [];

    const addField = (id, key) => {
      const input = document.getElementById(id);
      if (input && input.value.trim()) {
        channelItems.push({ key: key, value: input.value.trim() });
      }
    };

    if (channel === 'wechat') addField('dsa-notify-wechat', 'WECHAT_WEBHOOK_URL');
    if (channel === 'feishu') {
      addField('dsa-notify-feishu', 'FEISHU_WEBHOOK_URL');
      addField('dsa-notify-feishu-secret', 'FEISHU_WEBHOOK_SECRET');
    }
    if (channel === 'dingtalk') addField('dsa-notify-dingtalk', 'DINGTALK_WEBHOOK_URL');
    if (channel === 'email') {
      addField('dsa-notify-email-receivers', 'EMAIL_RECEIVERS');
      addField('dsa-notify-email-sender', 'EMAIL_SENDER');
      addField('dsa-notify-email-pass', 'EMAIL_PASSWORD');
    }
    if (channel === 'pushplus') addField('dsa-notify-pushplus', 'PUSHPLUS_TOKEN');
    if (channel === 'serverchan3') addField('dsa-notify-serverchan', 'SERVERCHAN3_SENDKEY');
    if (channel === 'telegram') {
      addField('dsa-notify-tg-token', 'TELEGRAM_BOT_TOKEN');
      addField('dsa-notify-tg-chat', 'TELEGRAM_CHAT_ID');
    }

    try {
      const res = await fetch('/api/v1/system/config/notification/test-channel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          channel: channel,
          items: channelItems,
          title: 'DSA 个人通知测试',
          content: '您好！这是一条来自 DSA 个人专属通知测试消息。配置成功！'
        })
      });
      const data = await res.json();
      if (data.success) {
        alert('✅ 测试消息发送成功！请检查您的接收渠道。');
      } else {
        alert('❌ 测试发送失败: ' + (data.message || data.error_code || '未知错误'));
      }
    } catch (e) {
      alert('请求测试失败: ' + e);
    }
  };

  window.dsaRefreshQr = function() {
    fetchAndRenderQr();
  };

  window.dsaUnbindWechat = async function() {
    if (!confirm('确定解绑当前微信号吗？解绑后微信端将无法接收投研研报。')) return;
    try {
      const res = await fetch('/api/v1/tenancy/auth/wechat-unbind', { method: 'POST' });
      if (res.ok) {
        alert('微信解绑成功');
        await checkUser(true);
        await updateMeStatus();
        fetchAndRenderQr();
      } else {
        alert('解绑失败');
      }
    } catch (e) {
      alert('请求错误: ' + e);
    }
  };

  window.dsaCreateUser = async function() {
    const u = document.getElementById('dsa-new-username').value.trim();
    const p = document.getElementById('dsa-new-password').value.trim();
    const n = document.getElementById('dsa-new-nickname').value.trim();
    const r = document.getElementById('dsa-new-role').value;

    if (!u || !p) {
      alert('用户名和密码不能为空');
      return;
    }
    if (p.length < 6) {
      alert('初始密码至少需要 6 位字符');
      return;
    }

    try {
      const res = await fetch('/api/v1/tenancy/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: u, password: p, display_name: n || u, role: r })
      });
      const data = await res.json();
      if (res.ok) {
        alert(`🎉 账号 [${u}] 开通成功！

初始密码：${p}

请将该账号和密码提供给团队成员。
成员在 Web 端直接输入账号密码即可登录；
在微信向机器人发送指令：
登录 ${u} ${p}
即可立刻绑定并使用量化投研服务！`);
        document.getElementById('dsa-new-username').value = '';
        document.getElementById('dsa-new-password').value = '';
        document.getElementById('dsa-new-nickname').value = '';
        window.dsaLoadUsersTable();
      } else {
        alert('开通失败: ' + (data.message || data.error));
      }
    } catch (e) {
      alert('请求异常: ' + e);
    }
  };

  window.dsaLoadUsersTable = async function() {
    const tbody = document.getElementById('dsa-users-tbody');
    try {
      const res = await fetch('/api/v1/tenancy/users');
      if (!res.ok) throw new Error('无权获取用户列表，请以管理员身份登录');
      const data = await res.json();
      const users = data.users || [];
      if (users.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="padding: 24px; text-align: center; color: hsl(var(--muted-foreground));">暂无其他用户</td></tr>';
        return;
      }
      tbody.innerHTML = users.map(u => `
        <tr>
          <td style="font-family: monospace; color: hsl(var(--muted-foreground));">${u.id}</td>
          <td style="font-weight: 600; color: hsl(var(--foreground));">${u.username}</td>
          <td style="color: hsl(var(--muted-foreground));">${u.display_name || '-'}</td>
          <td>
            <span style="background: ${u.username === 'cowagent' ? 'rgba(16,185,129,0.15)' : u.role === 'admin' ? 'rgba(59,130,246,0.15)' : 'hsl(var(--muted))'}; color: ${u.username === 'cowagent' ? '#10b981' : u.role === 'admin' ? '#3b82f6' : 'hsl(var(--foreground))'}; padding: 3px 8px; border-radius: 6px; font-size: 11px;">
              ${u.username === 'cowagent' ? '🤖 MCP服务账号' : u.role === 'admin' ? '系统管理员' : '普通成员'}
            </span>
          </td>
          <td>
            ${u.wechat_bound
              ? `<span style="color: #10b981; display: inline-flex; align-items: center; gap: 4px; font-weight: 500;">
                   <svg width="14" height="14" viewBox="0 0 24 24" fill="#10b981"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
                   已绑定 (${u.wechat_nickname || u.wechat_id})
                 </span>`
              : `<span style="color: hsl(var(--muted-foreground)); font-size: 12px;">○ 未绑定微信</span>`
            }
          </td>
          <td style="text-align: right; white-space: nowrap;">
            ${u.wechat_bound ? `<a href="https://cow.myfi.cc.cd/chat" target="_blank" style="display: inline-block; background: rgba(16,185,129,0.12); border: 1px solid rgba(16,185,129,0.35); color: #10b981; border-radius: 6px; padding: 2px 8px; font-size: 11px; text-decoration: none; margin-right: 6px;">💬 微信审计</a>` : ''}
            ${u.username !== 'cowagent' ? `<button onclick="window.dsaResetUserPwd(${u.id}, '${u.username}')" class="dsa-btn-secondary" style="padding: 2px 8px; font-size: 11px; margin-right: 6px;">重置密码</button>` : ''}
            ${!u.is_system && u.username !== 'admin' && u.username !== 'cowagent'
              ? `<button onclick="window.dsaDeleteUser(${u.id}, '${u.username}')" style="background: hsl(var(--destructive) / 0.15); border: 1px solid hsl(var(--destructive) / 0.4); color: hsl(var(--destructive)); border-radius: 6px; padding: 2px 8px; font-size: 11px; cursor: pointer;">删除</button>`
              : '<span style="color: hsl(var(--muted-foreground)); font-size: 11px; padding: 0 4px;">系统内置</span>'
            }
          </td>
        </tr>
      `).join('');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="6" style="padding: 20px; text-align: center; color: hsl(var(--destructive));">${e.message}</td></tr>`;
    }
  };

  window.dsaResetUserPwd = async function(uid, uname) {
    const pwd = prompt(`请输入用户 [${uname}] 的新密码（至少6位）：`);
    if (!pwd || !pwd.trim()) return;
    if (pwd.trim().length < 6) {
      alert('密码长度至少6位');
      return;
    }
    try {
      const res = await fetch(`/api/v1/tenancy/users/${uid}/reset-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_password: pwd.trim() })
      });
      if (res.ok) {
        alert(`用户 [${uname}] 密码重置成功！`);
      } else {
        alert('重置密码失败');
      }
    } catch (e) {
      alert('请求错误: ' + e);
    }
  };

  window.dsaDeleteUser = async function(uid, uname) {
    if (!confirm(`⚠️ 危险操作：确定永久删除用户 [${uname}] 吗？`)) return;
    try {
      const res = await fetch(`/api/v1/tenancy/users/${uid}`, { method: 'DELETE' });
      if (res.ok) {
        alert(`用户 [${uname}] 已成功删除`);
        window.dsaLoadUsersTable();
      } else {
        alert('删除失败');
      }
    } catch (e) {
      alert('请求错误: ' + e);
    }
  };

  async function updateMeStatus() {
    const user = await checkUser(true);
    if (!user) return;

    document.getElementById('dsa-current-username').textContent = `${user.username} (${user.display_name || '用户'})`;
    document.getElementById('dsa-login-cmd-example').textContent = `登录 ${user.username} <您的密码>`;

    const bindStatusEl = document.getElementById('dsa-current-bind-status');
    const unbindBox = document.getElementById('dsa-unbind-box');

    if (user.wechat_bound) {
      bindStatusEl.innerHTML = `<span style="color: #10b981; font-weight: 600;">已绑定 (${user.wechat_nickname || user.wechat_id || '微信用户'})` + `</span>`;
      unbindBox.style.display = 'block';
    } else {
      bindStatusEl.innerHTML = `<span style="color: #f59e0b; font-weight: 600;">尚未绑定微信</span>`;
      unbindBox.style.display = 'none';
    }
  }

  async function fetchAndRenderQr() {
    const qrContainer = document.getElementById('dsa-qr-container');
    const qrStatusEl = document.getElementById('dsa-qr-status');
    stopQrPolling();

    qrContainer.innerHTML = '<span style="color: #666; font-size: 12px;">正在生成微信二维码...</span>';
    qrStatusEl.className = 'dsa-status-badge dsa-status-wait';
    qrStatusEl.textContent = '⏳ 获取中...';

    try {
      const res = await fetch('/api/v1/tenancy/wechat/qrlogin');
      if (!res.ok) throw new Error('二维码获取失败');
      const data = await res.json();

      if (data.qr_image) {
        qrContainer.innerHTML = `<img src="${data.qr_image}" style="width: 100%; height: 100%; object-fit: contain; padding: 4px;" alt="WeChat QR">`;
        qrStatusEl.textContent = '⏳ 等待微信扫码...';
        startQrPolling();
      } else {
        qrContainer.innerHTML = '<span style="color: #ef4444; font-size: 12px;">生成失败，请点击刷新</span>';
      }
    } catch (e) {
      qrContainer.innerHTML = `<span style="color: #ef4444; font-size: 12px;">连接失败，请点击刷新</span>`;
    }
  }

  function startQrPolling() {
    stopQrPolling();
    qrPollTimer = setInterval(async () => {
      try {
        const res = await fetch('/api/v1/tenancy/wechat/qrlogin', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'poll' })
        });
        if (res.ok) {
          const data = await res.json();
          const st = data.qr_status;
          const qrStatusEl = document.getElementById('dsa-qr-status');
          if (!qrStatusEl) return;

          if (st === 'scaned') {
            qrStatusEl.className = 'dsa-status-badge dsa-status-scaned';
            qrStatusEl.textContent = '📲 已扫码，请在微信端确认';
          } else if (st === 'confirmed') {
            qrStatusEl.className = 'dsa-status-badge dsa-status-confirmed';
            qrStatusEl.textContent = '✅ 微信通道已连接';
            stopQrPolling();
            updateMeStatus();
          } else if (st === 'expired') {
            qrStatusEl.className = 'dsa-status-badge dsa-status-expired';
            qrStatusEl.textContent = '⚠️ 二维码已失效（请刷新）';
            stopQrPolling();
          }
        }
      } catch (e) {
        // continue
      }
    }, 2500);
  }

  function stopQrPolling() {
    if (qrPollTimer) {
      clearInterval(qrPollTimer);
      qrPollTimer = null;
    }
  }

  // Keyboard shortcut: Escape to close modals
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeAllCustomPages();
    }
  });

  // =========================================================================
  // 3. Role-Based Navigation & Security Enforcement
  // =========================================================================
  async function refreshUI() {
    // Check if on login page
    if (window.location.pathname.startsWith('/login')) {
      enhanceLoginPage();
      return;
    }

    const user = await checkUser();
    if (!user) return;

    // Reposition active overlays on each refresh
    positionCustomOverlay();

    // --- ENFORCE ROLE PERMISSIONS FOR NORMAL USERS ---
    if (user.role !== 'admin') {
      // 1. Hide "系统设置" (Settings) nav item — hide the entire NavLink wrapper
      document.querySelectorAll('a[href="/settings"], a[href="#/settings"]').forEach(el => {
        const navItem = el.closest('a') || el;
        navItem.style.display = 'none';
        const wrapper = navItem.parentElement;
        if (wrapper && wrapper !== navItem.closest('nav')) {
          wrapper.style.display = 'none';
        }
      });
      document.querySelectorAll('nav').forEach(nav => nav.classList.add('dsa-hide-settings'));

      // 2. If user navigated to /settings directly via address bar, immediately kick out
      if (window.location.pathname.startsWith('/settings')) {
        alert('⚠️ 权限限制：您当前为普通成员账号，无权访问底层系统设置。已为您返回首页。');
        window.location.replace('/');
        return;
      }
    }

    // --- Inject Navigation Items (Both Desktop Sidebar and Mobile Hamburger Drawer) ---
    const allNavs = document.querySelectorAll('nav');
    allNavs.forEach(nav => {
      if (nav.querySelector('.dsa-nav-extension-group')) {
        return;
      }

      const isInsideDrawer = Boolean(
        nav.closest('[role="dialog"]') ||
        nav.closest('[role="presentation"]') ||
        nav.closest('.max-w-xs') ||
        nav.closest('.animate-slide-in-left')
      );

      const sidebarGroup = document.createElement('div');
      sidebarGroup.className = 'dsa-nav-extension-group';
      sidebarGroup.style.display = 'flex';
      sidebarGroup.style.flexDirection = 'column';
      sidebarGroup.style.gap = '6px';
      sidebarGroup.style.margin = '4px 0';

      function closeDrawerIfOpen() {
        if (isInsideDrawer) {
          document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
          const closeBtn = document.querySelector(
            '[role="dialog"] button[aria-label*="关闭"], [role="dialog"] button[aria-label*="Close"], [role="presentation"] button'
          );
          if (closeBtn) closeBtn.click();
        }
      }

      // Admin Only Sidebar User Management
      if (user.role === 'admin') {
        const sideUser = document.createElement('div');
        sideUser.className = 'dsa-sidebar-item';
        sideUser.setAttribute('data-dsa-tab', 'users');
        sideUser.setAttribute('title', '团队用户管理');
        sideUser.innerHTML = `
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="shrink-0" style="flex-shrink: 0;"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          <span>用户管理</span>
        `;
        sideUser.onclick = () => {
          closeDrawerIfOpen();
          window.dsaOpenUsersModal();
        };
        sidebarGroup.appendChild(sideUser);
      }

      // WeChat Assistant for All Users
      const sideWx = document.createElement('div');
      sideWx.className = 'dsa-sidebar-item';
      sideWx.setAttribute('data-dsa-tab', 'wechat');
      sideWx.setAttribute('title', '微信量化智能助手（ClawBot）绑定');
      sideWx.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="#07c160" class="shrink-0" style="flex-shrink: 0;"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
        <span>绑定微信</span>
      `;
      sideWx.onclick = () => {
        closeDrawerIfOpen();
        window.dsaOpenWechatModal();
      };
      sidebarGroup.appendChild(sideWx);

      // Personal Notification Settings for All Users
      const sideNotify = document.createElement('div');
      sideNotify.className = 'dsa-sidebar-item';
      sideNotify.setAttribute('data-dsa-tab', 'notify');
      sideNotify.setAttribute('title', '个人通知渠道管理（独立推送）');
      sideNotify.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="#f59e0b" class="shrink-0" style="flex-shrink: 0;"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2zm-2 1H8v-6c0-2.48 1.51-4.5 4-4.5s4 2.02 4 4.5v6z"/></svg>
        <span>个人通知</span>
      `;
      sideNotify.onclick = () => {
        closeDrawerIfOpen();
        window.dsaOpenNotifyModal();
      };
      sidebarGroup.appendChild(sideNotify);

      const settingsLink = nav.querySelector('a[href="/settings"]');
      if (settingsLink) {
        nav.insertBefore(sidebarGroup, settingsLink);
      } else {
        nav.appendChild(sidebarGroup);
      }
    });
  }

  // Periodic DOM check & permission enforcement
  setInterval(refreshUI, 500);

  // Instant reactivity: hamburger button clicks and drawer opening
  document.addEventListener('click', function(e) {
    const btn = e.target && e.target.closest ? e.target.closest('button') : null;
    if (btn) {
      const aria = btn.getAttribute('aria-label') || '';
      if (aria.includes('导航') || aria.includes('Nav') || aria.includes('菜单') || btn.classList.contains('pointer-events-auto')) {
        setTimeout(refreshUI, 20);
        setTimeout(refreshUI, 100);
        setTimeout(refreshUI, 300);
      }
    }
  }, true);

  if (window.MutationObserver) {
    const navObserver = new MutationObserver(function(mutations) {
      for (let i = 0; i < mutations.length; i++) {
        const added = mutations[i].addedNodes;
        for (let j = 0; j < added.length; j++) {
          const node = added[j];
          if (node.nodeType === 1) {
            if (node.tagName === 'NAV' || (node.querySelector && node.querySelector('nav')) || node.getAttribute?.('role') === 'dialog' || node.getAttribute?.('role') === 'presentation') {
              refreshUI();
              return;
            }
          }
        }
      }
    });
    navObserver.observe(document.body, { childList: true, subtree: true });
  }
})();
