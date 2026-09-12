import type React from 'react';
import { useEffect, useState, useRef } from 'react';
import { QrCode, RefreshCw, Smartphone, ShieldCheck, CheckCircle2, AlertCircle, Unlink } from 'lucide-react';
import { tenancyApi, type TenantUserPublic, type WechatQrResponse } from '../../api/tenancy';
import { Button, Badge } from '../common';
import { SettingsSectionCard } from './SettingsSectionCard';
import { SettingsAlert } from './SettingsAlert';

export const WechatBindingCard: React.FC = () => {
  const [me, setMe] = useState<TenantUserPublic | null>(null);
  const [qrData, setQrData] = useState<WechatQrResponse | null>(null);
  const [qrStatus, setQrStatus] = useState<string>('wait');
  const [isLoadingQr, setIsLoadingQr] = useState(false);
  const [isUnbinding, setIsUnbinding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const pollTimerRef = useRef<number | null>(null);

  const loadUserData = async () => {
    try {
      const user = await tenancyApi.getMe();
      setMe(user);
    } catch {
      // ignore
    }
  };

  const fetchQr = async () => {
    setIsLoadingQr(true);
    setError(null);
    try {
      const res = await tenancyApi.getWechatQr();
      setQrData(res);
      setQrStatus('wait');
      startPolling();
    } catch (err: any) {
      setError(err?.response?.data?.message || '获取微信登录二维码失败，请检查 CowAgent 通道');
    } finally {
      setIsLoadingQr(false);
    }
  };

  const startPolling = () => {
    stopPolling();
    pollTimerRef.current = window.setInterval(async () => {
      try {
        const pollRes = await tenancyApi.pollWechatQr();
        if (pollRes?.qr_status) {
          setQrStatus(pollRes.qr_status);
          if (pollRes.qr_status === 'confirmed' || pollRes.qr_status === 'expired') {
            stopPolling();
            if (pollRes.qr_status === 'confirmed') {
              setSuccess('微信通道连接成功！请按照提示在微信内发送绑定指令完成账号激活。');
              loadUserData();
            }
          }
        }
      } catch {
        // continue polling
      }
    }, 2500);
  };

  const stopPolling = () => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  const handleUnbind = async () => {
    if (!window.confirm('确定要解除当前绑定的微信号吗？解绑后微信端将无法查询量化分析数据。')) return;
    setIsUnbinding(true);
    setError(null);
    try {
      await tenancyApi.unbindWechat();
      setSuccess('微信绑定已解除');
      await loadUserData();
      fetchQr();
    } catch (err: any) {
      setError(err?.response?.data?.message || '解绑失败');
    } finally {
      setIsUnbinding(false);
    }
  };

  useEffect(() => {
    loadUserData();
    fetchQr();
    return () => stopPolling();
  }, []);

  return (
    <SettingsSectionCard
      title="微信量化投研助手绑定"
      description="扫码连接 WeChat ClawBot 通道，并在微信聊天窗口发送激活指令绑定您的量化账号"
      icon={QrCode}
    >
      {error && <SettingsAlert variant="error" title="操作失败" message={error} className="mb-4" />}
      {success && <SettingsAlert variant="success" title="成功" message={success} className="mb-4" />}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-center">
        {/* QR Code Container */}
        <div className="flex flex-col items-center justify-center p-6 border border-border/70 rounded-2xl bg-card/60 backdrop-blur-sm shadow-inner">
          <div className="relative w-56 h-56 rounded-xl overflow-hidden border border-border/80 flex items-center justify-center bg-background/90 shadow-md">
            {isLoadingQr ? (
              <div className="flex flex-col items-center gap-2 text-muted-text">
                <RefreshCw className="w-8 h-8 animate-spin text-primary" />
                <span className="text-xs">加载二维码中...</span>
              </div>
            ) : qrData?.qr_image ? (
              <div className="relative w-full h-full">
                <img
                  src={qrData.qr_image}
                  alt="WeChat QR Code"
                  className={`w-full h-full object-contain p-2 ${qrStatus === 'expired' ? 'filter blur-sm grayscale' : ''}`}
                />
                {qrStatus === 'expired' && (
                  <div
                    onClick={fetchQr}
                    className="absolute inset-0 bg-black/60 flex flex-col items-center justify-center cursor-pointer text-white gap-2 transition hover:bg-black/70"
                  >
                    <RefreshCw className="w-8 h-8 text-amber-400 animate-pulse" />
                    <span className="text-xs font-semibold">二维码已失效，点击刷新</span>
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-col items-center gap-2 text-muted-text">
                <AlertCircle className="w-8 h-8 text-amber-500" />
                <span className="text-xs">未获取到二维码</span>
                <Button size="sm" variant="secondary" onClick={fetchQr}>重新获取</Button>
              </div>
            )}
          </div>

          {/* QR Status Pill */}
          <div className="mt-4 flex items-center gap-2">
            {qrStatus === 'wait' && <Badge variant="neutral">⏳ 等待手机微信扫码...</Badge>}
            {qrStatus === 'scaned' && <Badge variant="warning">📲 已扫码，请在微信端点击确认</Badge>}
            {qrStatus === 'confirmed' && <Badge variant="success">✅ 微信通道已连接</Badge>}
            {qrStatus === 'expired' && <Badge variant="error">⚠️ 二维码已过期</Badge>}

            <Button size="sm" variant="ghost" onClick={fetchQr} disabled={isLoadingQr} title="手动刷新二维码">
              <RefreshCw className={`w-3.5 h-3.5 ${isLoadingQr ? 'animate-spin' : ''}`} />
            </Button>
          </div>
        </div>

        {/* Instructions & Status */}
        <div className="space-y-4 text-sm">
          {/* Current user bind status */}
          <div className="p-4 rounded-xl border border-border/80 bg-background/50 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-muted-text">当前 Web 账号：</span>
              <span className="font-semibold text-foreground">{me?.username || '未登录'} ({me?.display_name || '用户'})</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-muted-text">微信绑定状态：</span>
              {me?.wechat_bound ? (
                <span className="inline-flex items-center gap-1.5 text-emerald-500 font-medium">
                  <CheckCircle2 className="w-4 h-4" />
                  已绑定 ({me.wechat_nickname || me.wechat_id || '微信用户'})
                </span>
              ) : (
                <span className="text-amber-500 font-medium">尚未绑定微信</span>
              )}
            </div>
            {me?.wechat_bound_at && (
              <div className="flex items-center justify-between text-xs text-muted-text">
                <span>绑定时间：</span>
                <span>{me.wechat_bound_at}</span>
              </div>
            )}
            {me?.wechat_bound && (
              <div className="pt-2 border-t border-border/60 flex justify-end">
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={handleUnbind}
                  disabled={isUnbinding}
                  className="flex items-center gap-1.5"
                >
                  <Unlink className="w-3.5 h-3.5" />
                  <span>{isUnbinding ? '正在解绑...' : '解除微信绑定'}</span>
                </Button>
              </div>
            )}
          </div>

          {/* Activation Step Guide */}
          <div className="p-4 rounded-xl border border-primary/20 bg-primary/5 space-y-2">
            <h4 className="font-semibold text-foreground flex items-center gap-2">
              <Smartphone className="w-4 h-4 text-primary" />
              <span>微信端激活指引</span>
            </h4>
            <ol className="list-decimal list-inside text-xs text-muted-text space-y-1.5 leading-relaxed">
              <li>使用手机微信扫描左侧二维码，关注或打开 ClawBot 智能助手；</li>
              <li>
                在微信聊天框中直接发送激活指令：<br />
                <code className="px-1.5 py-0.5 rounded bg-card border border-border font-mono text-emerald-400">
                  登录 {me?.username || '<账号>'} &lt;您的密码&gt;
                </code>
              </li>
              <li>系统校验通过后将自动完成双向绑定，即可在微信端直接发送股票代码咨询量化投研报告！</li>
            </ol>
          </div>
        </div>
      </div>
    </SettingsSectionCard>
  );
};
