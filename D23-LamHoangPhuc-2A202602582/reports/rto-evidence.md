# RTO/RPO Evidence — Lab 23 (Lâm Hoàng Phúc — 2A202602582)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.

Môi trường drill: bare mode chạy trong WSL Ubuntu (Python 3.14), `--mock`, `chaos --mode netblock`
(SIGSTOP), `EDGE_TTL_SECONDS=5`, `WARMUP_SECONDS=6`, health checker `interval=5s, threshold=3, timeout=2s`,
replicate `--every 30 --backend fs`, ingest `--rate 0.5`. Ngày chạy: 2026-10-09 (giờ UTC trong log).

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:49:24` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.4s` (ReadTimeout, 2017.2 ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage (dòng cuối vẫn fail) | `reports/drill-1-nodr.jsonl:33` |
| RTO | `NO_RECOVERY` (16/33 request fail) | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

t_outage = `2026-10-09T04:50:33` (ts `1791521433.004`).

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +2.0s | dòng `ok:false` đầu (ReadTimeout) | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | +14.9s | `to:UNHEALTHY, region:a`, `consecutive_fails=3` | `reports/health-events.jsonl:2` |
| Failover bắt đầu (verify target) | +18.9s | `step:1_verify_target` | `reports/failover-events.jsonl:1` |
| Snapshot restore xong | +19.4s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +25.8s | `step:4_wait_ready`, `waited_s=6.41` | `reports/failover-events.jsonl:4` |
| DNS cutover | +25.9s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+28.3s** | dòng `ok:true` đầu sau lỗi, `served_by=b` | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `28.3s` | 300s (5 phút) | **PASS** (dư 271.7s) |
| RPO — Vector DB | `6.0s` / `3` doc | 300s (5 phút) | **PASS** |

RPO lấy từ dòng `2_restore_snapshot` (`rpo_seconds=6.0`, `docs_lost=3`, `embed_model_version=embed-model=vi-e5-base@v3`)
ở `reports/failover-events.jsonl:2`. Snapshot được restore là chu kỳ replicate thứ 2
(`reports/replication.jsonl:2`, chụp lúc +12.7s); primary vẫn nhận thêm 3 doc tới +18.6s trước khi bị
restore so sánh. `tools/measure_rto.py` trả `"valid": true`, `"warnings": []` (`reports/measure-drill-2.json`).

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 14.9s (floor 15.0s) | `interval_s × threshold` = 5 × 3 trong `reports/health-events.jsonl:2` | Giảm interval (vd 2s × 3 = 6s) — đổi lại tăng tải probe và rủi ro flapping; hoặc kết hợp tín hiệu lỗi từ edge/loadgen |
| Runbook xác nhận + mở incident | 4.0s | alert +14.9s → `1_verify_target` +18.9s (vòng probe 2s + timeout 2s của runbook) | Cho runbook subscribe alert thay vì poll file; rút timeout probe |
| Snapshot restore | 0.5s | `1_verify_target` +18.9s → `3_scale_pool` +19.4s (`reports/failover-events.jsonl:3`) | Đã nhỏ vì fs copy; với S3 thật thì pre-stage weights ở region phụ (warm standby) |
| GPU pool warm-up | 6.4s | `waited_s=6.41` ở `4_wait_ready` (`reports/failover-events.jsonl:4`) | Giữ region phụ ở `pool_state=full` (hot standby) — tốn tiền GPU idle |
| DNS/LB TTL cache | 2.4s | t_recovered − t_cutover = 28.3 − 25.9 (+ ~0.1s ghi file cutover) | Giảm `EDGE_TTL_SECONDS`, hoặc LB chủ động invalidate cache khi cutover |
| **Tổng** | **28.3s** | 14.9 + 4.0 + 0.5 + 6.4 + 0.1 + 2.4 | = `rto_measured_s` của `tools/measure_rto.py` |
