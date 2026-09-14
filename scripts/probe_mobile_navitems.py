#!/usr/bin/env python3
"""诊断：移动端抽屉里菜单项被压扁 + 退出确认弹窗不显示。

截图症状（真实手机 1080x2400，管理员）：
  · 首页/问股/选股/持仓/AI建议/回测/告警/用量 —— **全部被压扁成一行行贴在一起**
  · 用户管理/绑定微信/个人通知/设置/主题/界面语言 —— 高度正常
  · 退出 —— 现在可见（但点击后确认弹窗不出现）

关键差异：被压扁的都是**上游原生** NavLink，正常的是**注入层**的 `.dsa-sidebar-item`。
注入项有自己的 `display:flex; align-items:center`，不依赖 flex-shrink；
原生项靠 `h-[var(--nav-item-height)]`，一旦父容器空间不足就被 flexShrink 压扁。

本探针：
  1. 枚举抽屉里 nav 的每个子项，量各自 height / flexShrink / 是否被压缩
  2. 点击「退出」，量 ConfirmDialog 是否存在、几何是否可见
"""
import base64
import json
import os
import sys
import time
import urllib.request

import websocket

HTTP = "http://127.0.0.1:9222"
APP = os.environ.get("DSA_URL", "http://127.0.0.1:8000/")
TOKEN = open("/tmp/dsa_admin_token").read().strip()
LABEL = sys.argv[1] if len(sys.argv) > 1 else "diag2"
OUTDIR = "/tmp/shots"
os.makedirs(OUTDIR, exist_ok=True)

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; 2210132C) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)


def http_json(path, method="GET"):
    req = urllib.request.Request(HTTP + path, method=method)
    with urllib.request.urlopen(req, timeout=20) as fh:
        return json.loads(fh.read().decode("utf-8", "replace"))


class Page:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(
            ws_url, suppress_origin=True, timeout=30,
            http_proxy_host=None, http_proxy_port=None,
        )
        self._id = 0

    def send(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            raw = self.ws.recv()
            if not raw:
                raise RuntimeError("ws closed")
            d = json.loads(raw)
            if d.get("id") == mid:
                if "error" in d:
                    raise RuntimeError(f"{method}: {d['error']}")
                return d.get("result", {})

    def eval(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True)
        return r.get("result", {}).get("value")

    def shot(self, path):
        r = self.send("Page.captureScreenshot", format="png")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(r["data"]))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


ITEMS_JS = r"""
(() => {
  const dialog = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!dialog) return { error: 'no dialog' };
  const nav = dialog.querySelector('nav');
  if (!nav) return { error: 'no nav' };
  const navCS = getComputedStyle(nav);
  const out = {
    nav: {
      flexGrow: navCS.flexGrow,
      flexShrink: navCS.flexShrink,
      flexBasis: navCS.flexBasis,
      overflowY: navCS.overflowY,
      scrollHeight: nav.scrollHeight,
      clientHeight: nav.clientHeight,
      offsetHeight: nav.offsetHeight,
      display: navCS.display,
      childCount: nav.children.length,
    },
    items: [],
  };
  // 逐个子项量几何
  for (const el of nav.children) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const label = (el.textContent || '').trim().slice(0, 14) || el.tagName;
    out.items.push({
      label,
      tag: el.tagName,
      cls0: (el.className || '').split(' ').slice(0, 3).join(' '),
      isInjected: (el.className || '').includes('dsa-sidebar-item'),
      h: Math.round(r.height),
      w: Math.round(r.width),
      top: Math.round(r.top),
      flexShrink: cs.flexShrink,
      flexGrow: cs.flexGrow,
      minHeight: cs.minHeight,
      cssHeight: cs.height,
      display: cs.display,
    });
  }
  return out;
})()
"""


def open_drawer(page):
    page.eval(
        r"""
        (() => {
          const bs = [...document.querySelectorAll('button')].filter(b =>
            b.querySelector('svg') && (b.className || '').includes('pointer-events-auto'));
          const t = bs.find(b => (b.getAttribute('aria-label') || '').includes('导航')) || bs[0];
          if (t) t.click();
          return !!t;
        })()
        """
    )
    time.sleep(2)


def main():
    page = None
    try:
        t = http_json("/json/new?about:blank", method="PUT")
        page = Page(t["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.setUserAgentOverride", userAgent=MOBILE_UA)
        page.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        # 贴近日志里那台真机：1080x2400 @ DPR3 → CSS 视口 360x800
        page.send(
            "Emulation.setDeviceMetricsOverride",
            width=360, height=800, deviceScaleFactor=3, mobile=True,
        )
        page.send("Page.navigate", url=APP)
        time.sleep(3)
        page.eval(f"document.cookie = 'dsa_user_token={TOKEN}; path=/';")
        page.send("Page.navigate", url=APP)
        time.sleep(4)

        open_drawer(page)
        m = page.eval(ITEMS_JS)
        print("=== 抽屉 nav 与逐项几何 ===")
        print(json.dumps(m.get("nav"), ensure_ascii=False, indent=2))
        print("\n逐项（重点看 isInjected=false 的那些）:")
        print(f"{'label':<16}{'inj':<6}{'h':<6}{'shrink':<8}{'cssH':<10}{'minH':<8}{'display'}")
        for it in m.get("items", []):
            print(f"{it['label']:<16}{str(it['isInjected']):<6}{it['h']:<6}"
                  f"{it['flexShrink']:<8}{it['cssHeight']:<10}{it['minHeight']:<8}{it['display']}")

        page.shot(os.path.join(OUTDIR, f"{LABEL}-drawer.png"))

        # --- 点「退出」，看确认弹窗 ---
        print("\n=== 点击「退出」后的确认弹窗 ===")
        page.eval(
            r"""
            (() => {
              const d = document.querySelector('[role="dialog"][aria-modal="true"]');
              if (!d) return 'no-drawer';
              const nav = d.querySelector('nav');
              let btn = null;
              if (nav) { let n = nav.nextElementSibling;
                while (n && n.tagName !== 'BUTTON') n = n.nextElementSibling;
                if (n) btn = n; }
              if (!btn) btn = [...d.querySelectorAll('button')].find(b =>
                /log\s*out|退出|登出/i.test(b.textContent || ''));
              if (!btn) return 'no-logout-btn';
              btn.click();
              return 'clicked:' + (btn.textContent || '').trim().slice(0, 12);
            })()
            """
        )
        time.sleep(2)

        dlg = page.eval(
            r"""
            (() => {
              const all = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')];
              return all.map(d => {
                const r = d.getBoundingClientRect();
                const cs = getComputedStyle(d);
                return {
                  modal: d.getAttribute('aria-modal'),
                  text: (d.textContent || '').trim().slice(0, 60),
                  display: cs.display,
                  visibility: cs.visibility,
                  opacity: cs.opacity,
                  zIndex: cs.zIndex,
                  pos: cs.position,
                  rect: { x: Math.round(r.left), y: Math.round(r.top),
                          w: Math.round(r.width), h: Math.round(r.height) },
                  inViewport: r.width > 0 && r.height > 0 &&
                              r.top < innerHeight && r.bottom > 0,
                };
              });
            })()
            """
        )
        print(json.dumps(dlg, ensure_ascii=False, indent=2))
        page.shot(os.path.join(OUTDIR, f"{LABEL}-confirm.png"))
        print(f"\n截图: {OUTDIR}/{LABEL}-drawer.png / {LABEL}-confirm.png")
        return 0
    finally:
        if page:
            page.close()


if __name__ == "__main__":
    sys.exit(main())
