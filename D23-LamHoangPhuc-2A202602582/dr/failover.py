"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    now = time.time()
    rec = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """/v1/state của 1 region; không raise — region chết thì trả về error."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def wait_ready(region: str, wait: float, poll: float = 0.5) -> tuple[bool, float, list]:
    """Poll /readyz tới khi 200 hoặc hết `wait` giây. Trả (ready, waited_s, last_reasons)."""
    t0 = time.time()
    reasons = []
    while True:
        try:
            r = httpx.get(f"{URL[region]}/readyz", timeout=2.0)
            if r.status_code == 200:
                return True, round(time.time() - t0, 2), []
            reasons = r.json().get("reasons", [])
        except Exception as e:
            reasons = [type(e).__name__]
        if time.time() - t0 >= wait:
            return False, round(time.time() - t0, 2), reasons
        time.sleep(poll)


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Bước 4 timeout -> abort, KHÔNG cutover."""
    t_start = time.time()
    primary = "a" if target == "b" else "b"
    target_dir = pathlib.Path(f"state/region-{target}")
    result = {"ok": False, "target": target, "primary": primary, "backend": backend}

    # 1 — trạng thái hiện tại của region đích (trước khi đụng vào gì)
    before = state_of(target)
    emit(step="1_verify_target", target=target, state=before)
    result["target_state_before"] = before

    # 2 — restore snapshot (state layer) + đo RPO thật bằng cách so với primary DB
    try:
        meta = snapshot.get(target, backend)
    except SystemExit as e:  # snapshot.get báo "chưa từng put" bằng SystemExit
        emit(step="2_restore_snapshot", target=target, ok=False, error=str(e))
        result["error"] = f"restore_failed: {e}"
        return result
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     target_dir / "vectors.sqlite")
    restore = {"snapshot_at": meta.get("snapshot_at"),
               "embed_model_version": meta.get("embed_model_version"),
               "rpo_seconds": r["rpo_seconds"], "docs_lost": r["docs_lost"],
               "primary_latest_doc_ts": r["primary_latest_doc_ts"],
               "restored_latest_doc_ts": r["restored_latest_doc_ts"]}
    emit(step="2_restore_snapshot", target=target, ok=True, **restore)
    result["restore"] = restore

    # 3 — scale GPU pool warm -> full (compute layer)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "pool_state").write_text("full")
    emit(step="3_scale_pool", target=target, pool_state="full")

    # 4 — chờ /readyz 200 (warm-up nằm trong RTO). Timeout -> ABORT, không cutover.
    ready, waited, reasons = wait_ready(target, wait)
    emit(step="4_wait_ready", target=target, ready=ready, waited_s=waited, reasons=reasons)
    result["ready"], result["waited_s"] = ready, waited
    if not ready:
        emit(step="abort", target=target, reason="target_not_ready_before_cutover",
             last_reasons=reasons, note="KHONG cutover: active_region giu nguyen")
        result["error"] = f"target_not_ready_after_{waited}s: {reasons}"
        result["elapsed_s"] = round(time.time() - t_start, 2)
        return result

    # 5 — chỉ bây giờ mới đổi "DNS" (traffic layer)
    active = pathlib.Path("edge/active_region")
    prev = active.read_text().strip() if active.exists() else None
    active.write_text(target)
    emit(step="5_dns_cutover", target=target, previous=prev, active_region=target)

    result["target_state_after"] = state_of(target)
    result["cutover"] = {"previous": prev, "active_region": target}
    result["ok"] = True
    result["elapsed_s"] = round(time.time() - t_start, 2)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
