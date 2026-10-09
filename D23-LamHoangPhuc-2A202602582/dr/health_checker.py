"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException as e:
        return False, f"timeout_{timeout}s({type(e).__name__})"
    except Exception as e:  # ConnectError khi process chết (mode stop)
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = r.json().get("reasons") or []
    except Exception:
        reasons = []
    return False, f"http_{r.status_code}:" + ",".join(reasons)


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL.

    Trạng thái ban đầu giả định HEALTHY (không ghi log); chỉ đổi sang UNHEALTHY sau
    `threshold` lần fail liên tiếp, và chỉ về lại HEALTHY sau `threshold` lần OK liên
    tiếp — chống flapping theo cả hai chiều.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    state = {r: {"status": "HEALTHY", "fails": 0, "oks": 0} for r in URL}
    end = time.time() + duration
    with out.open("a") as f:
        while time.time() < end:
            t_round = time.time()
            for region in URL:
                ok, reason = probe(region, timeout)
                s = state[region]
                if ok:
                    s["oks"], s["fails"] = s["oks"] + 1, 0
                else:
                    s["fails"], s["oks"] = s["fails"] + 1, 0
                new = s["status"]
                if s["status"] == "HEALTHY" and s["fails"] >= threshold:
                    new = "UNHEALTHY"
                elif s["status"] == "UNHEALTHY" and s["oks"] >= threshold:
                    new = "HEALTHY"
                if new != s["status"]:
                    now = time.time()
                    rec = {"ts": now,
                           "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
                           "event": "state_change", "region": region,
                           "from": s["status"], "to": new, "reason": reason,
                           "consecutive_fails": s["fails"], "consecutive_oks": s["oks"],
                           "interval_s": interval, "threshold": threshold,
                           "timeout_s": timeout, "detect_floor_s": round(interval * threshold, 2)}
                    f.write(json.dumps(rec) + "\n")
                    f.flush()
                    print("HEALTH", json.dumps(rec))
                    s["status"] = new
            time.sleep(max(0.0, interval - (time.time() - t_round)))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
