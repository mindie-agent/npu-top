# npu-top 0.3.0

[中文](npu-top-0.3.0.md)

Monitoring now shares SSH authentication with remote-dev and coordinator. A password is configured once through remote-dev, which persists it and installs a public key where supported. Password-only hosts continue using the saved password. The monitor database retains only `auth_ref`; routine collection never installs or repairs remote keys and does not create another credential store.

Batch onboarding uses a masked password field, accepts one common password per batch, and clears the input after submission. Per-server results distinguish key and password access while preserving completed authentication if later registration or scheduling fails. Duplicate submissions are blocked while pending. Explicit checks use a fresh SSH handshake instead of treating a cached connection as proof of current credentials.

Database migration adds the authentication-reference column automatically. Older hosts without a shared profile keep their existing monitor key and `known_hosts`. Shared profiles take precedence once configured. Custom bootstrap commands and monitor-owned askpass code are retired. Bidirectional coordinator inventory synchronization preserves the same reference. A reference that cannot resolve under the service user is explicitly reported as not ready.

The shared plaintext secret file defaults to `~/.mindie/ssh/credentials.json`, with POSIX directory/file modes `0700`/`0600`. Components must use the same user and shared paths. No system keychain or unlock prompt is used. Native Windows and WSL retain separate home directories.

Before release, remote-dev 0.11.0 must have a formal release and the candidate dependency range must be replaced with its exact Git commit. Mindie-diagnostics is pinned to 0.5.4 at `5ae643e19223f2181ecdbaa3edaf464137c4805e`. Current local acceptance passed 239 Python tests, 60 subtests, six frontend behavior tests, lint, TypeScript checks, the frontend build, and fixed benchmark workloads. Native Windows PowerShell and real WSL acceptance are still pending; final notes must record platform and source-revision evidence. The tag workflow generates and ships formal benchmark reports.
