# npu-top 0.3.0

[中文](npu-top-0.3.0.md)

Monitoring now shares SSH authentication with remote-dev and coordinator. A password is configured once through remote-dev, which persists it and installs a public key where supported. Password-only hosts continue using the saved password. The monitor database retains only `auth_ref`; routine collection never installs or repairs remote keys and does not create another credential store.

Batch onboarding uses a masked password field, accepts one common password per batch, and clears the input after submission. Per-server results distinguish key and password access while preserving completed authentication if later registration or scheduling fails. Duplicate submissions are blocked while pending. Explicit checks use a fresh SSH handshake instead of treating a cached connection as proof of current credentials.

Database migration adds the authentication-reference column automatically. Older hosts without a shared profile keep their existing monitor key and `known_hosts`. Shared profiles take precedence once configured. Custom bootstrap commands and monitor-owned askpass code are retired. Bidirectional coordinator inventory synchronization preserves the same reference. A reference that cannot resolve under the service user is explicitly reported as not ready.

The shared plaintext secret file defaults to `~/.mindie/ssh/credentials.json`, with POSIX directory/file modes `0700`/`0600`. Components must use the same user and shared paths. No system keychain or unlock prompt is used. Native Windows and WSL retain separate home directories.

This component pins the formal remote-dev 0.11.1 release at Git commit `2a750ec8c74d7f053410d48dd820aeb542e7d3b1`. Mindie-diagnostics is pinned to 0.5.4 at `5ae643e19223f2181ecdbaa3edaf464137c4805e`. Local acceptance passed 239 Python tests, 60 subtests, six frontend behavior tests, lint, TypeScript checks, the frontend build, and fixed benchmark workloads.

The Windows CI check of the actual Git executable exposed the Git for Windows 2.56 `ucrt64/bin` layout issue. Remote-dev 0.11.1 fixes SSH selection from Git Bash as well as PowerShell, and CI retains the installed-runtime path check.

Shared authentication passed real SSH acceptance on all three platforms. macOS passed seven tests using installed wheels for the three components and the Kimi candidate (34.58 seconds). Native Windows PowerShell passed four tests (14.186 seconds), and real WSL2 Ubuntu 24.04 passed four tests (12.694 seconds), with no failures or skips on either. [Acceptance run 40774308378](https://github.com/mindie-agent/remote-dev/actions/runs/40774308378) tested the same source tree as remote-dev candidate `a7564f49b1ff106e59f72961c851b7b11eb81130`. The real path covers coordinator onboarding, two Kimi sessions and virtual environments, and npu-top explicit connections and read-only probes after reopening its database, all reusing the default `credentials.json` under an isolated `REMOTE_DEV_AUTH_DIR`. Automatic public-key installation, password-only access, and revoked-key repair are also covered. Codex, Claude Code, and DSH are tested through endpoint profiles, without claiming complete native-host end-to-end acceptance. The tag workflow generates formal benchmark reports from clean source and ships them with the release.
