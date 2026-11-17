## 变更摘要 (Summary)

- Describe the root cause / trigger and the resulting behavioral change.

## 设计原则 Review (Pre-PR Checklist — `device-tool`)

- [ ] **DEV-ZERO-CTX** — Control paths and observation probes never initialize NPU runtime contexts, submit device work, or allocate HBM (`allocation_authority = False`, `Phy-ID 0` HBM delta `== 0 MiB`).
- [ ] **DEV-READONLY-PATH** — Default state directory uses owned MindIE/XDG storage (adopting `./data` only when a verified `npu-top` SQLite DB already exists without creating WAL sidecars), and SQLite failures preserve live probe observations (`receipt_unconfirmed=True`).
- [ ] **DEV-TOPOLOGY** — Ascend A2/A3/310P/A5 physical/logical topology (`Phy-ID` package pairs, DCMI masks, per-card exit codes) remains exact without temperature-based or fabricated fallbacks.
- [ ] **DEV-BOUNDED-MEM** — Historical telemetry windows stream via bounded SQLite cursors (`<= 512 MiB`), sequential probe passes share one monotonic collection deadline (`outer >= sum(inner) + grace`), core modules stay `< 800` lines, and all files use Unix `LF`.

## 证据记录 (Evidence)

- **DEV-ZERO-CTX**: `<command / test and measured output, or not-applicable: docs-only>`
- **DEV-READONLY-PATH**: `<command / test and observed filesystem/receipt state, or not-applicable: docs-only>`
- **DEV-TOPOLOGY**: `<command / golden fixture or live A2/A3 result, or not-applicable: docs-only>`
- **DEV-BOUNDED-MEM**: `<command / memory & deadline verification, or not-applicable: docs-only>`

## 验证结果 (Verification)

- Executed test suites (`pytest`, `npm test`), installed-wheel smoke test, LF check, and any unexecuted live-hardware scope.
