import apiClient from './index';

export interface TenancyCapabilities {
  multiuser_enabled: boolean;
  supported_setting_keys: string[];
  auth: {
    cookie_name: string;
    bearer_header: string;
    login_endpoint: string;
    token_endpoint: string;
  };
}

export interface TenantUserPublic {
  id: number;
  username: string;
  display_name: string;
  role: 'admin' | 'user';
  status: string;
  is_system: boolean;
  wechat_id?: string | null;
  wechat_nickname?: string | null;
  wechat_bound: boolean;
  wechat_bound_at?: string | null;
  created_at: string;
  updated_at: string;
  last_login_at?: string | null;
}

export interface CreateUserPayload {
  username: string;
  password: string;
  display_name?: string;
  role?: 'admin' | 'user';
}

export interface WechatQrResponse {
  status: string;
  qrcode_url?: string;
  qr_image?: string;
  qr_status?: 'wait' | 'scaned' | 'expired' | 'confirmed';
}

export const tenancyApi = {
  async getCapabilities(): Promise<TenancyCapabilities> {
    const { data } = await apiClient.get<TenancyCapabilities>('/api/v1/tenancy/capabilities');
    return data;
  },

  async getMe(): Promise<TenantUserPublic> {
    const { data } = await apiClient.get<TenantUserPublic>('/api/v1/tenancy/auth/me');
    return data;
  },

  async listUsers(): Promise<{ users: TenantUserPublic[]; total: number }> {
    const { data } = await apiClient.get<{ users: TenantUserPublic[]; total: number }>('/api/v1/tenancy/users');
    return data;
  },

  async createUser(payload: CreateUserPayload): Promise<TenantUserPublic> {
    const { data } = await apiClient.post<TenantUserPublic>('/api/v1/tenancy/users', payload);
    return data;
  },

  async deleteUser(userId: number): Promise<void> {
    await apiClient.delete(`/api/v1/tenancy/users/${userId}`);
  },

  async resetPassword(userId: number, newPassword: string): Promise<void> {
    await apiClient.post(`/api/v1/tenancy/users/${userId}/reset-password`, {
      new_password: newPassword,
    });
  },

  async unbindWechat(): Promise<void> {
    await apiClient.post('/api/v1/tenancy/auth/wechat-unbind');
  },

  async getWechatQr(): Promise<WechatQrResponse> {
    const { data } = await apiClient.get<WechatQrResponse>('/api/v1/tenancy/wechat/qrlogin');
    return data;
  },

  async pollWechatQr(): Promise<WechatQrResponse> {
    const { data } = await apiClient.post<WechatQrResponse>('/api/v1/tenancy/wechat/qrlogin', {
      action: 'poll',
    });
    return data;
  },
};
