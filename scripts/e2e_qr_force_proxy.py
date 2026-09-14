"""端到端探针：验证 DSA 代理层正确透传 force，且前端资源已就位。

覆盖整条链路（前端 -> DSA -> CowAgent）：
  1. 静态资源：容器内 dsa-wechat.js 含 force 分流代码，index.html 版本号已 bump
  2. 代理层：DSA 的 POST /wechat/qrlogin 是否原样透传 body.force
  3. 回归：GET 端点仍保留 logged_in 短路（不能被误改掉）

用法（在服务器宿主机执行）：
  python3 e2e_qr_force_proxy.py <DSA_BASE> <TOKEN>
其中 TOKEN 是有效的 DSA Bearer token。若未提供 token，则跳过需要鉴权的项，
只跑静态资源检查（仍是有意义的回归）。
"""
import json
import sys
import urllib.error
import urllib.request

DSA_BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""


def sh(script: str) -> str:
    import subprocess
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    return (r.stdout + r.stderr).strip()


def api(method: str, path: str, body=None):
    url = DSA_BASE.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:300]}
    except Exception as e:
        return -1, {"_error": f"{type(e).__name__}: {e}"}


ok = True


def check(label, cond, detail):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}: {detail}")


print("=" * 66)
print("1) 静态资源就位检查")
print("=" * 66)
js = "/app/static"
n_force = sh(f"docker exec dsa-server sh -c \"grep -c 'action: .refresh.' {js}/dsa-wechat.js\"")
n_online = sh(f"docker exec dsa-server sh -c \"grep -c 'dsaQrOnline' {js}/dsa-wechat.js\"")
ver = sh(f"docker exec dsa-server sh -c \"grep -o 'dsa-wechat.js?v=[^\\\"]*' {js}/index.html\"")
print(f"    force 分流出现 {n_force} 次")
print(f"    dsaQrOnline 出现 {n_online} 次")
print(f"    版本号: {ver}")
check("force 分流已部署", n_force.isdigit() and int(n_force) >= 1, f"grep 命中 {n_force}")
check("在线状态变量已部署", n_online.isdigit() and int(n_online) >= 2, f"grep 命中 {n_online}")
check("破缓存版本号已 bump", "qrforce" in ver, ver)

print()
print("=" * 66)
print("2) 代理层透传检查（需要 DSA token）")
print("=" * 66)
if not TOKEN:
    print("    未提供 token，跳过鉴权项。")
else:
    P = "/api/v1/tenancy/wechat/qrlogin"

    st_get, r_get = api("GET", P)
    print(f"\n  GET {P} -> HTTP {st_get}")
    print(f"    logged_in={r_get.get('logged_in')!r} forceable={r_get.get('forceable')!r} "
          f"qr_image={'有' if r_get.get('qr_image') else '空'}")

    st_rf, r_rf = api("POST", P, {"action": "refresh"})
    print(f"\n  POST {P} {{'action':'refresh'}} -> HTTP {st_rf}")
    print(f"    logged_in={r_rf.get('logged_in')!r} qr_image={'有' if r_rf.get('qr_image') else '空'}")

    st_fc, r_fc = api("POST", P, {"action": "refresh", "force": 1})
    print(f"\n  POST {P} {{'action':'refresh','force':1}} -> HTTP {st_fc}")
    print(f"    logged_in={r_fc.get('logged_in')!r} qr_image={'有' if r_fc.get('qr_image') else '空'}")

    check("GET 保留短路（回归）",
          st_get == 200 and r_get.get("logged_in") is True,
          f"HTTP {st_get}, logged_in={r_get.get('logged_in')!r}")
    check("POST refresh 无 force 仍短路",
          st_rf == 200 and r_rf.get("logged_in") is True and not r_rf.get("qr_image"),
          "未带 force 时不应签发新码")
    check("POST force=1 穿透代理并签发新码",
          st_fc == 200 and r_fc.get("logged_in") is not True and bool(r_fc.get("qr_image")),
          f"代理把 force 原样送到了 CowAgent（qr_image={'有' if r_fc.get('qr_image') else '空'}）")

    # 复检通道未被顶掉
    st_again, r_again = api("GET", P)
    check("通道未被顶掉",
          st_again == 200 and r_again.get("logged_in") is True,
          "签发新码后通道仍在线")

print()
print("=" * 66)
print("结论：" + ("全部通过" if ok else "存在问题，见上方 FAIL"))
print("=" * 66)
sys.exit(0 if ok else 1)
