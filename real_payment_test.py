"""真实支付流程测试 - 验证无虚假充值入口
测试要点：
1. 旧的模拟支付端点 /api/orders/{oid}/pay 必须不存在（404）
2. 创建订单不自动到账
3. 提交凭证不自动到账
4. 只有管理员确认后才到账
5. 管理员可查看待确认订单
"""
import requests
import json
import time
import io

BASE = "http://localhost:8000"
ADMIN_KEY = "demo-master"
results = []

def log(name, passed, evidence=""):
    status = "PASS" if passed else "FAIL"
    results.append((name, passed, evidence))
    print(f"[{status}] {name} | {evidence}")

# ========== 1. 管理员认证（直接使用主密钥） ==========
ADMIN_H = {"Authorization": f"Bearer {ADMIN_KEY}"}
r = requests.get(f"{BASE}/api/stats", headers=ADMIN_H)
log("管理员认证（主密钥）", r.status_code == 200, f"status={r.status_code}")

# ========== 2. 注册测试用户 ==========
ts = int(time.time())
user_email = f"paytest_{ts}@example.com"
r = requests.get(f"{BASE}/api/captcha")
captcha = r.json()
# 解析验证码问题，如 "8 - 4 = ?"
import re
q = captcha.get("question", "")
nums = [int(x) for x in re.findall(r'\d+', q)]
if '-' in q and len(nums) >= 2:
    captcha_answer = str(nums[0] - nums[1])
elif '+' in q and len(nums) >= 2:
    captcha_answer = str(nums[0] + nums[1])
else:
    captcha_answer = "0"
r = requests.post(f"{BASE}/api/auth/register", json={
    "email": user_email, "password": "Test123456",
    "captcha_token": captcha["token"], "captcha_answer": captcha_answer
})
log("注册测试用户", r.status_code == 200, f"email={user_email}")
r = requests.post(f"{BASE}/api/auth/login", json={"email": user_email, "password": "Test123456"})
user_token = r.json().get("session_token")
log("测试用户登录", r.status_code == 200 and user_token, f"status={r.status_code}")
USER_H = {"Authorization": f"Bearer {user_token}"}

# ========== 3. 获取用户初始余额 ==========
r = requests.get(f"{BASE}/api/auth/me", headers=USER_H)
initial_balance = r.json().get("balance", 0)
log("获取初始余额", r.status_code == 200, f"balance=${initial_balance}")

# ========== 4. 验证旧的模拟支付端点已移除 ==========
r = requests.post(f"{BASE}/api/orders/FakeOrderId/pay", headers=USER_H)
log("旧模拟支付端点已移除 (404/405)", r.status_code in (404, 405, 403), f"status={r.status_code}")

# ========== 5. 创建充值订单 - 验证不自动到账 ==========
r = requests.post(f"{BASE}/api/orders", headers=USER_H, json={
    "type": "topup", "amount": 10, "detail": "alipay", "payment_method": "alipay"
})
order_data = r.json()
order_id = order_data.get("id")
log("创建充值订单", r.status_code == 200 and order_id, f"order_id={order_id}, status={r.status_code}")
log("订单返回唯一金额", "pay_amount_actual" in order_data and "unique_suffix" in order_data,
    f"pay_amount_actual={order_data.get('pay_amount_actual')}, suffix={order_data.get('unique_suffix')}")
log("订单返回收款码信息", "qr_code" in order_data and "payment_name" in order_data,
    f"payment_name={order_data.get('payment_name')}")
log("订单状态为pending", order_data.get("status") == "pending", f"status={order_data.get('status')}")

# 验证创建订单后余额不变
r = requests.get(f"{BASE}/api/auth/me", headers=USER_H)
after_order_balance = r.json().get("balance", 0)
log("创建订单后余额不变", after_order_balance == initial_balance,
    f"before=${initial_balance}, after=${after_order_balance}")

