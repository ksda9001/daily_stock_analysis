/**
 * DSA Unified Identity & WeChat ClawBot Integration
 * Enterprise UI Extension: User Management & WeChat Assistant
 */
(function() {
  'use strict';

  // Inject Styles
  const style = document.createElement('style');
  style.textContent = `
    .dsa-wx-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 10px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      border: 1px solid rgba(7, 193, 96, 0.35);
      background: rgba(7, 193, 96, 0.12);
      color: #07c160;
      user-select: none;
    }
    .dsa-wx-btn:hover {
      background: rgba(7, 193, 96, 0.25);
      border-color: rgba(7, 193, 96, 0.6);
      transform: translateY(-1px);
    }
    .dsa-admin-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 10px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      border: 1px solid rgba(59, 130, 246, 0.35);
      background: rgba(59, 130, 246, 0.12);
      color: #3b82f6;
      user-select: none;
    }
    .dsa-admin-btn:hover {
      background: rgba(59, 130, 246, 0.25);
      border-color: rgba(59, 130, 246, 0.6);
      transform: translateY(-1px);
    }
    .dsa-floating-dock {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 99990;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border-radius: 14px;
      background: rgba(17, 24, 39, 0.9);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(255, 255, 255, 0.16);
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.6), 0 8px 10px -6px rgba(0, 0, 0, 0.4);
      transition: all 0.25s ease;
    }
    .dsa-floating-dock:hover {
      box-shadow: 0 20px 30px -10px rgba(0, 0, 0, 0.8);
      border-color: rgba(255, 255, 255, 0.28);
    }
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
      color: var(--secondary-text, #9ca3af);
      cursor: pointer;
      transition: all 0.15s ease;
      text-decoration: none;
      user-select: none;
    }
    .dsa-sidebar-item:hover {
      background: var(--nav-hover-bg, rgba(255, 255, 255, 0.06));
      color: #fff;
    }
    .dsa-modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(8px);
      z-index: 99999;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.25s ease;
    }
    .dsa-modal-backdrop.active {
      opacity: 1;
      pointer-events: auto;
    }
    .dsa-modal-box {
      background: #111827;
      color: #f3f4f6;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 20px;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7);
      width: 100%;
      max-width: 720px;
      max-height: 90vh;
      overflow-y: auto;
      transform: scale(0.95);
      transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .dsa-modal-backdrop.active .dsa-modal-box {
      transform: scale(1);
    }
    .dsa-modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 18px 24px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }
    .dsa-modal-title {
      font-size: 17px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .dsa-modal-close {
      background: transparent;
      border: none;
      color: #9ca3af;
      font-size: 20px;
      cursor: pointer;
      padding: 4px 8px;
      border-radius: 8px;
      line-height: 1;
    }
    .dsa-modal-close:hover {
      background: rgba(255, 255, 255, 0.1);
      color: #fff;
    }
    .dsa-modal-body {
      padding: 24px;
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

  // Create Modals
  const wechatModal = document.createElement('div');
  wechatModal.className = 'dsa-modal-backdrop';
  wechatModal.id = 'dsa-wechat-modal';
  wechatModal.innerHTML = `
    <div class="dsa-modal-box">
      <div class="dsa-modal-header">
        <div class="dsa-modal-title">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="#07c160"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
          <span>微信量化智能助手（ClawBot）绑定</span>
        </div>
        <button class="dsa-modal-close" onclick="window.dsaCloseWechatModal()">&times;</button>
      </div>
      <div class="dsa-modal-body">
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 24px; align-items: start;">
          
          <!-- Left: QR Code -->
          <div style="display: flex; flex-direction: column; align-items: center; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 20px;">
            <div id="dsa-qr-container" style="position: relative; width: 210px; height: 210px; border-radius: 12px; overflow: hidden; background: #fff; display: flex; align-items: center; justify-content: center;">
              <span style="color: #666; font-size: 12px;">正在获取微信二维码...</span>
            </div>
            <div style="margin-top: 14px; display: flex; align-items: center; gap: 8px;">
              <span id="dsa-qr-status" class="dsa-status-badge dsa-status-wait">⏳ 等待微信扫码...</span>
              <button onclick="window.dsaRefreshQr()" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.18); color: #fff; border-radius: 8px; padding: 3px 10px; font-size: 12px; cursor: pointer;">刷新二维码</button>
            </div>
          </div>

          <!-- Right: Instructions -->
          <div style="display: flex; flex-direction: column; gap: 14px;">
            <!-- Current Status -->
            <div style="background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px; font-size: 13px;">
              <div style="margin-bottom: 6px; color: #9ca3af;">当前登录账号：<strong id="dsa-current-username" style="color: #fff;">-</strong></div>
              <div style="color: #9ca3af;">绑定状态：<span id="dsa-current-bind-status" style="font-weight: 600;">查询中...</span></div>
              <div id="dsa-unbind-box" style="margin-top: 10px; display: none;">
                <button onclick="window.dsaUnbindWechat()" style="background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #ef4444; border-radius: 8px; padding: 5px 14px; font-size: 12px; cursor: pointer;">解除当前微信绑定</button>
              </div>
            </div>

            <!-- Activation Guide -->
            <div style="background: rgba(7, 193, 96, 0.08); border: 1px solid rgba(7, 193, 96, 0.25); border-radius: 12px; padding: 14px; font-size: 12px; line-height: 1.6;">
              <div style="font-weight: 600; color: #07c160; margin-bottom: 6px; font-size: 13px;">📲 微信端激活步骤：</div>
              <div style="color: #d1d5db; margin-bottom: 6px;">1. 使用手机微信扫描左侧二维码，关注并进入量化助手会话；</div>
              <div style="color: #d1d5db; margin-bottom: 6px;">
                2. 向微信助手直接发送登录指令：<br>
                <code id="dsa-login-cmd-example" style="display: inline-block; margin-top: 4px; padding: 3px 8px; border-radius: 6px; background: rgba(0,0,0,0.5); border: 1px solid rgba(7, 193, 96, 0.3); color: #34d399; font-family: monospace;">登录 admin &lt;密码&gt;</code>
              </div>
              <div style="color: #9ca3af; font-size: 11px;">绑定后永久生效，直接在微信发送股票代码（如 <code>600519</code>）即可立即触发多智能体量化深度研报！</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(wechatModal);

  // User Management Modal (Admin)
  const usersModal = document.createElement('div');
  usersModal.className = 'dsa-modal-backdrop';
  usersModal.id = 'dsa-users-modal';
  usersModal.innerHTML = `
    <div class="dsa-modal-box" style="max-width: 820px;">
      <div class="dsa-modal-header">
        <div class="dsa-modal-title">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="#3b82f6"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
          <span>量化团队用户管理（管理员开通制）</span>
        </div>
        <button class="dsa-modal-close" onclick="window.dsaCloseUsersModal()">&times;</button>
      </div>
      <div class="dsa-modal-body">
        <!-- Add User Form -->
        <div style="background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 18px; margin-bottom: 22px;">
          <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px; color: #fff; display: flex; align-items: center; justify-content: space-between;">
            <span>➕ 开通新用户账号</span>
            <span style="font-size: 12px; font-weight: normal; color: #9ca3af;">开通后将账号密码告知成员，微信直接发送即可激活</span>
          </div>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px;">
            <div>
              <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">用户名 (必填)</label>
              <input type="text" id="dsa-new-username" placeholder="如: zhangsan" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
            </div>
            <div>
              <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">初始密码 (至少6位)</label>
              <input type="password" id="dsa-new-password" placeholder="如: Pass1234" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
            </div>
            <div>
              <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">姓名 / 昵称</label>
              <input type="text" id="dsa-new-nickname" placeholder="如: 张三" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
            </div>
            <div>
              <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">系统角色</label>
              <select id="dsa-new-role" style="width: 100%; box-sizing: border-box; background: #1f2937; border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
                <option value="user">普通用户 (量化投研)</option>
                <option value="admin">系统管理员 (全权管理)</option>
              </select>
            </div>
          </div>
          <div style="margin-top: 14px; display: flex; justify-content: flex-end;">
            <button onclick="window.dsaCreateUser()" style="background: #2563eb; color: #fff; border: none; border-radius: 8px; padding: 8px 20px; font-size: 13px; font-weight: 500; cursor: pointer; transition: all 0.2s;">确认开通账号</button>
          </div>
        </div>

        <!-- Users Table -->
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;">
          <span style="font-size: 14px; font-weight: 600; color: #fff;">团队成员账号列表</span>
          <button onclick="window.dsaLoadUsersTable()" style="background: transparent; border: 1px solid rgba(255,255,255,0.15); color: #9ca3af; border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer;">刷新列表</button>
        </div>
        <div style="overflow-x: auto; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;">
          <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;">
            <thead>
              <tr style="background: rgba(255,255,255,0.03); border-bottom: 1px solid rgba(255,255,255,0.08); color: #9ca3af;">
                <th style="padding: 10px 12px;">ID</th>
                <th style="padding: 10px 12px;">用户名</th>
                <th style="padding: 10px 12px;">姓名/昵称</th>
                <th style="padding: 10px 12px;">角色</th>
                <th style="padding: 10px 12px;">微信绑定</th>
                <th style="padding: 10px 12px; text-align: right;">操作</th>
              </tr>
            </thead>
            <tbody id="dsa-users-tbody">
              <tr><td colspan="6" style="padding: 20px; text-align: center; color: #6b7280;">正在加载数据...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(usersModal);

  // Global functions
  window.dsaOpenWechatModal = async function() {
    wechatModal.classList.add('active');
    await updateMeStatus();
    fetchAndRenderQr();
  };

  window.dsaCloseWechatModal = function() {
    wechatModal.classList.remove('active');
    stopQrPolling();
  };

  window.dsaOpenUsersModal = function() {
    usersModal.classList.add('active');
    window.dsaLoadUsersTable();
  };

  window.dsaCloseUsersModal = function() {
    usersModal.classList.remove('active');
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
        alert(`🎉 账号 [${u}] 开通成功！\n\n初始密码：${p}\n\n请将该账号和密码提供给团队成员。\n成员在微信向机器人发送指令：\n登录 ${u} ${p}\n即可立刻绑定并使用量化投研服务！`);
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
        tbody.innerHTML = '<tr><td colspan="6" style="padding: 20px; text-align: center; color: #6b7280;">暂无其他用户</td></tr>';
        return;
      }
      tbody.innerHTML = users.map(u => `
        <tr style="border-bottom: 1px solid rgba(255,255,255,0.04);">
          <td style="padding: 10px 12px; font-family: monospace; color: #9ca3af;">${u.id}</td>
          <td style="padding: 10px 12px; font-weight: 600;">${u.username}</td>
          <td style="padding: 10px 12px; color: #d1d5db;">${u.display_name || '-'}</td>
          <td style="padding: 10px 12px;">
            <span style="background: ${u.role === 'admin' ? 'rgba(59,130,246,0.2)' : 'rgba(255,255,255,0.1)'}; color: ${u.role === 'admin' ? '#60a5fa' : '#d1d5db'}; padding: 2px 8px; border-radius: 6px; font-size: 11px;">
              ${u.role === 'admin' ? '系统管理员' : '普通用户'}
            </span>
          </td>
          <td style="padding: 10px 12px;">
            ${u.wechat_bound
              ? `<span style="color: #10b981; display: inline-flex; align-items: center; gap: 4px; font-weight: 500;">
                   <svg width="14" height="14" viewBox="0 0 24 24" fill="#10b981"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
                   已绑定 (${u.wechat_nickname || u.wechat_id})
                 </span>`
              : `<span style="color: #6b7280; font-size: 12px;">○ 未绑定微信</span>`
            }
          </td>
          <td style="padding: 10px 12px; text-align: right;">
            <button onclick="window.dsaResetUserPwd(${u.id}, '${u.username}')" style="background: transparent; border: 1px solid rgba(245,158,11,0.4); color: #f59e0b; border-radius: 6px; padding: 2px 8px; font-size: 11px; cursor: pointer; margin-right: 6px;">重置密码</button>
            ${!u.is_system && u.username !== 'admin'
              ? `<button onclick="window.dsaDeleteUser(${u.id}, '${u.username}')" style="background: transparent; border: 1px solid rgba(239,68,68,0.4); color: #ef4444; border-radius: 6px; padding: 2px 8px; font-size: 11px; cursor: pointer;">删除</button>`
              : ''
            }
          </td>
        </tr>
      `).join('');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="6" style="padding: 20px; text-align: center; color: #ef4444;">${e.message}</td></tr>`;
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
      bindStatusEl.innerHTML = `<span style="color: #10b981; font-weight: 600;">已绑定 (${user.wechat_nickname || user.wechat_id || '微信用户'})</span>`;
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
      window.dsaCloseWechatModal();
      window.dsaCloseUsersModal();
    }
  });

  // Inject UI Components
  async function refreshUI() {
    const user = await checkUser();
    if (!user) return;

    // 1. Inject Header Buttons
    const header = document.querySelector('header .max-w-\\[1680px\\]') || document.querySelector('header > div') || document.querySelector('header');
    if (header && !document.getElementById('dsa-header-group')) {
      const btnGroup = document.createElement('div');
      btnGroup.id = 'dsa-header-group';
      btnGroup.style.display = 'flex';
      btnGroup.style.alignItems = 'center';
      btnGroup.style.gap = '8px';
      btnGroup.style.marginRight = '8px';

      // WeChat Button
      const wxBtn = document.createElement('button');
      wxBtn.id = 'dsa-wechat-btn';
      wxBtn.className = 'dsa-wx-btn';
      wxBtn.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
        <span>${user.wechat_bound ? '微信已绑定' : '绑定微信'}</span>
      `;
      wxBtn.onclick = window.dsaOpenWechatModal;
      btnGroup.appendChild(wxBtn);

      // Admin User Management Button
      if (user.role === 'admin') {
        const usersBtn = document.createElement('button');
        usersBtn.id = 'dsa-users-btn';
        usersBtn.className = 'dsa-admin-btn';
        usersBtn.innerHTML = `
          <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
          <span>用户管理</span>
        `;
        usersBtn.onclick = window.dsaOpenUsersModal;
        btnGroup.appendChild(usersBtn);
      }

      const lastChild = header.lastElementChild;
      if (lastChild) {
        header.insertBefore(btnGroup, lastChild.previousElementSibling || lastChild);
      } else {
        header.appendChild(btnGroup);
      }
    }

    // 2. Inject Left Sidebar Navigation Items
    const nav = document.querySelector('nav[aria-label="主要导航"]') || document.querySelector('nav');
    if (nav && !document.getElementById('dsa-sidebar-group')) {
      const sidebarGroup = document.createElement('div');
      sidebarGroup.id = 'dsa-sidebar-group';
      sidebarGroup.style.display = 'flex';
      sidebarGroup.style.flexDirection = 'column';
      sidebarGroup.style.gap = '6px';
      sidebarGroup.style.margin = '4px 0';

      if (user.role === 'admin') {
        const sideUser = document.createElement('div');
        sideUser.className = 'dsa-sidebar-item';
        sideUser.innerHTML = `
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          <span style="font-weight: 500;">用户管理</span>
        `;
        sideUser.onclick = window.dsaOpenUsersModal;
        sidebarGroup.appendChild(sideUser);
      }

      const sideWx = document.createElement('div');
      sideWx.className = 'dsa-sidebar-item';
      sideWx.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="#07c160"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
        <span style="font-weight: 500;">微信投研助手</span>
      `;
      sideWx.onclick = window.dsaOpenWechatModal;
      sidebarGroup.appendChild(sideWx);

      // Insert before Settings or at bottom of nav
      const settingsLink = nav.querySelector('a[href="/settings"]');
      if (settingsLink) {
        nav.insertBefore(sidebarGroup, settingsLink);
      } else {
        nav.appendChild(sidebarGroup);
      }
    }

    // 3. Inject Floating Quick Action Dock
    if (!document.getElementById('dsa-floating-dock')) {
      const dock = document.createElement('div');
      dock.id = 'dsa-floating-dock';
      dock.className = 'dsa-floating-dock';
      
      let html = '';
      if (user.role === 'admin') {
        html += `
          <button onclick="window.dsaOpenUsersModal()" class="dsa-admin-btn" title="团队用户管理">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
            <span>用户管理</span>
          </button>
        `;
      }
      html += `
        <button onclick="window.dsaOpenWechatModal()" class="dsa-wx-btn" title="微信助手绑定">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
          <span>微信助手</span>
        </button>
      `;
      dock.innerHTML = html;
      document.body.appendChild(dock);
    }
  }

  // Periodic DOM check
  setInterval(refreshUI, 1200);
})();
