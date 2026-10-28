/**
 * [Role] Convert batch onboarding receipts into endpoint-specific UI results.
 * [Invariants] Preserve authentication outcomes and later failures independently.
 * [Timeouts & Locks] Pure synchronous mapping; no timers or locks.
 * [Side-Effect Policy] Does not mutate inputs or perform I/O.
 */
export type OnboardingServer = { name: string; host: string; port: number; username: string };
export type OnboardingReceipt = {
  input_index: number;
  registration: string;
  auth: { ok: boolean; state?: string; installation?: string; error?: string;
    auth_method?: string; bootstrap?: { installation?: string } };
  failure?: { stage: string; error: string };
};
export type OnboardingOutcome = { endpoint: string; status: 'success' | 'warning' | 'error'; message: string };

export function onboardingOutcomes(servers: OnboardingServer[], receipts: OnboardingReceipt[]): OnboardingOutcome[] {
  const indexes = new Set(receipts.map((item) => item.input_index));
  if (receipts.length !== servers.length || indexes.size !== servers.length ||
      receipts.some((item) => !Number.isInteger(item.input_index) || !servers[item.input_index] || typeof item.auth?.ok !== 'boolean')) {
    throw new Error('服务器添加回执不完整，请先核对清单');
  }
  return [...receipts].sort((a, b) => a.input_index - b.input_index).map((item) => {
    const server = servers[item.input_index];
    const host = server.host.includes(':') ? `[${server.host}]` : server.host;
    const endpoint = `${server.username}@${host}:${server.port}`;
    if (item.auth.ok) {
      const method = item.auth.auth_method === 'password' ? '密码连接已验证' : item.auth.auth_method === 'publickey' ? '密钥连接已验证' : 'SSH 连接已验证';
      return { endpoint, status: item.failure ? 'warning' : 'success', message: `${method}${item.failure ? `；${item.failure.error}` : ''}` };
    }
    const installed = (item.auth.bootstrap?.installation || item.auth.installation) === 'completed';
    const outcome = installed ? '密钥已安装；' : item.auth.state === 'unknown' ? '远端写入结果不确定，请先核对；' : '';
    // Preserve the authentication error even if saving the failure also failed.
    const errors = [item.auth.error, item.failure?.error].filter(Boolean).join('；');
    return { endpoint, status: 'error', message: `${outcome}${errors || '未完成'}` };
  });
}
