#!/usr/bin/env python3
"""部署后回归：移动端抽屉（滚动 + 不压扁 + 退出可见）+ 退出确认框可见。

与 verify_mobile_diag3.py 的区别：**不注入任何 CSS**，
直接量线上真实加载的 dsa-wechat.js 生效结果。

判定矩阵（6 档视口高度 x 3 项 + 确认框 1 项 = 19 checks）：
  C1 原生菜单项最小高度 >= 40px      （不再被压扁）
  C2 nav 可滚动：scrollHeight > clientHeight 且 overflowY == auto
  C3 Log out 高度 == 44px 且在视口内
  C4 点 Log out 后视口中心落在确认框内部
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
HEIGHTS = (800, 720, 680, 640, 600, 560)


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


MEASURE_JS = r"""
(() => {
  const d = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!d) return { error: 'no drawer' };
  const nav = d.querySelector('nav');
  if (!nav) return { error: 'no nav' };
  const cs = getComputedStyle(nav);
  const items = [...nav.children].map(el => {
    const r = el.getBoundingClientRect();
    return { label: (el.textContent || '').trim().slice(0, 10) || el.tagName,
             h: Math.round(r.height) };
  });
  // 只看导航项（注入的用户管理 wrapper 会很高，排除掉）
  const navish = items.filter(i => i.h < 200).map(i => i.h);
  const lb = [...d.querySelectorAll('button')]
    .find(b => /log\s*out|退出|登出/i.test(b.textContent || ''));
  let logout = null;
  if (lb) {
    const r = lb.getBoundingClientRect();
    logout = { h: Math.round(r.height), top: Math.round(r.top),
               bottom: Math.round(r.bottom),
               inView: r.bottom <= innerHeight && r.top >= 0 };
  }
  return {
    vh: innerHeight,
    navOverflowY: cs.overflowY,
    navClient: nav.clientHeight,
    navScroll: nav.scrollHeight,
    navScrollable: nav.scrollHeight > nav.clientHeight && cs.overflowY === 'auto',
    minItemH: navish.length ? Math.min(...navish) : null,
    maxItemH: navish.length ? Math.max(...navish) : null,
    logout,
  };
})()
"""

HIT_JS = r"""
(() => {
  const hit = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  const overlay = [...document.querySelectorAll('body > div')].find(el => {
    const cs = getComputedStyle(el);
    return cs.position === 'fixed' && cs.zIndex !== 'auto' &&
           /inset-0/.test(el.className || '') &&
           (el.textContent || '').includes('?');
  });
  const cs = overlay ? getComputedStyle(overlay) : null;
  return {
    overlayFound: !!overlay,
    overlayZ: cs ? cs.zIndex : null,
    centerHitText: hit ? (hit.textContent || '').trim().slice(0, 30) : null,
    insideOverlay: overlay && hit ? overlay.contains(hit) : false,
  };
})()
"""


def setup_viewport(page, h):
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
        "if(t)t.click(); return !!t; })()")
    time.sleep(1.2)


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "regress3"
    page = None
    passed = failed = 0
    fails = []
    try:
        t = http_json("/json/new?about:blank", method="PUT")
        page = Page(t["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.setUserAgentOverride", userAgent=MOBILE_UA)
        page.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)

        print("=== A. 抽屉布局（6 档高度 x C1/C2/C3） ===")
        for h in HEIGHTS:
            setup_viewport(page, h)
            m = page.eval(MEASURE_JS)
            if m.get("error"):
                print(f"  h={h}: {m['error']}")
                fails.append(f"h={h} {m['error']}")
                failed += 3
                continue
            lo = m["logout"]
            c1 = m["minItemH"] is not None and m["minItemH"] >= 40
            c2 = bool(m["navScrollable"])
            c3 = bool(lo) and lo["h"] == 44 and lo["inView"]
            for name, ok in (("C1", c1), ("C2", c2), ("C3", c3)):
                if ok:
                    passed += 1
                else:
                    failed += 1
                    fails.append(f"h={h} {name} fail: {m}")
            print(f"  h={h:<4} C1={'PASS' if c1 else 'FAIL'} C2={'PASS' if c2 else 'FAIL'} "
                  f"C3={'PASS' if c3 else 'FAIL'}  |  nav {m['navClient']}/{m['navScroll']} "
                  f"({m['navOverflowY']}) 项高 {m['minItemH']}~{m['maxItemH']} logout={lo}")

        print("\n=== B. 退出确认框（C4） ===")
        setup_viewport(page, 800)
        page.eval(
            "(() => { const lb=[...document.querySelectorAll('button')]"
            ".find(b=>/log\\s*out|退出|登出/i.test(b.textContent||''));"
            "if(lb)lb.click(); return !!lb; })()")
        time.sleep(1.5)
        hit = page.eval(HIT_JS)
        c4 = bool(hit.get("overlayFound")) and bool(hit.get("insideOverlay"))
        if c4:
            passed += 1
        else:
            failed += 1
            fails.append(f"C4 fail: {hit}")
        print(f"  C4={'PASS' if c4 else 'FAIL'}  overlayZ={hit.get('overlayZ')} "
              f"centerHit='{hit.get('centerHitText')}' insideOverlay={hit.get('insideOverlay')}")
        page.shot(os.path.join(OUTDIR, f"{label}-confirm.png"))

        print(f"\n=== 结论：{passed} PASS / {failed} FAIL（共 {passed + failed} 项）===")
        if fails:
            for f in fails:
                print("  -", f)
            return 1
        print(f"截图: {OUTDIR}/{label}-confirm.png")
        return 0
    finally:
        if page:
            page.close()


if __name__ == "__main__":
    sys.exit(main())
