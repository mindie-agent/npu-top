# Contributing to `npu-top`

`npu-top` uses the **`device-tool`** repository governance profile (`DEV-ZERO-CTX`, `DEV-READONLY-PATH`, `DEV-TOPOLOGY`, `DEV-BOUNDED-MEM`).

## 1. Flat Sibling + `.worktrees/` Development Layout

Keep the base `../npu-top` checkout clean and develop inside a linked Git worktree:

```bash
git -C ../npu-top fetch origin
git -C ../npu-top worktree add -b feat/<topic> ../.worktrees/npu-top-<topic> origin/main
```

When launching a sandboxed agent from `mindie-agent/`, grant write access to **both** `../npu-top` (Git `commondir`) and `../.worktrees/npu-top-<topic>`.

## 2. Local Verification & Installed-Wheel Smoke Test

1. **Tier 0 & Tier 1 isolated backend + frontend tests**:
   ```bash
   python3 -m pytest -q -m "not live_npu"
   npm test
   ```
   Tests automatically isolate `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_*`, `MINDIE_*`, `REMOTE_DEV_*`, `UV_CACHE_DIR`, and `TMPDIR` via `tests/conftest.py`.

2. **Installed-wheel smoke test outside the checkout**:
   ```bash
   npm run build && uv build
   uv venv /tmp/npu-top-smoke-venv
   uv pip install --python /tmp/npu-top-smoke-venv/bin/python dist/*.whl
   (cd /tmp && HOME=/tmp/npu-top-home /tmp/npu-top-smoke-venv/bin/python -c \
     "import npu_top; from npu_top.settings import Settings; assert 'site-packages' in npu_top.__file__; assert Settings.load().state_dir.name == 'npu-top'")
   ```

3. **Tier 2 standing A2/A3 read-only NPU verification** (when lab network is reachable):
   ```bash
   python3 -m pytest -q -m live_npu
   ```

4. **Unix `LF` & Pre-PR Checklist (`device-tool`)**:
   ```bash
   python3 .github/scripts/check_lf_eol.py
   python3 .github/scripts/check_pr_checklist.py --profile device-tool --body-file /path/to/pr_body.md
   ```
   Documentation-only PRs may record `not-applicable: docs-only` on unaffected hardware items.
