"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH = pathlib.Path("reports/health-events.jsonl")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    now = time.time()
    rec = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
           "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N (mặc định N)."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def last_outage(region: str) -> dict | None:
    """Sự kiện kill gần nhất của `region` trong chaos log (mốc t_outage)."""
    kills = [e for e in _jsonl(CHAOS) if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1] if kills else None


def health_alert(region: str, since: float) -> dict | None:
    """Alert UNHEALTHY đầu tiên của health checker cho `region` sau mốc `since`."""
    return next((e for e in _jsonl(HEALTH)
                 if e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                 and e.get("region") == region and e["ts"] >= since), None)


def confirm_outage(primary: str, since: float, interval: float, threshold: int,
                   max_wait: float) -> dict:
    """Không tin 1 lần fail: chờ alert của health checker, đồng thời tự probe primary.

    Xác nhận khi health checker đã báo UNHEALTHY (đường chính — operator hành động theo
    alert). Nếu không có health checker nào chạy, fallback: tự đếm `threshold` lần
    probe fail liên tiếp nhưng chỉ sau khi đã chờ hết `max_wait` giây.
    Primary hồi phục giữa chừng -> KHÔNG phải outage.
    """
    t0, fails, probes = time.time(), 0, []
    while True:
        ok, reason = hc.probe(primary, 2.0)
        fails = 0 if ok else fails + 1
        probes.append({"ts": round(time.time(), 3), "ready": ok, "reason": reason})
        alert = health_alert(primary, since)
        if alert and fails >= 1:
            return {"confirmed": True, "via": "health_checker_alert", "alert_ts": alert["ts"],
                    "consecutive_fails": fails, "probes": probes}
        if fails >= threshold and time.time() - t0 >= max_wait:
            return {"confirmed": True, "via": "runbook_probes", "alert_ts": None,
                    "consecutive_fails": fails, "probes": probes}
        if time.time() - t0 >= max_wait and fails == 0:
            return {"confirmed": False, "via": "primary_ready_again", "alert_ts": None,
                    "consecutive_fails": 0, "probes": probes}
        time.sleep(interval)


def golden_signals(region: str, n: int = 10) -> dict:
    """n request thật thẳng vào region (bỏ qua edge cache): p95 latency + error rate."""
    lat, errors = [], 0
    for i in range(n):
        t = time.time()
        try:
            r = httpx.get(f"{URL[region]}/v1/infer", params={"q": f"hoa don thang {i + 1}"},
                          timeout=3.0)
            ok = r.status_code == 200 and "error" not in r.json()
        except Exception:
            ok = False
        lat.append((time.time() - t) * 1000)
        errors += 0 if ok else 1
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(round(0.95 * len(lat))) - 1)]
    return {"requests": n, "errors": errors, "error_rate": errors / n,
            "p50_ms": round(lat[len(lat) // 2], 1), "p95_ms": round(p95, 1)}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook §4 "Region Chính Down"."""
    t_start = time.time()
    outage = last_outage(primary)
    t_outage = outage["ts"] if outage else None

    # 1 — xác nhận outage (nhiều lần probe / alert, không tin 1 lần fail)
    c = confirm_outage(primary, since=t_outage or t_start, interval=2.0, threshold=3,
                       max_wait=60.0)
    other_ok, other_reason = hc.probe(target, 2.0)
    step(1, "xac_nhan_outage", primary=primary, target=target, **c,
         target_alive_probe={"ready": other_ok, "reason": other_reason})
    if not c["confirmed"]:
        step(7, "post_incident", aborted=True, reason="primary van ready -> khong failover",
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "reason": "outage_not_confirmed", "confirm": c}

    # 2 — mở incident, bấm giờ. ts dòng này = mốc "operator biết tin", luôn SAU t_outage
    s2 = step(2, "thong_bao_incident", severity="SEV1",
              summary=f"region-{primary} khong ready, chuan bi failover sang region-{target}",
              t_outage=t_outage, t_outage_iso=outage.get("iso") if outage else None,
              t_alert=c["alert_ts"])
    if t_outage is not None:
        s2_delay = round(s2["ts"] - t_outage, 2)
        print(f"do tre thong bao (operator biet tin - t_outage) = {s2_delay}s")

    if not confirm(auto, f"Failover {primary} -> {target}?"):
        step(7, "post_incident", aborted=True, reason="operator tu choi failover",
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "reason": "operator_declined"}

    # 3 — failover MỘT LẦN DUY NHẤT (5 bước con tự log ra reports/failover-events.jsonl)
    fr = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", failover_ok=fr.get("ok"), waited_s=fr.get("waited_s"),
         failover_elapsed_s=fr.get("elapsed_s"), error=fr.get("error"))

    # 4 — chỉ ĐỌC lại kết quả state replica từ dict của bước 3
    after = fr.get("target_state_after") or {}
    restore = fr.get("restore") or {}
    step(4, "verify_state_replica", vector_count=after.get("count"),
         weights=after.get("weights"), pool_state=after.get("pool_state"),
         rpo_seconds=restore.get("rpo_seconds"), docs_lost=restore.get("docs_lost"),
         embed_model_version=restore.get("embed_model_version"))

    # 5 — chỉ ĐỌC lại kết quả cutover
    step(5, "dns_cutover", ok=bool(fr.get("ok")), cutover=fr.get("cutover"))

    # 6 — golden signals trên region phụ
    gs = golden_signals(target) if fr.get("ok") else None
    step(6, "verify_golden_signals", region=target, **(gs or {"skipped": True}))

    # 7 — tổng kết
    elapsed = round(time.time() - t_start, 2)
    step(7, "post_incident", ok=bool(fr.get("ok")), elapsed_s=elapsed,
         since_outage_s=None if t_outage is None else round(time.time() - t_outage, 2),
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    return {"ok": bool(fr.get("ok")), "confirm": {k: v for k, v in c.items() if k != "probes"},
            "failover": fr, "golden_signals": gs, "elapsed_s": elapsed}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
