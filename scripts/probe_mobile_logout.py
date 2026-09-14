#!/usr/bin/env python3
"""诊断探针：移动端抽屉里「退出按钮」为何不可见。

在**移动端视口**（390x844，设备模拟）下打开汉堡菜单，然后量：
  1. 抽屉内容容器（role=dialog > .flex-1.overflow-y-auto）的几何
  2. SidebarNav 根节点（.flex.h-full.flex-col）的几何 —— 重点看 scrollHeight vs clientHeight
  3. nav（.flex.flex-col）的几何与 computed flex-grow
  4. 退出按钮的几何与「是否在可视区内」

判定：
  - 若 nav 的 computed flex-grow != 0 且内容容器 scrollHeight > clientHeight
    → 证实「flex-1 的 nav 吃光高度，退出按钮被挤出」
  - 若按钮 getBoundingClientRect().bottom > 容器 bottom
    → 证实按钮在可视区之外（需要滚动才看得到）

用法（服务器上，需先 launch_chromium.sh + /tmp/dsa_admin_token）：
  python3 /tmp/probe_mobile_logout.py [label]
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
LABEL = sys.argv[1] if len(sys.argv) > 1 else "mobile-logout"
OUTDIR = "/tmp/shots"
os.makedirs(OUTDIR, exist_ok=True)

# 移动端视口 + 触摸模拟。注意 DSA 侧边栏的断点是 Tailwind lg=1024px，
# 取 390 宽确保落在移动端分支。
MOBILE_W, MOBILE_H = 390, 844
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def http_json(path, method="GET"):
    req = urllib.request.Request(HTTP + path, method=method)
    with urllib.request.urlopen(req, timeout=20) as fh:
        body = fh.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except Exception:
        return {"raw": body}


class Page:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(
            ws_url, suppress_origin=True, timeout=30,
            http_proxy_host=None, http_proxy_port=None,
        )
        self._id = 0

    def send(self, method, **params):
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            raw = self.ws.recv()
            if not raw:
                raise RuntimeError("ws closed")
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})

    def eval(self, expr, await_promise=False):
        r = self.send(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
        )
        return r.get("result", {}).get("value")

    def shot(self, path):
        r = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(r["data"]))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def new_page():
    t = http_json("/json/new?about:blank", method="PUT")
    return Page(t["webSocketDebuggerUrl"])


# --- 探测用的 JS：一次性取回所有需要的几何 ---
MEASURE_JS = r"""
(() => {
  const out = {};
  const dialog = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!dialog) { out.error = 'no dialog (drawer not open?)'; return out; }

  const scroller = dialog.querySelector('.overflow-y-auto') || dialog;
  const nav = dialog.querySelector('nav');
  const root = nav ? nav.parentElement : null;
  // 退出按钮：nav 之后的那个 button（SidebarNav 里 logout 在 nav 外）
  let logout = null;
  if (nav) {
    let n = nav.nextElementSibling;
    while (n && n.tagName !== 'BUTTON') n = n.nextElementSibling;
    if (n) logout = n;
  }
  if (!logout) {
    // 兜底：找含「退出」文本或 logout 字样的 button
    logout = [...dialog.querySelectorAll('button')].find(b => {
      const s = (b.textContent || '').trim();
      return s.includes('退出') || s.includes('登出') || /logout/i.test(s);
    }) || null;
  }

  const box = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      rect: { top: Math.round(r.top), bottom: Math.round(r.bottom), height: Math.round(r.height) },
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      flexGrow: cs.flexGrow,
      flexShrink: cs.flexShrink,
      overflowY: cs.overflowY,
      display: cs.display,
      text: (el.textContent || '').trim().slice(0, 40),
    };
  };

  out.viewport = { w: innerWidth, h: innerHeight };
  out.dialog = box(dialog);
  out.scroller = box(scroller);
  out.root = box(root);
  out.nav = box(nav);
  out.logout = box(logout);

  if (logout && scroller) {
    const lr = logout.getBoundingClientRect();
    const sr = scroller.getBoundingClientRect();
    out.logoutVisible = lr.bottom <= sr.bottom && lr.top >= sr.top;
    out.logoutBelowFold = Math.round(lr.bottom - sr.bottom);
  }
  out.navItemCount = nav ? nav.children.length : 0;
  return out;
})()
"""


def main():
    page = new_page()
    try:
        page.send("Page.enable")
        page.send("Runtime.enable")

        # 移动端视口 + 触摸
        page.send(
            "Emulation.setDeviceMetricsOverride",
            width=MOBILE_W, height=MOBILE_H,
            deviceScaleFactor=2, mobile=True,
        )
        page.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        page.send("Network.setUserAgentOverride", userAgent=MOBILE_UA)

        # 注入登录态（与 cdp_test.py 同口径：走真实 cookie）
        page.send("Page.navigate", url=APP)
        time.sleep(3)
        page.eval(f"document.cookie = 'dsa_user_token={TOKEN}; path=/';")
        page.send("Page.navigate", url=APP)
        time.sleep(4)

        print("=== 阶段 1：移动端默认状态（抽屉应关闭）===")
        closed = page.eval(
            "(() => ({"
            "  vw: innerWidth, vh: innerHeight,"
            "  dialog: !!document.querySelector('[role=\"dialog\"][aria-modal=\"true\"]'),"
            "  asideDisplay: (() => { const a = document.querySelector('aside');"
            "     return a ? getComputedStyle(a).display : 'no-aside'; })(),"
            "}))()"
        )
        print(json.dumps(closed, ensure_ascii=False, indent=2))

        # 打开汉堡菜单。Shell.tsx:42-49 的按钮 aria-label = '打开导航菜单'。
        # 页面上可能有多个 hamburger 样式的按钮，这里逐个报告以辨明身份。
        open_js = r"""
        (() => {
          const report = [];
          const btns = [...document.querySelectorAll('button')];
          const hamburgers = btns.filter(b => {
            const svg = b.querySelector('svg');
            if (!svg) return false;
            const cls = b.className || '';
            return cls.includes('lg:hidden') || cls.includes('pointer-events-auto');
          });
          for (const b of hamburgers) {
            const r = b.getBoundingClientRect();
            report.push({
              aria: b.getAttribute('aria-label'),
              cls: (b.className || '').slice(0, 70),
              at: [Math.round(r.left), Math.round(r.top)],
              visible: r.width > 0 && r.height > 0,
            });
          }
          // 优先点 aria-label 精确匹配的那个（Shell 的原生汉堡）
          let target = hamburgers.find(b => (b.getAttribute('aria-label') || '').includes('导航'))
                    || hamburgers.find(b => b.className.includes('lg:hidden'))
                    || hamburgers[0];
          if (target) { target.click(); return { clicked: true, report }; }
          return { clicked: false, report };
        })()
        """
        clicked = page.eval(open_js)
        print(f"\n[汉堡按钮枚举与点击]")
        print(json.dumps(clicked, ensure_ascii=False, indent=2))
        time.sleep(2.5)

        print("\n=== 阶段 2：抽屉打开后 ===")
        post = page.eval(MEASURE_JS)
        print(json.dumps(post, ensure_ascii=False, indent=2))

        path = os.path.join(OUTDIR, f"{LABEL}-mobile-drawer.png")
        page.shot(path)
        print(f"\n截图: {path}")

        # --- 判定 ---
        print("\n" + "=" * 66)
        print("判定")
        print("=" * 66)
        ok_diag = False
        if isinstance(post, dict) and "error" not in post:
            nav_grow = (post.get("nav") or {}).get("flexGrow")
            scr = post.get("scroller") or {}
            lo = post.get("logout") or {}
            overflow = (scr.get("scrollHeight") or 0) - (scr.get("clientHeight") or 0)
            below = post.get("logoutBelowFold")
            print(f"  视口            : {post.get('viewport')}")
            print(f"  nav flex-grow   : {nav_grow}")
            print(f"  内容容器溢出量  : {overflow}px (scrollHeight-clientHeight)")
            print(f"  退出按钮可见    : {post.get('logoutVisible')}")
            print(f"  按钮超出底边    : {below}px")
            if below is not None and below > 0:
                print(f"  → [确认] 退出按钮在可视区下方 {below}px，需滚动才能看见")
                ok_diag = True
            elif post.get("logoutVisible"):
                print("  → 按钮当前可见（可能视口够高）。可缩小高度复测。")
            if nav_grow == "1" and overflow > 0:
                print(f"  → [确认] nav flex-grow=1 吃光高度并造成 {overflow}px 溢出 —— 根因成立")
        else:
            print(f"  无法量取：{(post or {}).get('error')}")
        print("=" * 66)
        return 0 if ok_diag else 1
    finally:
        page.close()


if __name__ == "__main__":
    sys.exit(main())
