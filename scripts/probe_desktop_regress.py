#!/usr/bin/env python3
"""桌面端回归：确认 #3 的移动端修复没有影响桌面侧边栏。

桌面走 <aside>（不是 [role=dialog]），本次只改了
`[role="dialog"][aria-modal="true"] nav ...` 与移动端断点内的
confirm 层 z-index，理论上完全不影响桌面。本探针实测验证。

判定（1440x900）：
  D1 aside 可见（display != none）
  D2 aside 里的 nav 具备滚动能力：overflow-y == auto
     ⚠️ 不要求 scrollHeight > clientHeight —— 桌面高度充足时内容正好放下，
        没有溢出才是正常态（溢出只在矮窗口/菜单项多时才出现）。
  D3 aside 里的退出按钮 height >= 40px 且在视口内
  D4 抽屉（[role=dialog]）在桌面**不存在**（不应被意外显示）
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
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
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


JS = r"""
(() => {
  const aside = document.querySelector('aside');
  const as = aside ? getComputedStyle(aside) : null;
  const ar = aside ? aside.getBoundingClientRect() : null;
  const nav = aside ? aside.querySelector('nav') : null;
  const ncs = nav ? getComputedStyle(nav) : null;
  let logout = null;
  if (aside) {
    const lb = [...aside.querySelectorAll('button')]
      .find(b => /log\s*out|退出|登出/i.test(b.textContent || ''));
    if (lb) {
      const r = lb.getBoundingClientRect();
      logout = { h: Math.round(r.height), top: Math.round(r.top),
                 bottom: Math.round(r.bottom),
                 inView: r.bottom <= innerHeight && r.top >= 0 };
    }
  }
  const drawer = document.querySelector('[role="dialog"][aria-modal="true"]');
  return {
    vw: innerWidth, vh: innerHeight,
    asideExists: !!aside,
    asideDisplay: as ? as.display : null,
    asideW: ar ? Math.round(ar.width) : null,
    navOverflowY: ncs ? ncs.overflowY : null,
    navClient: nav ? nav.clientHeight : null,
    navScroll: nav ? nav.scrollHeight : null,
    navScrollable: nav ? (nav.scrollHeight > nav.clientHeight && ncs.overflowY === 'auto') : null,
    logout,
    drawerPresent: !!drawer,
  };
})()
"""


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "desktop"
    page = None
    try:
        t = http_json("/json/new?about:blank", method="PUT")
        page = Page(t["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.setUserAgentOverride", userAgent=DESKTOP_UA)
        page.send("Emulation.setDeviceMetricsOverride",
                  width=1440, height=900, deviceScaleFactor=1, mobile=False)
        page.send("Page.navigate", url=APP)
        time.sleep(2)
        page.eval(f"document.cookie = 'dsa_user_token={TOKEN}; path=/';")
        page.send("Page.navigate", url=APP)
        time.sleep(4)

        m = page.eval(JS)
        print("=== 桌面 1440x900 ===")
        print(json.dumps(m, ensure_ascii=False, indent=1))

        lo = m["logout"]
        checks = [
            ("D1 aside 可见", m["asideExists"] and m["asideDisplay"] not in (None, "none"),
             f"display={m['asideDisplay']}"),
            ("D2 aside nav 具备滚动能力", m["navOverflowY"] == "auto",
             f"{m['navClient']}/{m['navScroll']} overflowY={m['navOverflowY']}"),
            ("D3 退出按钮 >=40px 且在视口内", bool(lo) and lo["h"] >= 40 and lo["inView"],
             f"{lo}"),
            ("D4 桌面无抽屉", m["drawerPresent"] is False, f"drawer={m['drawerPresent']}"),
        ]
        ok = True
        for name, cond, detail in checks:
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
            ok = ok and cond
        page.shot(os.path.join(OUTDIR, f"{label}-desktop.png"))
        print(f"\n{'全部通过' if ok else '存在问题'}   截图: {OUTDIR}/{label}-desktop.png")
        return 0 if ok else 1
    finally:
        if page:
            page.close()


if __name__ == "__main__":
    sys.exit(main())
