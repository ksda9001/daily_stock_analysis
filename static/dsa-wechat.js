/**
 * DSA Unified Identity & WeChat ClawBot Integration
 * Enterprise UI Extension: Multi-User Login & User Management & WeChat Assistant
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
    .dsa-notify-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 10px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      border: 1px solid rgba(245, 158, 11, 0.35);
      background: rgba(245, 158, 11, 0.12);
      color: #f59e0b;
      user-select: none;
    }
    .dsa-notify-btn:hover {
      background: rgba(245, 158, 11, 0.25);
      border-color: rgba(245, 158, 11, 0.6);
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

  // =========================================================================
  // 1. Multi-User Login Enhancement on /login
  // =========================================================================
  function enhanceLoginPage() {
    // Native React bundle (LoginPage-_15D7jin.js & index-Dhkgyx-b.js) now natively handles
    // the username field and multi-user login state, avoiding DOM desync with React.
    return;
  }


  // =========================================================================
  // 2. Modals (WeChat Binding & Admin User Management)
  // =========================================================================
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

  // User Management Modal (Admin Only)
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
                <option value="user">普通成员 (量化投研)</option>
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
          <div style="display: flex; gap: 8px;">
            <a href="https://cow.myfi.cc.cd/chat" target="_blank" style="display: inline-flex; align-items: center; gap: 4px; background: rgba(16,185,129,0.15); border: 1px solid rgba(16,185,129,0.35); color: #10b981; border-radius: 6px; padding: 4px 10px; font-size: 12px; text-decoration: none; font-weight: 500;">
              <span>💬 打开 Cow 消息审计台</span>
            </a>
            <button onclick="window.dsaLoadUsersTable()" style="background: transparent; border: 1px solid rgba(255,255,255,0.15); color: #9ca3af; border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer;">刷新列表</button>
          </div>
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

  // Personal Notification Settings Modal (All Users)
  const notifyModal = document.createElement('div');
  notifyModal.className = 'dsa-modal-backdrop';
  notifyModal.id = 'dsa-notify-modal';
  notifyModal.innerHTML = `
    <div class="dsa-modal-box" style="max-width: 760px;">
      <div class="dsa-modal-header">
        <div class="dsa-modal-title">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="#f59e0b"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2zm-2 1H8v-6c0-2.48 1.51-4.5 4-4.5s4 2.02 4 4.5v6z"/></svg>
          <span>个人通知渠道管理（独立推送）</span>
        </div>
        <button class="dsa-modal-close" onclick="window.dsaCloseNotifyModal()">&times;</button>
      </div>
      <div class="dsa-modal-body">
        <div style="background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 14px; padding: 14px 18px; margin-bottom: 20px; font-size: 13px; color: #fbbf24; line-height: 1.6;">
          <strong>💡 个人推送隔离保障：</strong> 在此配置您个人的专属推送通道。系统每日统一定时分析时，将<strong>仅推送您自己自选股的分析结果</strong>到您配置的通道，绝不推送到他人渠道，互不干扰、隐私安全。
        </div>

        <!-- Notification Channels Form -->
        <div style="display: flex; flex-direction: column; gap: 18px;">
          <!-- 1. 企业微信机器人 -->
          <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px 16px;">
            <div style="font-weight: 600; font-size: 13px; color: #fff; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
              <span>💬 企业微信群机器人</span>
            </div>
            <div>
              <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">Webhook URL</label>
              <input type="text" id="dsa-notify-wechat" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
            </div>
          </div>

          <!-- 2. 飞书机器人 -->
          <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px 16px;">
            <div style="font-weight: 600; font-size: 13px; color: #fff; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
              <span>🕊️ 飞书群机器人</span>
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">Webhook URL</label>
                <input type="text" id="dsa-notify-feishu" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">签名密钥 (选填)</label>
                <input type="text" id="dsa-notify-feishu-secret" placeholder="飞书安全设置中的签名密钥" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
            </div>
          </div>

          <!-- 3. 钉钉机器人 -->
          <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px 16px;">
            <div style="font-weight: 600; font-size: 13px; color: #fff; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
              <span>📌 钉钉群机器人</span>
            </div>
            <div>
              <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">Webhook URL</label>
              <input type="text" id="dsa-notify-dingtalk" placeholder="https://oapi.dingtalk.com/robot/send?access_token=..." style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
            </div>
          </div>

          <!-- 4. 邮件推送 -->
          <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px 16px;">
            <div style="font-weight: 600; font-size: 13px; color: #fff; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
              <span>📧 邮件推送</span>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px;">
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">收件人邮箱 (多个用逗号隔开)</label>
                <input type="text" id="dsa-notify-email-receivers" placeholder="your_email@domain.com" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">发件邮箱 (选填，留空沿用系统)</label>
                <input type="text" id="dsa-notify-email-sender" placeholder="sender@domain.com" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">发件邮箱授权码 (选填)</label>
                <input type="password" id="dsa-notify-email-pass" placeholder="SMTP 授权码" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
            </div>
          </div>

          <!-- 5. 移动推送 / 其他 -->
          <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px 16px;">
            <div style="font-weight: 600; font-size: 13px; color: #fff; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
              <span>📱 移动应用推送 (PushPlus / Server酱 / Telegram)</span>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px;">
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">PushPlus Token</label>
                <input type="text" id="dsa-notify-pushplus" placeholder="PushPlus 用户 Token" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">Server酱3 SendKey</label>
                <input type="text" id="dsa-notify-serverchan" placeholder="Server酱 SendKey" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">Telegram Bot Token</label>
                <input type="text" id="dsa-notify-tg-token" placeholder="Bot Token" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
              <div>
                <label style="display: block; font-size: 11px; color: #9ca3af; margin-bottom: 4px;">Telegram Chat ID</label>
                <input type="text" id="dsa-notify-tg-chat" placeholder="Chat ID" style="width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 10px; font-size: 13px; color: #fff;">
              </div>
            </div>
          </div>
        </div>

        <div style="margin-top: 24px; display: flex; align-items: center; justify-content: space-between;">
          <div style="display: flex; gap: 8px;">
            <select id="dsa-notify-test-channel" style="background: #1f2937; border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 8px 12px; font-size: 12px; color: #fff;">
              <option value="wechat">测试企业微信</option>
              <option value="feishu">测试飞书</option>
              <option value="dingtalk">测试钉钉</option>
              <option value="email">测试邮件</option>
              <option value="pushplus">测试PushPlus</option>
              <option value="serverchan3">测试Server酱</option>
              <option value="telegram">测试Telegram</option>
            </select>
            <button onclick="window.dsaTestNotifyChannel()" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15); color: #e5e7eb; border-radius: 8px; padding: 8px 14px; font-size: 12px; cursor: pointer;">🧪 发送测试通知</button>
          </div>
          <button onclick="window.dsaSaveNotifySettings()" style="background: #059669; color: #fff; border: none; border-radius: 8px; padding: 10px 24px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s;">💾 保存个人通知设置</button>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(notifyModal);

  let cachedNotifySettings = {};

  window.dsaOpenNotifyModal = async function() {
    notifyModal.classList.add('active');
    await window.dsaLoadNotifySettings();
  };

  window.dsaCloseNotifyModal = function() {
    notifyModal.classList.remove('active');
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
        alert(`🎉 账号 [${u}] 开通成功！\n\n初始密码：${p}\n\n请将该账号和密码提供给团队成员。\n成员在 Web 端直接输入账号密码即可登录；\n在微信向机器人发送指令：\n登录 ${u} ${p}\n即可立刻绑定并使用量化投研服务！`);
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
            <span style="background: ${u.username === 'cowagent' ? 'rgba(16,185,129,0.15)' : u.role === 'admin' ? 'rgba(59,130,246,0.2)' : 'rgba(255,255,255,0.1)'}; color: ${u.username === 'cowagent' ? '#10b981' : u.role === 'admin' ? '#60a5fa' : '#d1d5db'}; padding: 2px 8px; border-radius: 6px; font-size: 11px;">
              ${u.username === 'cowagent' ? '🤖 MCP服务账号' : u.role === 'admin' ? '系统管理员' : '普通成员'}
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
          <td style="padding: 10px 12px; text-align: right; white-space: nowrap;">
            ${u.wechat_bound ? `<a href="https://cow.myfi.cc.cd/chat" target="_blank" style="display: inline-block; background: rgba(16,185,129,0.15); border: 1px solid rgba(16,185,129,0.4); color: #10b981; border-radius: 6px; padding: 2px 8px; font-size: 11px; text-decoration: none; margin-right: 6px;">💬 微信审计</a>` : ''}
            ${u.username !== 'cowagent' ? `<button onclick="window.dsaResetUserPwd(${u.id}, '${u.username}')" style="background: transparent; border: 1px solid rgba(245,158,11,0.4); color: #f59e0b; border-radius: 6px; padding: 2px 8px; font-size: 11px; cursor: pointer; margin-right: 6px;">重置密码</button>` : ''}
            ${!u.is_system && u.username !== 'admin' && u.username !== 'cowagent'
              ? `<button onclick="window.dsaDeleteUser(${u.id}, '${u.username}')" style="background: transparent; border: 1px solid rgba(239,68,68,0.4); color: #ef4444; border-radius: 6px; padding: 2px 8px; font-size: 11px; cursor: pointer;">删除</button>`
              : '<span style="color: #6b7280; font-size: 11px; padding: 0 4px;">系统内置</span>'
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

    // --- ENFORCE ROLE PERMISSIONS FOR NORMAL USERS ---
    if (user.role !== 'admin') {
      // 1. Hide "系统设置" (Settings) from sidebar completely
      const settingsLinks = document.querySelectorAll('a[href="/settings"], a[href="#/settings"]');
      settingsLinks.forEach(el => {
        el.style.display = 'none';
      });

      // 2. If user navigated to /settings directly via address bar, immediately kick out
      if (window.location.pathname.startsWith('/settings')) {
        alert('⚠️ 权限限制：您当前为普通成员账号，无权访问底层系统设置。已为您返回首页。');
        window.location.replace('/');
        return;
      }
    }

    // --- 1. Inject Header Buttons ---
    const header = document.querySelector('header .max-w-\\[1680px\\]') || document.querySelector('header > div') || document.querySelector('header');
    if (header && !document.getElementById('dsa-header-group')) {
      const btnGroup = document.createElement('div');
      btnGroup.id = 'dsa-header-group';
      btnGroup.style.display = 'flex';
      btnGroup.style.alignItems = 'center';
      btnGroup.style.gap = '8px';
      btnGroup.style.marginRight = '8px';

      // WeChat Button (Available to all logged-in users)
      const wxBtn = document.createElement('button');
      wxBtn.id = 'dsa-wechat-btn';
      wxBtn.className = 'dsa-wx-btn';
      wxBtn.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
        <span>${user.wechat_bound ? '微信已绑定' : '绑定微信'}</span>
      `;
      wxBtn.onclick = window.dsaOpenWechatModal;
      btnGroup.appendChild(wxBtn);

      // Personal Notification Button (Available to all logged-in users)
      const notifyBtn = document.createElement('button');
      notifyBtn.id = 'dsa-notify-btn';
      notifyBtn.className = 'dsa-notify-btn';
      notifyBtn.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2zm-2 1H8v-6c0-2.48 1.51-4.5 4-4.5s4 2.02 4 4.5v6z"/></svg>
        <span>个人通知</span>
      `;
      notifyBtn.onclick = window.dsaOpenNotifyModal;
      btnGroup.appendChild(notifyBtn);

      // Admin User Management Button (Admin Only)
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

    // --- 2. Inject Left Sidebar Navigation Items ---
    const nav = document.querySelector('nav[aria-label="主要导航"]') || document.querySelector('nav');
    if (nav && !document.getElementById('dsa-sidebar-group')) {
      const sidebarGroup = document.createElement('div');
      sidebarGroup.id = 'dsa-sidebar-group';
      sidebarGroup.style.display = 'flex';
      sidebarGroup.style.flexDirection = 'column';
      sidebarGroup.style.gap = '6px';
      sidebarGroup.style.margin = '4px 0';

      // Admin Only Sidebar User Management
      if (user.role === 'admin') {
        const sideUser = document.createElement('div');
        sideUser.id = 'dsa-sidebar-users-btn';
        sideUser.className = 'dsa-sidebar-item';
        sideUser.innerHTML = `
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          <span style="font-weight: 500;">用户管理</span>
        `;
        sideUser.onclick = window.dsaOpenUsersModal;
        sidebarGroup.appendChild(sideUser);
      }

      // WeChat Assistant for All Users
      const sideWx = document.createElement('div');
      sideWx.className = 'dsa-sidebar-item';
      sideWx.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="#07c160"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
        <span style="font-weight: 500;">微信投研助手</span>
      `;
      sideWx.onclick = window.dsaOpenWechatModal;
      sidebarGroup.appendChild(sideWx);

      // Personal Notification Settings for All Users
      const sideNotify = document.createElement('div');
      sideNotify.className = 'dsa-sidebar-item';
      sideNotify.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="#f59e0b"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2zm-2 1H8v-6c0-2.48 1.51-4.5 4-4.5s4 2.02 4 4.5v6z"/></svg>
        <span style="font-weight: 500;">个人通知设置</span>
      `;
      sideNotify.onclick = window.dsaOpenNotifyModal;
      sidebarGroup.appendChild(sideNotify);

      const settingsLink = nav.querySelector('a[href="/settings"]');
      if (settingsLink) {
        nav.insertBefore(sidebarGroup, settingsLink);
      } else {
        nav.appendChild(sidebarGroup);
      }
    }

    // --- 3. Inject Floating Quick Action Dock ---
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
      } else {
        html += `
          <span style="font-size: 12px; color: #9ca3af; padding: 0 4px; display: inline-flex; align-items: center; gap: 4px;">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
            ${user.display_name || user.username}
          </span>
        `;
      }
      html += `
        <button onclick="window.dsaOpenNotifyModal()" class="dsa-notify-btn" title="个人专属通知设置">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2zm-2 1H8v-6c0-2.48 1.51-4.5 4-4.5s4 2.02 4 4.5v6z"/></svg>
          <span>通知设置</span>
        </button>
        <button onclick="window.dsaOpenWechatModal()" class="dsa-wx-btn" title="微信助手绑定">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M8.5 2C4.36 2 1 4.91 1 8.5c0 2.02 1.07 3.84 2.76 5.07l-.7 2.12c-.08.24.16.46.38.35l2.67-1.34c.75.22 1.55.35 2.39.35.25 0 .5-.01.74-.04-.2-.64-.31-1.32-.31-2.01 0-3.87 3.58-7 8-7 .2 0 .4 0 .6.02C16.32 4.41 12.69 2 8.5 2zM19 8c-3.87 0-7 2.69-7 6s3.13 6 7 6c.69 0 1.36-.09 1.98-.26l2.25 1.13c.22.11.46-.11.38-.35l-.59-1.78C23.95 17.65 25 15.93 25 14c0-3.31-3.13-6-7-6z"/></svg>
          <span>${user.wechat_bound ? '微信已绑定' : '绑定微信'}</span>
        </button>
      `;
      dock.innerHTML = html;
      document.body.appendChild(dock);
    }
  }

  // Periodic DOM check & permission enforcement
  setInterval(refreshUI, 600);
})();
