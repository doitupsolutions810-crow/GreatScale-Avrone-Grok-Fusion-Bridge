# Round 6 summary

Docker verdict: **docker is not installed in this environment**.
Rounds 3–6 ran against in-process stand-ins. The compose file describes the real stack (Avrone image pin, 256m/0.5 CPU, toxiproxy, read-only rootfs, test-runner) for a machine where Docker works.

## This run

- Drill runtime: 222.5s
- Soak: 300/300 forwarded, dupes=0, p50=0.444 p95=0.684 p99=0.875 ms, 376.328 ev/s, OOMKilled=false restarts=0 rss_kb=27012
- Toxiproxy mock: mock toxiproxy 0-500ms on the blackboard hop plus RSTs every 29 requests, 500 cycles, delivered 40/40, p50=0.632 p95=101.235 p99=501.44 ms
- Bench: shim 2047.352 ev/s, mock container proxy 1161.356 ev/s, stored 10000 (mock-proxy)
- Watchdog: parent watchdog pinged every 2s, restarted a killed service, restarted frozen uptime, and backed off a dead service [1.0, 2.0, 4.0]
- Worst failure mode: toxiproxy (mock toxiproxy 0-500ms on the blackboard hop plus RSTs every 29 requests, 500 cycles, delivered 40/40, p50=0.632 p95=101.235 p99=501.44 ms)
- Known xfails: lone_surrogate, invalid_utf8, dedup_source_id

| Phase | Round | Real or mock | Status | Detail |
| --- | --- | --- | --- | --- |
| pytest | unit | local | pass | 45 passed in 29.35s |
| integration | unit | local | pass | PASS: curiosity=0.520, forwarded=1, seen=3 |
| soak | R3 | mock | pass | 300/300 forwarded, dupes=0, p50=0.444 p95=0.684 p99=0.875 ms, 376.328 ev/s, OOMKilled=false restarts=0 rss_kb=27012 |
| partition | R3 | mock | pass | mock 50% SYN-drop (RST on accept) for 10s; delivered 20/20 errors=175 |
| dns_failure | R3 | mock | pass | AVRONE host failed DNS for 10s then delivered 1/1 |
| toxiproxy | R4 | mock | pass | mock toxiproxy 0-500ms on the blackboard hop plus RSTs every 29 requests, 500 cycles, delivered 40/40, p50=0.632 p95=101.235 p99=501.44 ms |
| cpu_starvation | R4 | mock | pass | stalls timed out (4 errors) then delivered 1/1 |
| image_pull | R4 | mock | pass | docker is not installed in this environment; pull_ok=False; shim fallback forwarded 1/1 |
| volume_permission | R4 | mock | pass | state dir mode 000; forwarded 1 state_errors=6 |
| health_503 | R4 | mock | pass | http unhealthy for 30s then delivered 1/1 |
| slow_start | R6 | mock | pass | bind unhealthy for 30s then delivered 1/1 |
| compose_dns | R6 | mock | pass | hostnames avrone and cognitive-bridge resolved via FUSION_HOST_MAP; forwarded 1 |
| watchdog | R5 | parent | pass | parent watchdog pinged every 2s, restarted a killed service, restarted frozen uptime, and backed off a dead service [1.0, 2.0, 4.0] |

## Edge cases

| Case | Status | Detail |
| --- | --- | --- |
| clock_skew | pass | ±1h timestamps stored as given |
| idempotency_conflict | pass | same key + different body is 409; original kept |
| token_rotation | pass | previous token worked, then was removed |
| half_open | pass | idle TCP dropped; next request accepted |
| slowloris | pass | slow request body timed out; server still accepts |
| disk_full | pass | blackboard write returns 507 ENOSPC and stores nothing |
| state_enospc | pass | ENOSPC on the state file is counted; the event still forwards |
| sigterm | pass | SIGTERM finished the in-flight post and wrote state |
| corrupt_state | pass | corrupt state is discarded; the post is forwarded again |
| burst_10000 | pass | 10000+idle+10000 stored exactly, replay of b0 is a duplicate |
| mixed_case_headers | pass | mixed-case admin and idempotency headers accepted |
| gzip_body | pass | gzip body inflated under the cap and stored |
| http10 | pass | HTTP/1.0 client received HTTP/1.0 and the event was stored |
| cap_before_auth | pass | 4097 bytes is 413 with a bad token |
| single_relay_lock | pass | a second process cannot lock the same blackboard |
| readonly_rootfs | pass | mock read-only rootfs rejected a write; relay state on tmpfs delivered |
| dedup_correct | pass | harness dedup is (source, id) |
| lone_surrogate | xfail | known upstream: lone-surrogate post makes Avrone GET /api/blackboard 500 |
| invalid_utf8 | xfail | known upstream: CK invalid UTF-8 returns 500 |
| dedup_source_id | xfail | known upstream: dedup is moving to (source, id); id-only peers still collapse sources |

## Earlier rounds (not re-measured here)

- 500, 1000, and 2000 events were delivered exactly, with 0 duplicates.
- Edge cases that round: 40 passed out of 42.
- A 30-minute soak delivered 6880/6880 with p99 29 ms.
- On the user's Mac, the pinned image at 256m and 0.5 CPU had OOMKilled=false.
- The read-only rootfs run on that machine worked: the relay functioned and the container could not write.

This run passed (3 known xfails). Tag: `v0.6.0-edge-hardened`.