# ========== 6. 提交付款凭证 - 验证不自动到账 ==========
# 创建一个假的图片文件作为凭证
fake_img = io.BytesIO(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde')
fake_img.name = "proof.png"
r = requests.post(f"{BASE}/api/orders/{order_id}/submit-proof",
                  headers=USER_H,
                  data={"txid": "TEST_TX_001", "note": "测试凭证"},
                  files={"screenshot": ("proof.png", fake_img, "image/png")})
proof_result = r.json()
log("提交付款凭证", r.status_code == 200 and proof_result.get("status") == "verifying",
    f"status={r.status_code}, result_status={proof_result.get('status')}")
log("凭证提交后订单状态=verifying", proof_result.get("status") == "verifying",
    f"message={proof_result.get('message')}")

# 验证提交凭证后余额仍不变
r = requests.get(f"{BASE}/api/auth/me", headers=USER_H)
after_proof_balance = r.json().get("balance", 0)
log("提交凭证后余额仍不变", after_proof_balance == initial_balance,
    f"before=${initial_balance}, after=${after_proof_balance}")

# ========== 7. 管理员查看待确认订单 ==========
r = requests.get(f"{BASE}/api/admin/orders/pending", headers=ADMIN_H)
pending_orders = r.json() if r.status_code == 200 else []
found_my_order = any(o.get("id") == order_id for o in pending_orders)
log("管理员查看待确认订单", r.status_code == 200, f"count={len(pending_orders)}")
log("待确认列表包含我的订单", found_my_order, f"order_id={order_id}")

# 验证管理员能看到凭证信息
my_order = next((o for o in pending_orders if o.get("id") == order_id), {})
log("管理员可见凭证截图", "screenshot_path" in my_order and my_order["screenshot_path"],
    f"screenshot={my_order.get('screenshot_path')}")
log("管理员可见交易号", my_order.get("proof_txid") == "TEST_TX_001",
    f"txid={my_order.get('proof_txid')}")

# ========== 8. 管理员确认订单 - 验证此时才到账 ==========
r = requests.post(f"{BASE}/api/admin/orders/{order_id}/confirm", headers=ADMIN_H)
confirm_result = r.json()
log("管理员确认订单", r.status_code == 200 and confirm_result.get("ok"),
    f"status={r.status_code}, added_quota={confirm_result.get('added_quota')}")
expected_quota = 10 * 12  # ¥10 * 12 = $120
log("确认后到账额度正确", confirm_result.get("added_quota") == expected_quota,
    f"expected={expected_quota}, actual={confirm_result.get('added_quota')}")

# 验证确认后余额增加
r = requests.get(f"{BASE}/api/auth/me", headers=USER_H)
final_balance = r.json().get("balance", 0)
log("确认后余额增加", final_balance == initial_balance + expected_quota,
    f"before=${initial_balance}, after=${final_balance}, expected=${initial_balance + expected_quota}")

# ========== 9. 验证重复确认被拦截 ==========
r = requests.post(f"{BASE}/api/admin/orders/{order_id}/confirm", headers=ADMIN_H)
log("重复确认被拦截", r.status_code == 400, f"status={r.status_code}")

# ========== 10. 管理员拒绝订单测试 ==========
# 创建新订单（用alipay确保支付方式可用）
r = requests.post(f"{BASE}/api/orders", headers=USER_H, json={
    "type": "topup", "amount": 50, "detail": "alipay", "payment_method": "alipay"
})
order2 = r.json()
order2_id = order2.get("id")
log("创建拒绝测试订单", r.status_code == 200 and order2_id, f"order2_id={order2_id}")

# 提交凭证
fake_img2 = io.BytesIO(b'\x89PNG\r\n\x1a\n')
fake_img2.name = "proof2.png"
r = requests.post(f"{BASE}/api/orders/{order2_id}/submit-proof",
                  headers=USER_H,
                  data={"txid": "TEST_TX_002", "note": "测试拒绝"},
                  files={"screenshot": ("proof2.png", fake_img2, "image/png")})
log("拒绝测试订单提交凭证", r.status_code == 200, f"status={r.status_code}")

# 管理员拒绝
r = requests.post(f"{BASE}/api/admin/orders/{order2_id}/reject", headers=ADMIN_H, data={"reason": "未收到款项"})
log("管理员拒绝订单", r.status_code == 200 and r.json().get("ok"), f"status={r.status_code}, resp={r.text[:200]}")

# 验证拒绝后余额不变
r = requests.get(f"{BASE}/api/auth/me", headers=USER_H)
balance_after_reject = r.json().get("balance", 0)
log("拒绝订单后余额不变", balance_after_reject == final_balance,
    f"before=${final_balance}, after=${balance_after_reject}")

# ========== 11. 验证前端无模拟支付入口 ==========
r = requests.get(f"{BASE}/")
home_html = r.text
has_simulated_pay = "模拟" in home_html or "simulated" in home_html.lower() or "/api/orders/" in home_html and "/pay" in home_html
log("首页无模拟支付入口", not has_simulated_pay, f"模拟字样={('模拟' in home_html)}")

r = requests.get(f"{BASE}/dashboard")
dash_html = r.text
# 检查前端JS中是否有直接到账的pay调用
has_auto_pay = "added_quota" in dash_html and "/pay" in dash_html and "submit-proof" not in dash_html
log("仪表盘无虚假充值入口", not has_auto_pay, f"has_submit_proof={'submit-proof' in dash_html}")

# ========== 12. 验证支付配置端点 ==========
r = requests.get(f"{BASE}/api/payment/config", headers=USER_H)
log("支付配置端点正常", r.status_code == 200, f"status={r.status_code}, methods={len(r.json().get('methods', []))}")

# ========== 汇总 ==========
total = len(results)
passed = sum(1 for _, p, _ in results if p)
failed = total - passed
print(f"\n{'='*60}")
print(f"真实支付流程测试结果：{passed}/{total} 通过 ({passed*100//total}%)")
print(f"{'='*60}")
if failed:
    print(f"\n失败项：")
    for name, p, ev in results:
        if not p:
            print(f"  - {name}: {ev}")
