# npu-top 0.3.2

[中文](npu-top-0.3.2.md)

## Telemetry Coverage Honesty, Local State Boundaries & DCMI Timeout Arithmetic

This release resolves the cross-repo Design Principles audit items for `npu-top` ([`mindie-agent#332`](https://github.com/mindie-agent/mindie-agent/issues/332))—eliminating fake all-card coverage on malformed `cards_json`, reserving `unknown` strictly for uncertain remote writes, and enforcing `outer >= inner + kill_after + grace` on DCMI probes—and adopts protected `main` release verification:

- **Refuse Fake All-Card Coverage on Damaged or Illegal `cards_json` ([#34](https://github.com/mindie-agent/npu-top/pull/34))**:
  - `telemetry_window` strictly validates `cards_json` decoding and integer NPU ID lists; runs with unparseable (`cards_json_unparseable`) or non-integer-list (`cards_json_invalid_filter`) card filters are excluded rather than matching every device, keeping scheduled `coverage` as `null` (`coverage_basis="cards_json_unparseable"`) and naming the affected `run_id`s in `measurement_limitations`.
- **Reserve `unknown` Strictly for Uncertain Remote Writes ([#34](https://github.com/mindie-agent/npu-top/pull/34))**:
  - `apply_inventory_exception` re-raises `TimeoutError` (mapped to `503 storage_timeout` on batch POST), records local SQLite registration failures as `registration="failed"`, and records unexecuted `connect()` exceptions as `auth.state="not_ready"`, preserving `"unknown"` exclusively when `submission_state == "uncertain"`.
- **DCMI Probe Timeout Arithmetic ([#34](https://github.com/mindie-agent/npu-top/pull/34))**:
  - `probe` sets the optional DCMI SSH outer timeout to `inner (5s) + kill_after (1s) + cleanup_grace (3s) = 9s`, returning `ConfiguredTimeoutTooSmall` when the configured SSH timeout cannot cover that sum.
- **Protected Release Pipeline ([#35](https://github.com/mindie-agent/npu-top/pull/35), [#36](https://github.com/mindie-agent/npu-top/pull/36))**:
  - Enforce protected-`main` publication policy checks, real old-client upgrade verification, and explicit `MINDIE_RELEASE_DEPLOY_KEY` secret forwarding.

Actual test and benchmark results for this release are recorded in the bundled `benchmark-results.json`, `benchmark-results.md`, and CI runs.
