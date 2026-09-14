#!/usr/bin/env python3
"""诊断 #3：移动端菜单被压扁 + 退出确认框不出现。

前置结论（probe_mobile_navitems.py before3 实测，360x800）：
  · 该视口下渲染**正常**，截图里 12 项都清晰、Log out 可见
  · 所以「压扁」只在**更矮的可用视口**下复现 —— 用户真机
    1080x2400 @DPR3 = 360x800 CSS，但微信内置浏览器/Chrome 的
    地址栏 + 底部工具栏会吃掉约 100~160px，真实可用高度 ≈ 640~700px
  · 点 Log out 后 DOM 里**仍然只有一个 [role=dialog]**（就是抽屉本身），
    说明确认框没出现 —— 需要区分「点击没生效」还是「弹窗没渲染」

本探针做两件事：
  A. 在 5 档高度下量 nav 是否溢出、子项是否被压缩（找出压扁阈值）
  B. 用真实事件序列点 Log out，逐步 dump dialog 数量与可见性
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


NAV_JS = r"""
(() => {
  const d = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!d) return { error: 'no dialog' };
  const nav = d.querySelector('nav');
  if (!nav) return { error: 'no nav' };
  const items = [];
  for (const el of nav.children) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    items.push({
      label: (el.textContent || '').trim().slice(0, 10) || el.tagName,
      h: Math.round(r.height),
      top: Math.round(r.top),
      cssH: cs.height,
      shrink: cs.flexShrink,
      minH: cs.minHeight,
    });
  }
  // 退出按钮是 nav 的兄弟
  let logout = null;
  const all = [...d.querySelectorAll('button')];
  const lb = all.find(b => /log\s*out|退出|登出/i.test(b.textContent || ''));
  if (lb) {
    const r = lb.getBoundingClientRect();
    logout = { h: Math.round(r.height), top: Math.round(r.top),
               bottom: Math.round(r.bottom), text: (lb.textContent||'').trim() };
  }
  return {
    vh: innerHeight,
    nav: { clientHeight: nav.clientHeight, scrollHeight: nav.scrollHeight,
           offsetHeight: nav.offsetHeight,
           overflowY: getComputedStyle(nav).overflowY,
           overflow: nav.scrollHeight - nav.clientHeight,
           flex: getComputedStyle(nav).flex },
    content: (() => {
      const p = nav.parentElement;
      const r = p.getBoundingClientRect();
      return { h: Math.round(r.height), scrollH: p.scrollHeight,
               clientH: p.clientHeight, overflow: getComputedStyle(p).overflowY,
               over: p.scrollHeight - p.clientHeight };
    })(),
    logout,
    items,
  };
})()
"""


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "diag3"
    page = None
    try:
        t = http_json("/json/new?about:blank", method="PUT")
        page = Page(t["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.setUserAgentOverride", userAgent=MOBILE_UA)
        page.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)

        print("=== A. 不同视口高度下的 nav 压缩情况 ===")
        for h in (800, 720, 680, 640, 600, 560):
            page.send("Emulation.setDeviceMetricsOverride",
                      width=360, height=h, deviceScaleFactor=1, mobile=True)
            page.send("Page.navigate", url=APP)
            time.sleep(2)
            page.eval(f"document.cookie = 'dsa_user_token={TOKEN}; path=/';")
            page.send("Page.navigate", url=APP)
            time.sleep(3)
            page.eval(
                "(() => { const bs=[...document.querySelectorAll('button')].filter(b=>b.querySelector('svg'));"
                "const t=bs.find(b=>(b.getAttribute('aria-label')||'').match(/导航|navigation/i))||bs[0];"
                "if(t)t.click(); return !!t; })()"
            )
            time.sleep(1.5)
            m = page.eval(NAV_JS)
            if m.get("error"):
                print(f"  h={h}: {m['error']}")
                continue
            hs = [i["h"] for i in m["items"]]
            squashed = sum(1 for x in hs if x < 36)
            print(f"  h={h:<5} vh={m['vh']:<5} nav.client={m['nav']['clientHeight']:<5} "
                  f"scroll={m['nav']['scrollHeight']:<5} over={m['nav']['overflow']:<5} "
                  f"父溢出={m['content']['over']:<5} 子项h={hs} 压扁数={squashed} "
                  f"logout={m['logout']}")

        # --- B. 点 Log out 的完整事件序列 ---
        print("\n=== B. 点击 Log out 后的 dialog 变化 ===")
        page.send("Emulation.setDeviceMetricsOverride",
                  width=360, height=800, deviceScaleFactor=1, mobile=True)
        page.send("Page.navigate", url=APP)
        time.sleep(2)
        page.eval(f"document.cookie = 'dsa_user_token={TOKEN}; path=/';")
        page.send("Page.navigate", url=APP)
        time.sleep(3)
        page.eval(
            "(() => { const bs=[...document.querySelectorAll('button')].filter(b=>b.querySelector('svg'));"
            "const t=bs.find(b=>(b.getAttribute('aria-label')||'').match(/导航|navigation/i))||bs[0];"
            "if(t)t.click(); return !!t; })()"
        )
        time.sleep(1.5)

        dump_js = r"""
        (() => {
          const all = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')];
          return { count: all.length, list: all.map(d => {
            const r = d.getBoundingClientRect();
            const cs = getComputedStyle(d);
            return { role: d.getAttribute('role'), modal: d.getAttribute('aria-modal'),
                     text: (d.textContent||'').trim().slice(0,50),
                     disp: cs.display, vis: cs.visibility, op: cs.opacity,
                     z: cs.zIndex, pos: cs.position,
                     rect: [Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)] };
          })};
        })()
        """
        print("  点击前:", json.dumps(page.eval(dump_js), ensure_ascii=False))

        clicked = page.eval(
            r"""
            (() => {
              const d = document.querySelector('[role="dialog"][aria-modal="true"]');
              if (!d) return 'no-drawer';
              const lb = [...d.querySelectorAll('button')]
                .find(b => /log\s*out|退出|登出/i.test(b.textContent || ''));
              if (!lb) return 'no-logout-btn';
              lb.scrollIntoView({block:'center'});
              const r = lb.getBoundingClientRect();
              const cx = r.left + r.width/2, cy = r.top + r.height/2;
              const hit = document.elementFromPoint(cx, cy);
              lb.click();
              return 'clicked rect=' + JSON.stringify([Math.round(r.left),Math.round(r.top),
                     Math.round(r.width),Math.round(r.height)]) +
                     ' hit=' + (hit ? hit.tagName + '.' + (hit.className||'').split(' ')[0] : 'null') +
                     ' hitIsBtn=' + (hit === lb || (hit && lb.contains(hit)));
            })()
            """
        )
        print("  点击动作:", clicked)
        time.sleep(2)
        print("  点击后:", json.dumps(page.eval(dump_js), ensure_ascii=False))
        page.shot(os.path.join(OUTDIR, f"{label}-afterclick.png"))
        print(f"\n截图: {OUTDIR}/{label}-afterclick.png")
        return 0
    finally:
        if page:
            page.close()


if __name__ == "__main__":
    sys.exit(main())
