import urllib.request
import time
import random
import threading

TARGET_URL = "https://tokengo-d0cb.onrender.com/health"
CHECK_INTERVAL = 120  
MAX_RETRIES = 3

def ping(url):
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=15) as response:
            status = response.status
            content = response.read().decode('utf-8')
            return status, content
    except Exception as e:
        return None, str(e)

def keep_alive_worker():
    print(f"[外部保活] 开始监控 {TARGET_URL}")
    consecutive_failures = 0
    
    while True:
        for attempt in range(MAX_RETRIES):
            status, content = ping(TARGET_URL)
            
            if status == 200:
                print(f"[外部保活] ✓ {time.strftime('%Y-%m-%d %H:%M:%S')} - 状态正常")
                consecutive_failures = 0
                break
            else:
                print(f"[外部保活] ✗ {time.strftime('%Y-%m-%d %H:%M:%S')} - 失败 ({status}): {content}")
                time.sleep(5)
        else:
            consecutive_failures += 1
            print(f"[外部保活] ⚠️ 连续失败 {consecutive_failures} 次")
        
        sleep_time = CHECK_INTERVAL + random.randint(0, 30)
        time.sleep(sleep_time)

if __name__ == "__main__":
    worker = threading.Thread(target=keep_alive_worker, daemon=True)
    worker.start()
    print("[外部保活] 保活服务已启动，按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[外部保活] 保活服务已停止")
