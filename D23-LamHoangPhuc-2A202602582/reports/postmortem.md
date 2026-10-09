# Postmortem — DR Drill Lab 23 (Lâm Hoàng Phúc — 2A202602582)

Theo template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

**Tóm tắt:** Drill 2 ngày 2026-10-09, region-a bị netblock (SIGSTOP) lúc 04:50:33 UTC trong khi
loadgen chạy 2 req/s qua edge. Health checker phát hiện sau 14.9s, runbook `--auto` failover sang
region-b, request đầu tiên thành công từ region-b ở +28.3s. **RTO 28.3s, RPO 6.0s / 3 doc — PASS cả hai.**
Đối chứng drill 1 (không DR): `NO_RECOVERY`, 16/33 request fail và không bao giờ hồi phục.

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time (UTC) | +s | Sự kiện | Evidence |
|---|---:|---|---|
| 2026-10-09T04:50:33 | 0 | outage bắt đầu (kill region-a, netblock) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T04:50:34 | 2.0 | user đầu tiên bị ảnh hưởng (503 ReadTimeout ~2s) | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T04:50:47 | 14.9 | health check alert: region-a UNHEALTHY (3 fail liên tiếp) | `reports/health-events.jsonl:2` |
| 2026-10-09T04:50:51 | 18.8 | runbook xác nhận outage qua alert + mở incident SEV1 | `reports/runbook-run.jsonl:1`, `reports/runbook-run.jsonl:2` |
| 2026-10-09T04:50:51 | 18.9 | operator confirm cutover (`--auto`) → failover bắt đầu `1_verify_target` | `reports/failover-events.jsonl:1` |
| 2026-10-09T04:50:52 | 19.4 | restore snapshot xong (rpo 6.0s, 3 doc mất) | `reports/failover-events.jsonl:2` |
| 2026-10-09T04:50:58 | 25.8 | region-b ready sau warm-up 6.41s | `reports/failover-events.jsonl:4` |
| 2026-10-09T04:50:58 | 25.9 | DNS cutover `active_region=b` | `reports/failover-events.jsonl:5` |
| 2026-10-09T04:51:00 | 27.1 | golden signals region-b: 10/10 OK, p95 122.3ms | `reports/runbook-run.jsonl:6` |
| 2026-10-09T04:51:01 | 28.3 | resolved — request đầu tiên OK, `served_by=b` | `reports/drill-2-withdr.jsonl:39` |

