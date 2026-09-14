#!/usr/bin/env python3
"""确认「退出确认框在移动端不显示」的真正原因。

线索（读源码）：
  · ConfirmDialog  → <div class="fixed inset-0 z-50 ...">  + createPortal(…, document.body)
                     **没有 role 属性**，所以上一版探针用 [role=dialog] 查不到它
  · Drawer         → <div class="fixed inset-0 overflow-hidden" style={{zIndex}} role="presentation">
                     Shell.tsx 没传 zIndex → 默认 50
  两者都是 z-50，谁是赢家取决于 DOM 顺序 / stacking context。

本探针：点击 Log out 后，按 `.fixed.inset-0` 枚举所有全屏浮层，
量 z-index / DOM 顺序 / elementFromPoint 命中者，并截图。
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
            http_proxy_host=None, http_proxy_port=None)
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


OVERLAY_JS = r"""
(() => {
  // 所有 fixed 全屏浮层，外加 ConfirmDialog 的特征节点
  const out = [];
  const all = [...document.querySelectorAll('div')];
  all.forEach((el, idx) => {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed') return;
    const r = el.getBoundingClientRect();
    if (r.width < innerWidth * 0.8 || r.height < innerHeight * 0.8) return;
    out.push({
      idx,
      cls: (el.className || '').slice(0, 70),
      role: el.getAttribute('role'),
      z: cs.zIndex,
      disp: cs.display,
      vis: cs.visibility,
      op: cs.opacity,
      bg: cs.backgroundColor,
      domIndex: [...document.body.children].indexOf(el),
      bodyChild: [...document.body.children].indexOf(el) >= 0,
      text: (el.textContent || '').trim().slice(0, 40),
      rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    });
  });
  // body 直接子节点顺序（portal 会挂在最后）
  const bodyKids = [...document.body.children].map((c, i) => ({
    i, tag: c.tagName,
    cls: (c.className || '').toString().slice(0, 50),
    z: getComputedStyle(c).zIndex,
    pos: getComputedStyle(c).position,
  }));
  // 视口中心命中谁
  const hit = document.elementFromPoint(innerWidth/2, innerHeight/2);
  return {
    overlays: out,
    bodyKids,
    centerHit: hit ? { tag: hit.tagName, cls: (hit.className||'').toString().slice(0,80),
                       text: (hit.textContent||'').trim().slice(0,40) } : null,
    hasConfirmText: /确定要退出|退出登录|Are you sure/i.test(document.body.innerText || ''),
    bodyTextTail: (document.body.innerText || '').trim().slice(-160),
  };
})()
"""


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "confirm"
    page = None
    try:
        t = http_json("/json/new?about:blank", method="PUT")
        page = Page(t["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.setUserAgentOverride", userAgent=MOBILE_UA)
        page.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
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
            "if(t)t.click(); return !!t; })()")
        time.sleep(1.5)
        print("=== 抽屉打开后 ===")
        print(json.dumps(page.eval(OVERLAY_JS), ensure_ascii=False, indent=1))

        page.eval(
            r"""
            (() => {
              const lb = [...document.querySelectorAll('button')]
                .find(b => /log\s*out|退出|登出/i.test(b.textContent || ''));
              if (!lb) return 'no-btn';
              lb.click();
              return 'clicked';
            })()""")
        time.sleep(2)
        print("\n=== 点击 Log out 后 ===")
        m = page.eval(OVERLAY_JS)
        print(json.dumps(m, ensure_ascii=False, indent=1))
        page.shot(os.path.join(OUTDIR, f"{label}-afterlogout.png"))
        print(f"\n截图: {OUTDIR}/{label}-afterlogout.png")
        return 0
    finally:
        if page:
            page.close()


if __name__ == "__main__":
    sys.exit(main())
