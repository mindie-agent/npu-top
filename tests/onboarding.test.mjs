import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { URL } from 'node:url';
import ts from 'typescript';

const { outputText } = ts.transpileModule(
  readFileSync(new URL('../app/onboarding.ts', import.meta.url), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } },
);
const { onboardingOutcomes } = await import(`data:text/javascript,${encodeURIComponent(outputText)}`);

const servers = [
  { name: 'one', host: '192.0.2.1', port: 22, username: 'root' },
  { name: 'two', host: '2001:db8::2', port: 2222, username: 'ops' },
];
const receipt = (input_index, auth, failure) => ({ input_index, registration: 'completed', auth, failure });

test('matches mixed results to endpoints even when receipts arrive out of order', () => {
  const rows = onboardingOutcomes(servers, [
    receipt(1, { ok: false, error: '密码认证被拒绝' }),
    receipt(0, { ok: true }),
  ]);
  assert.deepEqual(rows.map((row) => row.endpoint), ['root@192.0.2.1:22', 'ops@[2001:db8::2]:2222']);
  assert.equal(rows[0].status, 'success');
  assert.equal(rows[1].status, 'error');
  assert.match(rows[1].message, /密码认证被拒绝/);
});

test('preserves verified auth when scheduling later fails', () => {
  const [row] = onboardingOutcomes(servers.slice(0, 1), [
    receipt(0, { ok: true }, { stage: 'schedule_observation', error: '采集调度失败' }),
  ]);
  assert.equal(row.status, 'warning');
  assert.match(row.message, /密钥连接已验证.*采集调度失败/);
});

test('preserves both the original auth error and its recording failure', () => {
  const [row] = onboardingOutcomes(servers.slice(0, 1), [
    receipt(0, { ok: false, installation: 'completed', error: '密钥登录验证失败' },
      { stage: 'record_auth_failure', error: '数据库写入失败' }),
  ]);
  assert.match(row.message, /密钥已安装.*密钥登录验证失败.*数据库写入失败/);
});

test('explains uncertain installation separately from authentication rejection', () => {
  const [row] = onboardingOutcomes(servers.slice(0, 1), [
    receipt(0, { ok: false, state: 'unknown', error: '连接中断' }),
  ]);
  assert.equal(row.status, 'error');
  assert.match(row.message, /远端写入结果不确定.*连接中断/);
});

test('rejects incomplete, duplicate, invalid and out-of-range receipts', () => {
  const good = receipt(0, { ok: true });
  for (const receipts of [[good], [good, good], [good, receipt(2, { ok: true })],
    [good, receipt(-1, { ok: true })], [good, receipt(1, {})]]) {
    assert.throws(() => onboardingOutcomes(servers, receipts), /回执不完整/);
  }
});
