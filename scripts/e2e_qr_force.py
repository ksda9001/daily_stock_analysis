"""探针：验证 WeixinQrHandler 的 force 通道。

**必须在 cowagent 容器内执行**：端点有来源 IP 白名单
（web_channel.py:320 只放行 172.* / 127.0.0.1），宿主机与本机都打不进去。
容器内无 curl，故用 urllib。

判定矩阵：
  A. GET 无 force（通道在线）       -> 期望 logged_in=true, forceable=true, 无新码
  B. POST {action:refresh} 无 force -> 期望与 A 同构（保持短路）
  C. POST {action:refresh,force:1}  -> 期望**不再**回 logged_in，而是签发新码
  D. POST {action:refresh,force:0}  -> 期望等同 A
  E. 复检 GET 无 force              -> 期望仍 logged_in（证明「签发无副作用」）

安全性：C 只是**签发**新码，不触碰现有 bot token。
真正顶掉会话的是「扫码确认」（_poll_status 的 confirmed 分支），本探针不执行确认。
"""
import json
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:9899/api/weixin/qrlogin"
PATH = "/api/weixin/qrlogin"


def call(method: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(URL, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def probe(label: str, method: str, body=None) -> dict:
    status, raw = call(method, body)
    try:
        data = json.loads(raw)
    except Exception:
        data = {"_parse_error": True, "_raw_head": raw[:400]}
    print(f"\n--- {label} ---")
    print(f"    请求: {method} {PATH} {body if body else ''}")
    print(f"    HTTP {status}")
    if data.get("_parse_error"):
        print(f"    原始响应: {data['_raw_head']}")
        return data
    qr = data.get("qr_image") or ""
    qrurl = data.get("qrcode_url") or ""
    print(f"    status    = {data.get('status')!r}")
    print(f"    logged_in = {data.get('logged_in')!r}")
    print(f"    forceable = {data.get('forceable')!r}")
    print(f"    qr_status = {data.get('qr_status')!r}")
    print(f"    qr_image  = {'<有 %d 字节>' % len(qr) if qr else '空'}")
    print(f"    qrcode_url= {'<有 %d 字节>' % len(qrurl) if qrurl else '空'}")
    print(f"    message   = {data.get('message')!r}")
    return data


r = {}
r["A"] = probe("A. GET 无 force（应保持短路）", "GET")
r["B"] = probe("B. POST refresh 无 force（应保持短路）", "POST", {"action": "refresh"})
r["C"] = probe("C. POST refresh force=1（应签发新码）", "POST", {"action": "refresh", "force": 1})
r["D"] = probe("D. POST refresh force=0（应等同 A）", "POST", {"action": "refresh", "force": 0})
r["E"] = probe("E. 复检 GET 无 force（证明签发无副作用）", "GET")

print("\n" + "=" * 66)
print("判定")
print("=" * 66)

ok = True


def check(label, cond, detail):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}: {detail}")


a, b, c, d, e = r["A"], r["B"], r["C"], r["D"], r["E"]

check("A 短路保持", a.get("logged_in") is True and not a.get("qr_image"),
      "GET 无 force 仍回 logged_in=true 且无新码")
check("A 可发现性", a.get("forceable") is True,
      "返回 forceable=true，前端据此把按钮切成「重新扫码」")
check("B refresh 无 force 不绕过",
      b.get("logged_in") is True and not b.get("qr_image"),
      "POST refresh 缺 force 时行为与 GET 一致")
check("C force=1 绕过短路",
      c.get("logged_in") is not True and bool(c.get("qr_image") or c.get("qrcode_url")),
      f"force=1 时签发新码（logged_in={c.get('logged_in')!r}）")
check("D force=0 不绕过",
      d.get("logged_in") is True and not d.get("qr_image"),
      "显式 force=0 等同缺省")
check("E 通道未被顶掉", e.get("logged_in") is True,
      "签发新码后通道仍 logged_in —— 验证「签发无副作用、只有确认才顶掉」")

print("\n" + "=" * 66)
print("结论：" + ("全部通过" if ok else "存在问题，见上方 FAIL"))
print("=" * 66)
sys.exit(0 if ok else 1)
