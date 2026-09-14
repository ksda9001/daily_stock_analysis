#!/usr/bin/env python3
"""回归探针：移动端抽屉里「退出按钮」在多档视口下都必须可见可点。

修的是 `[role="dialog"] nav` 抢高度导致退出按钮被挤出+压扁。
本探针在多个（宽度, 高度）组合下逐一验证：
  1. 退出按钮 height 必须 ≥ 40px（不能被 flex 压扁）
  2. 退出按钮底边必须在内容容器可视区内（logoutBelowFold <= 0）
  3. nav 必须是 flex-grow:0 + overflow-y:auto（不再抢高度）
  4. 退出按钮必须真的可点（elementFromPoint 命中它，即没被遮挡）

矮视口（如 568）是回归重点：菜单必然溢出，按钮必须仍固定可见。

用法：PYTHONPATH=/tmp/pylibs python3 probe_mobile_logout_regress.py [label]
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
LABEL = sys.argv[1] if len(sys.argv) > 1 else "regress"
OUTDIR = "/tmp/shots"
os.makedirs(OUTDIR, exist_ok=True)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# (标签, 宽, 高) —— 844 是 iPhone 竖屏可用高度；568 是 iPhone SE 一代的极端矮屏
VIEWPORTS = [
    ("iphone14-390x844", 390, 844),
    ("iphone-se-375x667", 375, 667),
    ("tiny-320x568", 320, 568),
]


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
  const dialog = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!dialog) return { error: 'no dialog' };
  const scroller = dialog.querySelector('.overflow-y-auto') || dialog;
  const nav = dialog.querySelector('nav');
  let logout = null;
  if (nav) {
    let n = nav.nextElementSibling;
    while (n && n.tagName !== 'BUTTON') n = n.nextElementSibling;
    if (n) logout = n;
  }
  if (!logout) {
    logout = [...dialog.querySelectorAll('button')].find(b =>
      /log\s*out|退出|登出/i.test(b.textContent || '')
    ) || null;
  }
  const cs = nav ? getComputedStyle(nav) : {};
  const out = {
    nav: nav ? {
      flexGrow: cs.flexGrow,
      overflowY: cs.overflowY,
      scrollHeight: nav.scrollHeight,
      clientHeight: nav.clientHeight,
      itemCount: nav.children.length,
    } : null,
    viewport: { w: innerWidth, h: innerHeight },
  };
  if (!logout) { out.error = 'no logout button'; return out; }
  const lr = logout.getBoundingClientRect();
  const sr = scroller.getBoundingClientRect();
  out.logout = {
    height: Math.round(lr.height),
    top: Math.round(lr.top),
    bottom: Math.round(lr.bottom),
    flexShrink: getComputedStyle(logout).flexShrink,
    text: (logout.textContent || '').trim(),
  };
  out.scrollerBottom = Math.round(sr.bottom);
  out.belowFold = Math.round(lr.bottom - sr.bottom);
  out.visible = lr.bottom <= sr.bottom + 1 && lr.height >= 40;
  // 命中测试：按钮中心点必须解析到按钮自身（未被遮挡）
  const cx = lr.left + lr.width / 2;
  const cy = lr.top + lr.height / 2;
  const hitEl = document.elementFromPoint(cx, cy);
  out.clickable = !!(hitEl && (hitEl === logout || logout.contains(hitEl)));
  out.hitTag = hitEl ? hitEl.tagName : null;
  return out;
})()
"""


def open_drawer(page):
    page.eval(
        r"""
        (() => {
          const btns = [...document.querySelectorAll('button')].filter(b =>
            b.querySelector('svg') && (b.className || '').includes('pointer-events-auto'));
          const t = btns.find(b => (b.getAttribute('aria-label') || '').includes('导航')) || btns[0];
          if (t) t.click();
          return !!t;
        })()
        """
    )
    time.sleep(2)


def close_drawer(page):
    page.eval(
        r"""
        (() => {
          const d = document.querySelector('[role="dialog"][aria-modal="true"]');
          if (!d) return false;
          const x = [...d.querySelectorAll('button')].find(b =>
            /close|关闭/i.test(b.getAttribute('aria-label') || ''));
          if (x) { x.click(); return true; }
          return false;
        })()
        """
    )
    time.sleep(1.5)


def main():
    page = None
    try:
        t = http_json("/json/new?about:blank", method="PUT")
        page = Page(t["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.setUserAgentOverride", userAgent=MOBILE_UA)
        page.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)

        # 先登录一次（cookie 会跟到后续 viewport）
        page.send(
            "Emulation.setDeviceMetricsOverride",
            width=390, height=844, deviceScaleFactor=2, mobile=True,
        )
        page.send("Page.navigate", url=APP)
        time.sleep(3)
        page.eval(f"document.cookie = 'dsa_user_token={TOKEN}; path=/';")

        ok = True
        for label, w, h in VIEWPORTS:
            page.send(
                "Emulation.setDeviceMetricsOverride",
                width=w, height=h, deviceScaleFactor=2, mobile=True,
            )
            page.send("Page.navigate", url=APP)
            time.sleep(3.5)
            open_drawer(page)
            m = page.eval(MEASURE_JS)
            path = os.path.join(OUTDIR, f"{LABEL}-{label}.png")
            page.shot(path)

            print(f"\n--- {label} ({w}x{h}) ---")
            if not isinstance(m, dict) or m.get("error"):
                print(f"    量取失败: {(m or {}).get('error')}")
                ok = False
                close_drawer(page)
                continue
            nv, lo = m.get("nav") or {}, m.get("logout") or {}
            print(f"    nav: flex-grow={nv.get('flexGrow')} overflowY={nv.get('overflowY')} "
                  f"content={nv.get('scrollHeight')}px visible={nv.get('clientHeight')}px "
                  f"items={nv.get('itemCount')}")
            print(f"    退出按钮: height={lo.get('height')}px flexShrink={lo.get('flexShrink')} "
                  f"text={lo.get('text')!r}")
            print(f"    belowFold={m.get('belowFold')}px  visible={m.get('visible')}  "
                  f"clickable={m.get('clickable')} (hit={m.get('hitTag')})")
            print(f"    截图: {path}")

            checks = [
                ("nav 不抢高度", nv.get("flexGrow") == "0", f"flex-grow={nv.get('flexGrow')}"),
                ("nav 自身可滚", nv.get("overflowY") == "auto", f"overflow-y={nv.get('overflowY')}"),
                ("按钮未被压扁", (lo.get("height") or 0) >= 40, f"height={lo.get('height')}px"),
                ("按钮在可视区内", (m.get("belowFold") or 0) <= 1, f"belowFold={m.get('belowFold')}px"),
                ("按钮可点未被遮挡", m.get("clickable") is True, f"hit={m.get('hitTag')}"),
            ]
            for name, cond, detail in checks:
                print(f"      [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
                ok = ok and cond
            close_drawer(page)

        print("\n" + "=" * 66)
        print("结论：" + ("全部通过" if ok else "存在问题，见上方 FAIL"))
        print("=" * 66)
        return 0 if ok else 1
    finally:
        if page:
            page.close()


if __name__ == "__main__":
    sys.exit(main())
