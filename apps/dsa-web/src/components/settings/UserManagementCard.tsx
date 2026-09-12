import type React from 'react';
import { useEffect, useState } from 'react';
import { Users, UserPlus, Trash2, KeyRound, CheckCircle2, XCircle, RefreshCw } from 'lucide-react';
import { tenancyApi, type TenantUserPublic } from '../../api/tenancy';
import { Button, Input, Badge } from '../common';
import { SettingsSectionCard } from './SettingsSectionCard';
import { SettingsAlert } from './SettingsAlert';

export const UserManagementCard: React.FC = () => {
  const [users, setUsers] = useState<TenantUserPublic[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // New user form state
  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newDisplayName, setNewDisplayName] = useState('');
  const [newRole, setNewRole] = useState<'user' | 'admin'>('user');
  const [isCreating, setIsCreating] = useState(false);

  const loadUsers = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await tenancyApi.listUsers();
      setUsers(res.users || []);
    } catch (err: any) {
      setError(err?.response?.data?.message || '加载用户列表失败，请确认您具有管理员权限');
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newUsername.trim() || !newPassword.trim()) {
      setError('用户名和密码不能为空');
      return;
    }
    setIsCreating(true);
    setError(null);
    setSuccess(null);
    try {
      await tenancyApi.createUser({
        username: newUsername.trim(),
        password: newPassword,
        display_name: newDisplayName.trim() || newUsername.trim(),
        role: newRole,
      });
      setSuccess(`成功为用户 [${newUsername}] 开通账号！`);
      setNewUsername('');
      setNewPassword('');
      setNewDisplayName('');
      setNewRole('user');
      await loadUsers();
    } catch (err: any) {
      setError(err?.response?.data?.message || '开通用户账号失败');
    } finally {
      setIsCreating(false);
    }
  };

  const handleDeleteUser = async (user: TenantUserPublic) => {
    if (user.is_system || user.username === 'admin') {
      alert('系统内置账号不可删除');
      return;
    }
    if (!window.confirm(`确认删除用户 [${user.username}] 吗？该操作不可撤回。`)) return;

    try {
      await tenancyApi.deleteUser(user.id);
      setSuccess(`用户 [${user.username}] 已删除`);
      await loadUsers();
    } catch (err: any) {
      setError(err?.response?.data?.message || '删除用户失败');
    }
  };

  const handleResetPassword = async (user: TenantUserPublic) => {
    const pwd = window.prompt(`请输入用户 [${user.username}] 的新密码（至少 6 位）：`);
    if (!pwd || !pwd.trim()) return;

    try {
      await tenancyApi.resetPassword(user.id, pwd.trim());
      setSuccess(`用户 [${user.username}] 密码重置成功`);
    } catch (err: any) {
      setError(err?.response?.data?.message || '重置密码失败');
    }
  };

  useEffect(() => {
    loadUsers();
  }, []);

  return (
    <SettingsSectionCard
      title="用户管理中心（邀请开通制）"
      description="由管理员统一为量化分析团队成员开通账号，并监控微信端绑定与激活状态"
      icon={Users}
    >
      {error && <SettingsAlert variant="error" title="错误" message={error} className="mb-4" />}
      {success && <SettingsAlert variant="success" title="成功" message={success} className="mb-4" />}

      {/* Create User Form */}
      <form onSubmit={handleCreateUser} className="p-4 rounded-xl border border-border/80 bg-card/60 mb-6 space-y-4">
        <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
          <UserPlus className="w-4 h-4 text-primary" />
          <span>开通新成员账号</span>
        </h4>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-3">
          <div>
            <label className="block text-xs text-muted-text mb-1">用户名 *</label>
            <input
              type="text"
              className="w-full px-3 py-1.5 rounded-lg border border-border bg-background text-foreground text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              placeholder="例如: zhangsan"
              value={newUsername}
              onChange={(e) => setNewUsername(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="block text-xs text-muted-text mb-1">初始密码 *</label>
            <input
              type="password"
              className="w-full px-3 py-1.5 rounded-lg border border-border bg-background text-foreground text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              placeholder="至少6位密码"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="block text-xs text-muted-text mb-1">显示昵称</label>
            <input
              type="text"
              className="w-full px-3 py-1.5 rounded-lg border border-border bg-background text-foreground text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              placeholder="例如: 张三"
              value={newDisplayName}
              onChange={(e) => setNewDisplayName(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs text-muted-text mb-1">角色权限</label>
            <select
              className="w-full px-3 py-1.5 rounded-lg border border-border bg-background text-foreground text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              value={newRole}
              onChange={(e) => setNewRole(e.target.value as 'user' | 'admin')}
            >
              <option value="user">普通用户 (量化投研)</option>
              <option value="admin">管理员 (系统管理)</option>
            </select>
          </div>
        </div>
        <div className="flex justify-end pt-1">
          <Button type="submit" size="sm" variant="primary" disabled={isCreating}>
            {isCreating ? '正在开通...' : '➕ 开通账号'}
          </Button>
        </div>
      </form>

      {/* User Table */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-sm font-semibold text-foreground">系统用户列表 ({users.length})</h4>
          <Button size="sm" variant="ghost" onClick={loadUsers} disabled={isLoading} title="刷新用户列表">
            <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
          </Button>
        </div>

        <div className="overflow-x-auto rounded-xl border border-border/80">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="border-b border-border/70 bg-muted/40 text-muted-text">
                <th className="p-2.5">ID</th>
                <th className="p-2.5">用户名</th>
                <th className="p-2.5">昵称</th>
                <th className="p-2.5">角色</th>
                <th className="p-2.5">微信绑定状态</th>
                <th className="p-2.5">创建时间</th>
                <th className="p-2.5 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-b border-border/50 hover:bg-muted/20 transition-colors">
                  <td className="p-2.5 font-mono text-muted-text">{u.id}</td>
                  <td className="p-2.5 font-semibold text-foreground">{u.username}</td>
                  <td className="p-2.5">{u.display_name}</td>
                  <td className="p-2.5">
                    <Badge variant={u.role === 'admin' ? 'primary' : 'neutral'}>
                      {u.role === 'admin' ? '管理员' : '普通用户'}
                    </Badge>
                  </td>
                  <td className="p-2.5">
                    {u.wechat_bound ? (
                      <span className="inline-flex items-center gap-1 text-emerald-500 font-medium">
                        <CheckCircle2 className="w-3.5 h-3.5" />
                        已绑定: {u.wechat_nickname || u.wechat_id}
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-muted-text">
                        <XCircle className="w-3.5 h-3.5" />
                        未绑定
                      </span>
                    )}
                  </td>
                  <td className="p-2.5 text-muted-text">
                    {u.created_at ? u.created_at.substring(0, 16) : '-'}
                  </td>
                  <td className="p-2.5 text-right space-x-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => handleResetPassword(u)}
                      title="重置密码"
                      className="p-1 h-7"
                    >
                      <KeyRound className="w-3.5 h-3.5 text-amber-500" />
                    </Button>
                    {!u.is_system && u.username !== 'admin' && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => handleDeleteUser(u)}
                        title="删除用户"
                        className="p-1 h-7 hover:text-red-500"
                      >
                        <Trash2 className="w-3.5 h-3.5 text-red-500" />
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr>
                  <td colSpan={7} className="p-6 text-center text-muted-text">
                    暂无用户记录
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </SettingsSectionCard>
  );
};
