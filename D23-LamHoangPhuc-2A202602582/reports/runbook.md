# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong. Chạy mọi lệnh từ thư mục gốc repo. Kịch bản: primary = `a`, standby = `b`.

**Trigger:** health checker ghi `"to": "UNHEALTHY", "region": "a"` vào `reports/health-events.jsonl`
(3 lần probe `/readyz` fail liên tiếp, interval 5s), hoặc error rate của edge tăng đột biến.

**Cách nhanh (bán tự động, khuyến nghị):** `python3 dr/runbook.py --primary a --target b --backend fs`
— tự làm 7 bước dưới đây, hỏi `y/N` trước khi failover, log từng bước vào `reports/runbook-run.jsonl`.
Chỉ dùng `--auto` trong drill/CI.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` (chạy 3 lần, cách nhau 5s) và `tail -n 3 reports/health-events.jsonl` | `a.ready=false` cả 3 lần **và** có dòng `UNHEALTHY` cho region `a`; đồng thời `b.alive=true` (nếu `b` cũng chết → DỪNG, escalate, không failover) | on-call SRE |
| 2 | Mở incident + bấm giờ RTO | Mở kênh `#inc-region-a` (SEV1), ghi giờ: `date -u +%FT%TZ`; lấy t_outage/alert: `grep UNHEALTHY reports/health-events.jsonl \| tail -1` | Có incident ID + ts mở incident (runbook tự ghi `step:2` vào `reports/runbook-run.jsonl`) | on-call SRE (Incident Commander) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py lag --backend fs` (ghi lại RPO), rồi `python3 state/snapshot.py get --region b --backend fs` | Lệnh in JSON có `restored_at` và `embed_model_version` trùng với region a; `curl -s localhost:8002/v1/state` cho `count>0`, `weights:true` | on-call SRE |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` rồi poll: `until curl -sf localhost:8002/readyz; do sleep 1; done` | `/readyz` của b trả **200** (`"ready": true`); thường mất ~6s warm-up. Quá 60s → DỪNG, KHÔNG cutover, escalate | on-call SRE |
| 5 | DNS/LB cutover | **Chỉ sau khi bước 4 xong:** `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` cho `active_region=b` (chờ tối đa TTL 5s) và `curl -s localhost:8080/v1/infer` trả `"edge_region":"b"` | on-call SRE (IC duyệt) |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' localhost:8080/v1/infer; done` | 10/10 trả `200`, p95 < 500ms, error rate < 1% (drill: 0/10 lỗi, p95 122.3ms) | on-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` != null, `valid: true`; mở draft `reports/postmortem.md` trong 48h | Incident Commander |

Bước 3–5 là đúng 5 bước con của `python3 dr/failover.py --target b --backend fs` (verify → restore →
scale → wait ready → cutover); chạy script thay cho gõ tay để có log `reports/failover-events.jsonl`.
**Không bao giờ** đổi `edge/active_region` trước khi `/readyz` của b trả 200 — user sẽ nhận 503 từ cả hai region.

## Rollback (failover ngược về region A)

**Điều kiện rollback ngay (về A hoặc giữ nguyên A):**
- Bước 4 timeout (b không ready sau 60s) → không cutover, giữ `active_region=a`, escalate.
- Sau cutover, golden signals của b tệ hơn ngưỡng (error rate ≥ 5% hoặc p95 ≥ 2s trong 5 phút) **và**
  region a đã `ready` trở lại → cutover ngược: `printf a > edge/active_region`.

**Failback có kế hoạch (khi A đã khỏi):** chỉ khi region-a `/readyz` trả 200 liên tục ≥ 15 phút
(health checker ghi `to: HEALTHY` cho a), **và** dữ liệu ingest vào b trong thời gian incident đã được
replicate ngược b → a (`python3 state/snapshot.py put --region b` rồi `get --region a`). Làm trong giờ hành chính,
cùng quy trình 5 bước (verify → restore → scale → ready → cutover) với target = a.

**Ai quyết định:** Incident Commander (on-call lead) có quyền trigger rollback/failback; on-call SRE thực thi.
Không có failback tự động (§4 Anti-Patterns: full-auto không có circuit breaker → 2 region flap qua lại).
Tối đa 1 lần failover mỗi chiều trong 1 giờ trừ khi IC chấp thuận bằng văn bản trong kênh incident.