Độ trễ thông báo (operator biết tin − t_outage) = 18.8s, trong đó 14.9s là detect floor của
health checker, 3.9s là runbook poll alert.

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.3s` · gap: `-271.7s` (đạt, dư 90%)
- RPO mục tiêu: 300s · đo được: `6.0s` (`3` doc bị mất) · gap: `-294.0s` (đạt)
- **Bước tốn nhiều giây nhất:** health-check detect (14.9s ≈ 53% RTO). Vì checker cố ý đợi
  3 lần fail liên tiếp × 5s để chống flapping; với netblock, mỗi probe còn treo thêm tới timeout 2s.
  Bước lớn thứ hai là GPU pool warm-up 6.4s (23%) — region-b là warm standby, không phải hot.

Phân rã RTO (chi tiết ở `reports/rto-evidence.md`): detect 14.9s + runbook confirm 4.0s +
restore 0.5s + warm-up 6.4s + cutover 0.1s + DNS/edge TTL 2.4s = 28.3s.

Lưu ý RPO: RPO lý thuyết là chu kỳ replicate 30s; lần này đo được 6.0s chỉ vì snapshot gần nhất
rơi gần thời điểm restore. RPO sẽ dao động 0–30s (+ thời gian ingest) giữa các lần chạy.

## 3. Root cause (5 whys)

Câu hỏi: *nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại hoặc chậm?*

1. Vì sao user bị lỗi 28s? → Vì traffic vẫn trỏ về region-a cho tới khi DNS cutover ở +25.9s và cache edge hết TTL.
2. Vì sao cutover không sớm hơn? → Vì phải đợi (a) health checker xác nhận outage 15s, (b) region-b warm-up 6.4s.
3. Vì sao region-b phải warm-up và restore? → Vì kiến trúc là active-passive warm standby: B chạy process nhưng
   không có data/weights và pool ở `warm` (đã thấy ở Bước 1: `count:0, weights:false`).
4. Vì sao data ở B không có sẵn? → Replication chỉ đẩy snapshot lên object store mỗi 30s, không restore liên tục
   vào B; và trong outage thật, `rpo()` cần đọc DB của region-a — **sẽ không đọc được** nếu cả đĩa region-a mất,
   khi đó docs_lost không đo được chính xác mà chỉ ước lượng được bằng `replication lag`.
5. Vì sao chấp nhận thiết kế này? → Đánh đổi chi phí: giữ GPU pool full ở 2 region tốn gấp đôi. Root cause
   hệ thống: **RTO bị chi phối bởi detection policy + warm-standby**, và **việc đo RPO phụ thuộc vào region
   đã chết** — đó là điểm runbook sẽ thất bại trong outage thật (bước 2 của failover).

Điểm yếu khác phát hiện được: runbook phụ thuộc file `reports/health-events.jsonl` cục bộ để nhận alert;
nếu health checker chạy ở máy khác hoặc chết, runbook rơi về fallback tự probe sau 60s (RTO tăng ~60s).

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Giảm health-check interval 5s → 2s (giữ threshold 3), probe timeout 2s → 1s | SRE on-call lead | 2026-10-23 | RTO −9s (floor 15s → 6s) |
| 2 | Runbook nhận alert qua webhook/queue thay vì poll file mỗi 2s | Platform team | 2026-10-30 | RTO −3 đến −4s |
| 3 | Giữ region-b ở `pool_state=full` trong giờ cao điểm (hot standby theo lịch) | Infra + FinOps | 2026-11-06 | RTO −6.4s (bỏ warm-up), đổi lại chi phí GPU idle |
| 4 | Edge invalidate cache ngay khi `active_region` đổi (hoặc TTL 5s → 1s) | Network/edge owner | 2026-10-30 | RTO −2 đến −4s |
| 5 | Replicate 30s → 10s, ghi `latest_doc_ts` vào MANIFEST để tính RPO mà không cần đọc DB primary | Data platform | 2026-11-13 | RPO tối đa −20s; RPO đo được cả khi region-a mất đĩa |
| 6 | Chạy game day hàng tháng với `--mode stop` và `netblock`, ngẫu nhiên thời điểm kill | SRE | 2026-11-30 | Không giảm trực tiếp — giữ con số RTO đáng tin |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   5s × 3 = **15s** detect floor. Đo được detect ở +14.9s (`reports/health-events.jsonl:2`) = **52.7%** của RTO 28.3s.
   (14.9 < 15 vì probe lỗi đầu tiên tình cờ rơi ngay sau t_outage.) Với RTO mục tiêu 300s, về lý thuyết có thể
   chọn interval tới ~(300 − 15s restore/warm-up/TTL) / 3 ≈ 90s, nhưng nên giữ ≤ 10s để còn dư cho bước thủ công.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì?**
   Floor từ 15s → 3s, RTO giảm ~12s (còn ~16s). Cái giá (§4 flapping): 3 lần fail trong 3s rất dễ đến từ GC pause,
   một đợt deploy, hay network blip ngắn → false positive → failover không cần thiết, và nếu rollback cũng tự động
   thì traffic flap A↔B liên tục, mỗi lần flap lại tốn warm-up + TTL. Ngoài ra probe tăng 5× tải lên `/readyz`
   (đọc SQLite mỗi lần). Muốn giảm detect an toàn hơn: giữ interval vừa phải nhưng thêm tín hiệu thứ hai
   (error rate từ edge) và giữ bước confirm bán tự động.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   `docs_lost=3` nghĩa là 3 ticket khách gửi trong 6.0s cuối trước khi restore **biến mất vĩnh viễn** — RAG ở region-b
   không bao giờ trả lời được dựa trên chúng, và khách sẽ phải gửi lại. Trong outage thật con số này có thể lên
   tới cả chu kỳ replicate (30s ≈ 15 doc ở 0.5 doc/s), và nếu đĩa region-a mất thì ta thậm chí không biết chính
   xác doc nào mất — chỉ biết cửa sổ thời gian. Cần thông báo cho khách hàng bị ảnh hưởng theo cửa sổ thời gian
   đó, và 6 giờ chạy trên region-b nghĩa là mọi ingest mới chỉ nằm ở B — khi failback về A phải replicate ngược
   B → A trước, nếu không sẽ mất tiếp dữ liệu của 6 giờ đó.
