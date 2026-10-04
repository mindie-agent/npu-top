'use client';

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';

type Chip = {
  chip_id?: number;
  phy_id?: number;
  bus_id?: string;
  aicore_percent?: number;
  hbm?: { used_mb?: number; total_mb?: number };
};
type OwnershipLabel = { value: string; kind: 'employee_id' | 'initials'; sources?: Array<'pwd' | 'container'> };
type NpuProcess = {
  pid: number;
  name?: string;
  npu_process_name?: string;
  npu_memory_mb?: number;
  user?: string;
  cwd?: string;
  command?: string;
  executable?: string;
  container?: { id?: string; short_id?: string; name?: string; image?: string; status?: string; source?: string };
  ownership_labels?: OwnershipLabel[];
};
type Device = {
  npu_id: number;
  name?: string;
  health?: string;
  aicore_percent?: number;
  temperature_c?: number;
  power_w?: number;
  busy: boolean;
  hbm?: { used_mb?: number; total_mb?: number };
  chips?: Chip[];
  processes?: NpuProcess[];
};
type Disk = { filesystem: string; mount: string; used_percent: number; available_bytes: number; total_bytes: number };
type Container = { id?: string; name?: string; image?: string; status?: string; state?: string; stats?: { cpu_percent?: string; memory?: string } };
type Summary = Record<string, number | null | undefined>;
type Snapshot = {
  status: string;
  collected_at: number;
  duration_ms?: number;
  error?: string;
  hostname?: string;
  process_details?: { state: string; error?: string; error_type?: string; exit_code?: number };
  recording?: { state: string; error_type?: string; diagnostic_ref?: string };
  summary?: Summary;
  devices?: Device[];
  disks?: Disk[];
  mounts?: unknown[];
  docker?: { available?: boolean; running?: number; containers?: Container[] };
};
type Server = { id: string; name: string; host: string; port: number; username: string; tags: string[]; enabled: boolean; last_seen_at?: number; last_error?: string; status: string; snapshot?: Snapshot };
type Runtime = { mode: string; effective_interval: number; idle_interval: number; history_interval: number; active_viewers: number; collecting: boolean; last_cycle_at?: number; cycle_duration_ms?: number; allowed_intervals: number[]; collector_status?: string; collector_error_type?: string; storage_failures?: number; maintenance_error?: { error_type: string }; inventory?: { state: string; error?: string; error_type?: string } };
type Overview = { generated_at: number; totals: Summary; servers: Server[]; runtime: Runtime };
type HistoryPoint = { bucket: number; cpu_percent?: number; npu_util_percent?: number; memory_percent?: number; hbm_percent?: number; disk_max_percent?: number; busy_npu_count?: number; npu_count?: number };
type HeatmapDevice = { npu_id: number; name?: string; utilization_percent?: number; hbm_percent?: number; busy_percent?: number };
type HeatmapPoint = HistoryPoint & { sample_count: number; devices: HeatmapDevice[] };
type View = 'overview' | 'server' | 'history' | 'servers';
type MetricKey = 'npu_util_percent' | 'hbm_percent' | 'cpu_percent' | 'memory_percent' | 'disk_max_percent';

const API = '';
async function requestJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : `API ${response.status}`);
  return data as T;
}
const LOW_PRIORITY_TAG = '低优先级';
const emptyRuntime: Runtime = { mode: 'idle', effective_interval: 120, idle_interval: 120, history_interval: 30, active_viewers: 0, collecting: false, allowed_intervals: [1, 5, 10, 30] };
const emptyOverview: Overview = { generated_at: 0, totals: { servers: 0, online_servers: 0, npu_count: 0, busy_npu_count: 0, idle_npu_count: 0, hbm_used_mb: 0, hbm_total_mb: 0, npu_util_percent: null }, servers: [], runtime: emptyRuntime };
const metricLabels: Record<MetricKey, string> = { npu_util_percent: 'NPU 利用率', hbm_percent: 'HBM 占用', cpu_percent: 'CPU 利用率', memory_percent: '系统内存', disk_max_percent: '磁盘水位' };

function number(value: number | null | undefined, suffix = '') { return value == null || !Number.isFinite(value) ? '—' : `${Math.round(value)}${suffix}`; }
function precise(value: number | null | undefined, suffix = '') { return value == null || !Number.isFinite(value) ? '—' : `${value.toFixed(1)}${suffix}`; }
function bytes(value: number | null | undefined) { if (value == null) return '—'; const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let size = value; let index = 0; while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; } return `${size >= 10 || index < 2 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`; }
function mb(value: number | null | undefined) { return value == null ? '—' : bytes(value * 1024 * 1024); }
function percent(used?: number | null, total?: number | null) { return used != null && total ? Math.max(0, Math.min(100, used * 100 / total)) : null; }
function clampPercent(value?: number | null) { return value == null || !Number.isFinite(value) ? 0 : Math.max(0, Math.min(100, value)); }
function timeAgo(ts?: number) { if (!ts) return '尚未采集'; const delta = Math.max(0, Math.floor(Date.now() / 1000 - ts)); if (delta < 5) return '刚刚'; if (delta < 60) return `${delta} 秒前`; if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`; if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`; return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }); }
function dateKey(timestamp: number) { const date = new Date(timestamp * 1000); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`; }
function average(points: HistoryPoint[], key: MetricKey) { const values = points.map((point) => point[key]).filter((value): value is number => value != null && Number.isFinite(value)); return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null; }

function StatusDot({ status }: { status: string }) { return <span className={`status-dot status-dot--${status}`} aria-hidden="true" />; }

function CopyButton({ value, label = '复制', className = '' }: { value: string; label?: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  async function copy(event: React.MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.stopPropagation();
    const legacyCopy = () => {
      const input = document.createElement('textarea');
      input.value = value;
      input.style.position = 'fixed';
      input.style.opacity = '0';
      document.body.appendChild(input);
      input.select();
      document.execCommand('copy');
      input.remove();
    };
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(value);
      else legacyCopy();
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      legacyCopy();
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    }
  }
  return <button type="button" className={`copy-button ${className}`} onClick={copy} title={`复制：${value}`} aria-label={`复制 ${value}`}>{copied ? '已复制' : label}</button>;
}

function dieColor(hbmPercent: number | null) {
  const ratio = Math.min(1, clampPercent(hbmPercent) / 50);
  const start = [235, 242, 252];
  const end = [32, 111, 235];
  const channel = (index: number) => Math.round(start[index] + (end[index] - start[index]) * ratio);
  return `rgb(${channel(0)} ${channel(1)} ${channel(2)})`;
}

function NpuDie({ device, chip, detail = false }: { device: Device; chip?: Chip; detail?: boolean }) {
  const telemetry = chip || device;
  const hbm = percent(telemetry.hbm?.used_mb, telemetry.hbm?.total_mb);
  const active = clampPercent(telemetry.aicore_percent) > 0;
  const dieId = chip?.phy_id ?? chip?.chip_id ?? device.npu_id;
  const identity = chip ? `NPU ${device.npu_id} / Die ${dieId}` : `NPU ${device.npu_id}`;
  return <span className={`npu-die ${detail ? 'npu-die--detail' : ''} ${hbm != null && hbm >= 32 ? 'is-dark' : ''}`} style={{ backgroundColor: dieColor(hbm) }} title={`${identity} · HBM ${number(hbm, '%')} · AICore ${number(telemetry.aicore_percent, '%')}`}><b>{dieId}</b>{detail && <small>HBM {number(hbm, '%')}</small>}{active && <i className="npu-die__activity" aria-label="AICore 活跃" />}</span>;
}

function physicalDies(devices: Device[]) {
  return devices.flatMap<{ device: Device; chip: Chip | undefined }>((device) => device.chips?.length ? device.chips.map((chip) => ({ device, chip })) : [{ device, chip: undefined }]);
}

function MiniBar({ value, tone = 'blue' }: { value?: number | null; tone?: 'blue' | 'green' | 'orange' | 'purple' }) {
  return <span className={`mini-bar mini-bar--${tone}`}><i style={{ width: `${clampPercent(value)}%` }} /></span>;
}

function MetricCard({ label, value, suffix, detail, tone = 'blue', icon }: { label: string; value: string | number; suffix?: string; detail: string; tone?: string; icon: string }) {
  return <article className={`metric-card metric-card--${tone}`}><div className="metric-card__head"><span>{label}</span><i aria-hidden="true">{icon}</i></div><strong>{value}<small>{suffix}</small></strong><p>{detail}</p></article>;
}

function ServerTags({ tags, compact = false }: { tags: string[]; compact?: boolean }) {
  if (!tags.length) return null;
  return <span className={`server-tags ${compact ? 'server-tags--compact' : ''}`}>{tags.map((tag) => <span className={tag === LOW_PRIORITY_TAG ? 'is-low-priority' : ''} key={tag}>{tag}</span>)}</span>;
}

function TrendCanvas({ points, metric }: { points: HistoryPoint[]; metric: MetricKey }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const draw = () => {
      const ratio = window.devicePixelRatio || 1;
      const box = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, box.width * ratio);
      canvas.height = Math.max(1, box.height * ratio);
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, box.width, box.height);
      ctx.font = '11px Inter, system-ui, sans-serif';
      ctx.fillStyle = '#84909f';
      ctx.textAlign = 'right';
      for (let index = 0; index <= 4; index += 1) {
        const y = 18 + index * (box.height - 42) / 4;
        ctx.strokeStyle = '#e7ebf0';
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(42, y); ctx.lineTo(box.width - 8, y); ctx.stroke();
        ctx.fillText(`${100 - index * 25}%`, 34, y + 4);
      }
      const usable = points.filter((point) => point[metric] != null);
      if (usable.length < 2) { ctx.textAlign = 'center'; ctx.fillText('当前窗口暂无足够历史样本', box.width / 2, box.height / 2); return; }
      const coordinates = usable.map((point, index) => [42 + index * (box.width - 52) / (usable.length - 1), 18 + (100 - clampPercent(point[metric])) * (box.height - 42) / 100]);
      const gradient = ctx.createLinearGradient(0, 12, 0, box.height);
      gradient.addColorStop(0, 'rgba(32, 111, 235, .26)');
      gradient.addColorStop(1, 'rgba(32, 111, 235, 0)');
      ctx.beginPath(); ctx.moveTo(coordinates[0][0], box.height - 24); coordinates.forEach(([x, y]) => ctx.lineTo(x, y)); ctx.lineTo(coordinates.at(-1)![0], box.height - 24); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill();
      ctx.beginPath(); coordinates.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.strokeStyle = '#206feb'; ctx.lineWidth = 2.5; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.stroke();
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [points, metric]);
  return <canvas className="trend-canvas" ref={ref} aria-label={`${metricLabels[metric]}历史趋势`} />;
}

function buildDays(dayCount: number) {
  const end = new Date(); end.setHours(0, 0, 0, 0);
  return Array.from({ length: dayCount }, (_, index) => { const date = new Date(end); date.setDate(end.getDate() - (dayCount - index - 1)); return { key: dateKey(date.getTime() / 1000), label: `${date.getMonth() + 1}/${date.getDate()}`, full: `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日` }; });
}

function heatLevel(value: number | null) { if (value == null) return 'empty'; if (value < 3) return '0'; return String(Math.min(5, Math.ceil(clampPercent(value) / 20))); }

function HistoryHeatmap({ title, subtitle, points, days, tone, valueFor }: { title: string; subtitle: string; points: HeatmapPoint[]; days: ReturnType<typeof buildDays>; tone: 'blue' | 'green' | 'purple' | 'orange'; valueFor: (point: HeatmapPoint) => number | null }) {
  const index = useMemo(() => new Map(points.map((point) => { const date = new Date(point.bucket * 1000); return [`${dateKey(point.bucket)}:${Math.floor(date.getHours() / 2)}`, point] as const; })), [points]);
  const cellSize = days.length > 45 ? 9 : days.length > 14 ? 11 : 13;
  return <article className={`heatmap-card heatmap-card--${tone}`}><header><div><h3>{title}</h3><p>{subtitle}</p></div><span>2h AVG</span></header><div className="heatmap-scroll"><div className="heatmap-grid" style={{ gridTemplateColumns: `26px repeat(${days.length}, ${cellSize}px)` }}><i />{days.map((day, dayIndex) => <b key={day.key} title={day.full}>{dayIndex % (days.length > 35 ? 7 : days.length > 14 ? 4 : 1) === 0 || dayIndex === days.length - 1 ? day.label : ''}</b>)}{Array.from({ length: 12 }, (_, row) => [<em key={`time-${row}`}>{String(row * 2).padStart(2, '0')}</em>, ...days.map((day) => { const point = index.get(`${day.key}:${row}`); const value = point ? valueFor(point) : null; const startHour = String(row * 2).padStart(2, '0'); const endHour = String((row + 1) * 2).padStart(2, '0'); return <span key={`${day.key}-${row}`} className={`heat-cell heat-cell--${heatLevel(value)}`} title={`${day.full} ${startHour}:00–${endHour}:00 · ${value == null ? '无样本' : `${value.toFixed(1)}%`}`} />; })])}</div></div><footer><span>低</span>{Array.from({ length: 6 }, (_, level) => <i className={`heat-cell--${level}`} key={level} />)}<span>高</span></footer></article>;
}

function ServerCard({ server, onOpen, onCollect }: { server: Server; onOpen: () => void; onCollect: () => void }) {
  const summary = server.snapshot?.summary;
  const devices = server.snapshot?.devices || [];
  const dies = physicalDies(devices);
  const hbm = percent(summary?.hbm_used_mb, summary?.hbm_total_mb);
  return <article className={`server-card server-card--${server.status}`}><button className="server-card__open" onClick={onOpen} aria-label={`查看 ${server.name} 详情`}><header><div className="server-card__identity"><StatusDot status={server.status} /><div><h3>{server.name}</h3><p>{server.snapshot?.hostname || server.host}</p><ServerTags tags={server.tags} compact /></div></div><span className="chevron">›</span></header><div className="device-strip" style={dies.length ? { gridTemplateColumns: `repeat(${dies.length}, minmax(0, 1fr))` } : undefined} aria-label={`${devices.length} 张 NPU，${dies.length} 个 die`}>{dies.length ? dies.map(({ device, chip }) => <NpuDie key={`${device.npu_id}-${chip?.phy_id ?? chip?.chip_id ?? "logical"}`} device={device} chip={chip} />) : <p>{server.last_error || '等待首次设备采样'}</p>}</div><div className="server-card__metrics"><div><span>CPU</span><strong>{number(summary?.cpu_percent, '%')}</strong><MiniBar value={summary?.cpu_percent} /></div><div><span>内存</span><strong>{number(percent(summary?.memory_used_bytes, summary?.memory_total_bytes), '%')}</strong><MiniBar value={percent(summary?.memory_used_bytes, summary?.memory_total_bytes)} tone="purple" /></div><div><span>HBM</span><strong>{number(hbm, '%')}</strong><MiniBar value={hbm} tone="green" /></div></div></button><footer><span>{dies.filter(({ device, chip }) => clampPercent((chip || device).aicore_percent) > 0).length} die 活跃 · {dies.filter(({ device, chip }) => clampPercent((chip || device).aicore_percent) === 0).length} die 可用 · {timeAgo(server.snapshot?.collected_at || server.last_seen_at)}</span><button onClick={onCollect}>立即采集</button></footer></article>;
}

export default function Dashboard() {
  const [overview, setOverview] = useState<Overview>(emptyOverview);
  const [view, setView] = useState<View>('overview');
  const [selectedServerId, setSelectedServerId] = useState<string>('');
  const [serverSearch, setServerSearch] = useState('');
  const [interval, setIntervalValue] = useState(10);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [historyError, setHistoryError] = useState('');
  const [viewerError, setViewerError] = useState('');
  const [modal, setModal] = useState(false);
  const [tagServer, setTagServer] = useState<Server | null>(null);
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [heatmap, setHeatmap] = useState<HeatmapPoint[]>([]);
  const [range, setRange] = useState('7d');
  const [metric, setMetric] = useState<MetricKey>('npu_util_percent');
  const [result, setResult] = useState('');
  const clientId = useMemo(() => `viewer_${crypto.randomUUID().replaceAll('-', '')}`, []);

  const load = useCallback(async () => { try { const response = await fetch(`${API}/api/overview`, { cache: 'no-store' }); if (!response.ok) throw new Error(`API ${response.status}`); setOverview(await response.json()); setError(''); } catch (reason) { setError(reason instanceof Error ? reason.message : '无法连接本地采集器'); } }, []);
  const heartbeat = useCallback(async (visible = true) => { try { await requestJson(`/api/viewers/${clientId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ interval, visible }) }); setViewerError(''); } catch (reason) { setViewerError(reason instanceof Error ? reason.message : '刷新频率未确认生效'); } }, [clientId, interval]);

  useEffect(() => { const hash = window.location.hash.slice(1); if (!['overview', 'server', 'history', 'servers'].includes(hash)) return; const timer = window.setTimeout(() => setView(hash as View), 0); return () => clearTimeout(timer); }, []);
  useEffect(() => { const initial = window.setTimeout(() => { void load(); void heartbeat(!document.hidden); }, 0); const refresh = window.setInterval(load, interval * 1000); const lease = window.setInterval(() => heartbeat(!document.hidden), 10000); const visibility = () => heartbeat(!document.hidden); document.addEventListener('visibilitychange', visibility); return () => { clearTimeout(initial); clearInterval(refresh); clearInterval(lease); document.removeEventListener('visibilitychange', visibility); }; }, [load, heartbeat, interval]);
  const orderedServers = useMemo(() => [...overview.servers].sort((left, right) => Number(left.tags.includes(LOW_PRIORITY_TAG)) - Number(right.tags.includes(LOW_PRIORITY_TAG)) || left.name.localeCompare(right.name, 'zh-CN')), [overview.servers]);
  const effectiveServerId = overview.servers.some((server) => server.id === selectedServerId) ? selectedServerId : (orderedServers[0]?.id || '');
  const selectedServer = orderedServers.find((server) => server.id === effectiveServerId) || orderedServers[0];
  useEffect(() => { if (view !== 'history' || !effectiveServerId) return; const controller = new AbortController(); const params = `range=${range}&server_id=${encodeURIComponent(effectiveServerId)}`; const timezone = -new Date().getTimezoneOffset() * 60; Promise.all([requestJson<{ points: HistoryPoint[] }>(`/api/history?${params}`, { cache: 'no-store', signal: controller.signal }), requestJson<{ points: HeatmapPoint[] }>(`/api/history/heatmap?${params}&timezone_offset=${timezone}`, { cache: 'no-store', signal: controller.signal })]).then(([trend, heat]) => { if (!Array.isArray(trend.points) || !Array.isArray(heat.points)) throw new Error('历史响应缺少采样数据'); setHistory(trend.points); setHeatmap(heat.points); setHistoryError(''); }).catch((reason) => { if (!controller.signal.aborted) { setHistory([]); setHeatmap([]); setHistoryError(reason instanceof Error ? reason.message : '历史查询失败'); } }); return () => controller.abort(); }, [view, range, effectiveServerId, overview.generated_at]);
  const filteredServers = orderedServers.filter((server) => `${server.name} ${server.host} ${server.tags.join(' ')}`.toLowerCase().includes(serverSearch.toLowerCase()));
  const total = overview.totals;
  const hbmPercent = percent(total.hbm_used_mb, total.hbm_total_mb);
  const dayCount = range === '90d' ? 90 : range === '30d' ? 30 : 7;
  const heatmapDays = useMemo(() => buildDays(dayCount), [dayCount]);
  const deviceIds = useMemo(() => Array.from(new Set(heatmap.flatMap((point) => point.devices.map((device) => device.npu_id)))).sort((a, b) => a - b), [heatmap]);
  const alerts = overview.servers.flatMap((server) => (server.snapshot?.disks || []).filter((disk) => disk.used_percent >= 85).map((disk) => ({ server, disk }))).sort((a, b) => b.disk.used_percent - a.disk.used_percent);

  async function addServers(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const lines = String(form.get('servers') || '').split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
    const servers = lines.map((line) => {
      const [name, host, port = '22', username = 'root', tags = ''] = line.split(',').map((value) => value.trim());
      return { name, host: host || name, port: Number(port), username, tags: tags ? tags.split('|') : [] };
    });
    const passwords = String(form.get('passwords') || '').split(/\r?\n/).filter(Boolean);
    setResult('正在逐台检查连接并配置监控密钥…');
    try {
      type Receipt = { input_index: number; registration: string; auth: { ok: boolean; state?: string; installation?: string; error?: string }; failure?: { stage: string; error: string } };
      const data = await requestJson<{ results: Receipt[] }>('/api/servers/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ servers, passwords }),
      });
      if (!Array.isArray(data.results) || data.results.length !== servers.length) throw new Error('服务器添加回执不完整，请先核对清单');
      const ok = data.results.filter((item) => item.auth.ok).length;
      const issues = data.results.filter((item) => !item.auth.ok || item.failure).map((item) => {
        const outcome = item.auth.installation === 'completed' ? '密钥已安装；' : item.auth.state === 'unknown' ? '远端写入结果不确定，请先核对；' : '';
        return `第 ${item.input_index + 1} 台：${outcome}${item.failure?.error || item.auth.error || '未完成'}`;
      });
      setResult(`${ok}/${servers.length} 台已验证密钥连接。${issues.join('；')} 一次性密码未保存。`);
      await load();
    } catch (reason) {
      setResult(reason instanceof Error ? reason.message : '批量添加结果未确认，请先核对清单');
    }
  }
  async function action(path: string, method = 'POST', body?: object) {
    try {
      await requestJson(path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
      setActionError('');
      await load();
      return true;
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '操作未确认完成，请先核对当前状态');
      return false;
    }
  }
  async function saveTags(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!tagServer) return; const form = new FormData(event.currentTarget); const tags = String(form.get('tags') || '').split(/[|,，]/).map((tag) => tag.trim()).filter(Boolean); if (tagServer.tags.includes(LOW_PRIORITY_TAG) && !tags.includes(LOW_PRIORITY_TAG)) tags.push(LOW_PRIORITY_TAG); if (await action(`/api/servers/${tagServer.id}`, 'PUT', { tags })) setTagServer(null); }
  function openServer(server: Server) { setSelectedServerId(server.id); setView('server'); }

  const titles: Record<View, [string, string]> = { overview: ['集群实时总览', '跨服务器资源、空闲设备与基础设施状态'], server: [selectedServer?.name || '服务器详情', selectedServer ? `${selectedServer.username}@${selectedServer.host}:${selectedServer.port}` : '选择服务器查看详情'], history: ['资源历史', selectedServer ? `${selectedServer.name} · 趋势与 2 小时热力图` : '选择服务器查看历史'], servers: ['服务器管理', '本地清单、连接状态与采集控制'] };
  const runtimeError = [
    overview.runtime.storage_failures || overview.runtime.maintenance_error ? '历史记录未完成；当前观测仍保留，不能视作历史已保存' : '',
    overview.runtime.inventory?.state === 'failed' ? `${overview.runtime.inventory.error} (${overview.runtime.inventory.error_type})` : '',
    overview.runtime.collector_status && !['running', 'not_started'].includes(overview.runtime.collector_status) ? `采集器 ${overview.runtime.collector_status}: ${overview.runtime.collector_error_type || '请核对运行状态'}` : '',
  ].filter(Boolean).join('；');

  return <main className="app-shell">
    <aside className="sidebar">
      <button className="brand" onClick={() => setView('overview')}><span className="brand__mark">N</span><span><strong>NPU Fleet</strong><small>Ascend observability</small></span></button>
      <nav className="primary-nav" aria-label="主导航">
        <button className={view === 'overview' ? 'is-active' : ''} onClick={() => setView('overview')}><i>⌂</i><span>集群总览</span><b>{number(total.online_servers)}/{number(total.servers)}</b></button>
        <button className={view === 'history' ? 'is-active' : ''} onClick={() => setView('history')}><i>▦</i><span>资源历史</span></button>
        <button className={view === 'servers' ? 'is-active' : ''} onClick={() => setView('servers')}><i>⚙</i><span>服务器管理</span></button>
      </nav>
      <div className="sidebar__section"><span>服务器</span><b>{overview.servers.filter((server) => server.status === 'online').length} 在线</b></div>
      <label className="server-search"><span>⌕</span><input value={serverSearch} onChange={(event) => setServerSearch(event.target.value)} placeholder="搜索服务器或标签" aria-label="搜索服务器" />{serverSearch && <button onClick={() => setServerSearch('')} aria-label="清空搜索">×</button>}</label>
      <div className="server-list">{filteredServers.map((server) => { const summary = server.snapshot?.summary; return <button key={server.id} className={`server-row ${view === 'server' && selectedServer?.id === server.id ? 'is-selected' : ''}`} onClick={() => openServer(server)}><StatusDot status={server.status} /><span><strong>{server.name}{server.tags.includes(LOW_PRIORITY_TAG) && <em>低优先级</em>}</strong><small>{server.snapshot?.devices?.length || 0} NPU · HBM {number(percent(summary?.hbm_used_mb, summary?.hbm_total_mb), '%')} · CPU {number(summary?.cpu_percent, '%')}</small></span><i>›</i></button>; })}{!filteredServers.length && <p className="server-list__empty">没有匹配的服务器</p>}</div>
      <div className="collector-card"><div><span className={`pulse ${overview.runtime.collecting ? 'is-working' : ''}`} /><strong>{runtimeError ? '采集异常' : overview.runtime.mode === 'interactive' ? '实时采集中' : '低频巡检中'}</strong></div><p>{overview.runtime.effective_interval} 秒一次 · {overview.runtime.active_viewers} 个活跃页面</p><MiniBar value={overview.runtime.mode === 'interactive' ? 100 : 35} tone="green" /></div>
    </aside>

    <section className="workspace"><header className="topbar"><div><p>{titles[view][1]}</p><h1>{titles[view][0]}</h1></div><div className="top-actions"><label>刷新频率<select value={interval} onChange={(event) => setIntervalValue(Number(event.target.value))}>{overview.runtime.allowed_intervals.map((value) => <option value={value} key={value}>{value} 秒</option>)}</select></label><span className={`live-state ${error || runtimeError ? 'is-offline' : ''}`}><i />{error || runtimeError ? '采集不可用' : `已同步 · ${timeAgo(overview.generated_at)}`}</span><button className="primary-button" onClick={() => setModal(true)}>＋ 添加服务器</button></div></header>
      <div className="workspace-scroll">
        {(error || runtimeError || actionError || viewerError || (view === 'history' && historyError)) && <p className="panel" role="alert">{[error, runtimeError, actionError, viewerError, view === 'history' ? historyError : ''].filter(Boolean).join(' · ')}</p>}
        {view === 'overview' && <div className="page-stack"><section className="metric-grid" aria-label="集群核心指标"><MetricCard label="在线服务器" value={number(total.online_servers)} suffix={` / ${number(total.servers)}`} detail={`${number(total.npu_count)} 张 NPU 已发现`} tone="blue" icon="S" /><MetricCard label="平均 NPU 利用率" value={number(total.npu_util_percent, '%')} detail={overview.runtime.collecting ? '正在刷新当前采样' : `上次采样 ${timeAgo(overview.runtime.last_cycle_at)}`} tone="purple" icon="U" /><MetricCard label="集群 HBM" value={number(hbmPercent, '%')} detail={`${mb(total.hbm_used_mb)} / ${mb(total.hbm_total_mb)}`} tone="green" icon="M" /><MetricCard label="可用设备" value={number(total.idle_npu_count)} suffix=" 张" detail={`${number(total.busy_npu_count)} 张处于繁忙状态`} tone="orange" icon="A" /></section>
          <section className="fleet-layout"><article className="panel fleet-board"><header className="panel-heading"><div><p className="section-kicker">Fleet</p><h2>服务器资源矩阵</h2><span>选择服务器查看每张 NPU、磁盘与容器明细</span></div><div className="legend"><span><i className="legend-hbm" />HBM 占用</span><span><i className="legend-active" />AICore 活跃</span></div></header>{overview.servers.length ? <div className="server-grid">{orderedServers.map((server) => <ServerCard key={server.id} server={server} onOpen={() => openServer(server)} onCollect={() => action(`/api/servers/${server.id}/collect`)} />)}</div> : <div className="empty-state"><strong>连接第一台 NPU 服务器</strong><p>批量粘贴主机信息，系统会建立专用密钥并开始低频采集。</p><button className="primary-button" onClick={() => setModal(true)}>添加服务器</button></div>}</article>
            <aside className="overview-rail"><article className="panel cadence-panel"><header><span className="icon-tile">↻</span><div><h3>自适应采集</h3><p>{overview.runtime.mode === 'interactive' ? '页面正在请求实时数据' : '当前没有活跃前端页面'}</p></div></header><div className="cadence-value"><strong>{overview.runtime.effective_interval}</strong><span>秒</span></div><dl><div><dt>实时资源</dt><dd>{overview.runtime.effective_interval}s</dd></div><div><dt>基础设施</dt><dd>{overview.runtime.mode === 'interactive' ? '60s' : `${overview.runtime.idle_interval}s`}</dd></div><div><dt>历史写入</dt><dd>≥ {overview.runtime.history_interval}s</dd></div></dl></article><article className="panel alert-panel"><header><span className="icon-tile icon-tile--orange">!</span><div><h3>基础设施提醒</h3><p>磁盘水位达到 85% 时提示</p></div></header><div className="alert-list">{alerts.slice(0, 5).map(({ server, disk }) => <button key={`${server.id}-${disk.mount}`} onClick={() => openServer(server)}><span><strong>{server.name}</strong><small>{disk.mount} · {bytes(disk.available_bytes)} 可用</small></span><b>{disk.used_percent}%</b></button>)}{!alerts.length && <div className="all-clear"><i>✓</i><span><strong>状态良好</strong><small>暂无高水位磁盘</small></span></div>}</div></article></aside>
          </section></div>}

        {view === 'server' && selectedServer && <ServerDetail server={selectedServer} onCollect={() => action(`/api/servers/${selectedServer.id}/collect`)} onHistory={() => setView('history')} />}

        {view === 'history' && <div className="page-stack history-page"><section className="history-toolbar panel"><div><label>服务器<select value={effectiveServerId} onChange={(event) => setSelectedServerId(event.target.value)}>{orderedServers.map((server) => <option value={server.id} key={server.id}>{server.name}</option>)}</select></label><label>趋势指标<select value={metric} onChange={(event) => setMetric(event.target.value as MetricKey)}>{Object.entries(metricLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label></div><div className="range-tabs" role="group" aria-label="历史范围">{['7d', '30d', '90d'].map((value) => <button key={value} className={range === value ? 'is-active' : ''} onClick={() => setRange(value)}>{value}</button>)}</div></section><section className="history-summary metric-grid metric-grid--history"><MetricCard label={`${metricLabels[metric]}平均`} value={precise(average(history, metric), '%')} detail={`${history.length} 个趋势桶`} tone="blue" icon="Ø" /><MetricCard label="设备繁忙均值" value={precise(average(history, 'npu_util_percent'), '%')} detail={`${deviceIds.length} 张 NPU 有历史`} tone="purple" icon="N" /><MetricCard label="HBM 平均" value={precise(average(history, 'hbm_percent'), '%')} detail={`最长保留 ${range}`} tone="green" icon="H" /><MetricCard label="磁盘最高水位" value={number(history.length ? Math.max(...history.map((point) => point.disk_max_percent || 0)) : null, '%')} detail="窗口内峰值" tone="orange" icon="D" /></section><article className="panel trend-panel"><header className="panel-heading"><div><p className="section-kicker">Timeline</p><h2>{metricLabels[metric]}趋势</h2><span>{selectedServer?.name || '未选择服务器'} · SQLite 历史数据自动分桶</span></div></header><TrendCanvas points={history} metric={metric} /><footer className="trend-axis"><span>{history.length ? new Date(history[0].bucket * 1000).toLocaleString('zh-CN', { hour12: false }) : '暂无数据'}</span><span>{history.length ? new Date(history.at(-1)!.bucket * 1000).toLocaleString('zh-CN', { hour12: false }) : ''}</span></footer></article><section className="heatmap-section"><header className="panel-heading"><div><p className="section-kicker">Activity heatmap</p><h2>资源活动热力图</h2><span>每列代表一天、每格代表 2 小时；CPU、内存与 NPU 状态在同页对照</span></div></header><div className="heatmap-list"><HistoryHeatmap title="CPU" subtitle="整机处理器利用率" points={heatmap} days={heatmapDays} tone="blue" valueFor={(point) => point.cpu_percent ?? null} /><HistoryHeatmap title="系统内存" subtitle="整机内存占用" points={heatmap} days={heatmapDays} tone="orange" valueFor={(point) => point.memory_percent ?? null} /><HistoryHeatmap title="NPU 汇总" subtitle="设备平均 AICore 利用率" points={heatmap} days={heatmapDays} tone="purple" valueFor={(point) => point.npu_util_percent ?? null} /><HistoryHeatmap title="HBM 汇总" subtitle="设备显存占用" points={heatmap} days={heatmapDays} tone="green" valueFor={(point) => point.hbm_percent ?? null} />{deviceIds.map((deviceId) => <HistoryHeatmap key={deviceId} title={`NPU ${deviceId}`} subtitle="AICore 利用率" points={heatmap} days={heatmapDays} tone="purple" valueFor={(point) => point.devices.find((device) => device.npu_id === deviceId)?.utilization_percent ?? null} />)}</div></section></div>}

        {view === 'servers' && <section className="panel server-table"><header className="panel-heading"><div><p className="section-kicker">Inventory</p><h2>已纳管服务器</h2><span>标签可用于搜索和分组；主工作区禁用设备自动标记为低优先级</span></div><button className="secondary-button" onClick={() => action('/api/collect')}>全部采集</button></header>{overview.servers.length ? <div className="table-wrap"><table><thead><tr><th>服务器</th><th>连接地址</th><th>设备</th><th>标签</th><th>最近采样</th><th>状态</th><th>操作</th></tr></thead><tbody>{orderedServers.map((server) => <tr key={server.id}><td><button className="table-server" onClick={() => openServer(server)}><StatusDot status={server.status} /><strong>{server.name}</strong></button></td><td><code>{server.username}@{server.host}:{server.port}</code></td><td>{server.snapshot?.devices?.length || 0} NPU</td><td><div className="table-tags"><ServerTags tags={server.tags} /><button onClick={() => setTagServer(server)}>编辑标签</button></div></td><td>{timeAgo(server.last_seen_at)}</td><td><span className={`status-label status-label--${server.status}`}>{server.enabled ? (server.status === 'online' ? '在线' : '待检查') : '已暂停'}</span></td><td className="row-actions"><button onClick={() => action(`/api/servers/${server.id}`, 'PUT', { enabled: !server.enabled })}>{server.enabled ? '暂停' : '启用'}</button><button className="is-danger" onClick={() => confirm(`移除 ${server.name}？历史数据也会删除。`) && action(`/api/servers/${server.id}`, 'DELETE')}>移除</button></td></tr>)}</tbody></table></div> : <div className="empty-state"><strong>服务器列表为空</strong><button className="primary-button" onClick={() => setModal(true)}>批量添加</button></div>}</section>}
      </div>
    </section>

    {modal && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setModal(false)}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="add-title"><header><div><p className="section-kicker">Batch onboarding</p><h2 id="add-title">添加 NPU 服务器</h2></div><button aria-label="关闭" onClick={() => setModal(false)}>×</button></header><form onSubmit={addServers}><label>服务器列表<span>每行：名称, 主机, 端口, 用户, 标签1|标签2</span><textarea name="servers" required rows={7} placeholder={'example-a3-01, 192.0.2.21, 22, root, A3|训练\nexample-a2-07, 192.0.2.37, 22, root, A2'} /></label><label>一次性密码候选<span>可选，每行一个；仅在本次请求内按顺序尝试</span><textarea name="passwords" rows={4} autoComplete="new-password" spellCheck={false} /></label><div className="security-note"><b>安全连接策略</b><p>先检查已有密钥；失败后才尝试候选密码。成功后安装项目专用 Ed25519 公钥，后续只使用密钥。Host Key 变化会立即中止连接。</p></div>{result && <p className="form-result">{result}</p>}<footer><button type="button" className="secondary-button" onClick={() => setModal(false)}>关闭</button><button type="submit" className="primary-button">开始检查并添加</button></footer></form></section></div>}
    {tagServer && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setTagServer(null)}><section className="modal modal--tags" role="dialog" aria-modal="true" aria-labelledby="tag-title"><header><div><p className="section-kicker">Server tags</p><h2 id="tag-title">编辑 {tagServer.name} 的标签</h2></div><button aria-label="关闭" onClick={() => setTagServer(null)}>×</button></header><form onSubmit={saveTags}><label>服务器标签<span>使用竖线、逗号或中文逗号分隔，最多 20 个</span><input name="tags" autoFocus defaultValue={tagServer.tags.filter((tag) => tag !== LOW_PRIORITY_TAG).join(' | ')} placeholder="A3 | 训练 | 北京机房" /></label>{tagServer.tags.includes(LOW_PRIORITY_TAG) && <div className="security-note security-note--priority"><b>主工作区低优先级设备</b><p>“低优先级”由主工作区设备状态自动同步，保存时会继续保留。</p></div>}<footer><button type="button" className="secondary-button" onClick={() => setTagServer(null)}>取消</button><button type="submit" className="primary-button">保存标签</button></footer></form></section></div>}
  </main>;
}

function NpuProcessPanel({ devices }: { devices: Device[] }) {
  const processes = Array.from(devices.reduce((byPid, device) => {
    for (const process of device.processes || []) {
      const current = byPid.get(process.pid) || { ...process, npuIds: [] as number[] };
      if (!current.npuIds.includes(device.npu_id)) current.npuIds.push(device.npu_id);
      byPid.set(process.pid, current);
    }
    return byPid;
  }, new Map<number, NpuProcess & { npuIds: number[] }>()).values());
  if (!processes.length) return null;

  const groups = Array.from(processes.reduce((byContainer, process) => {
    const container = process.container;
    const key = container?.id || container?.name || 'host';
    const current = byContainer.get(key) || { container, processes: [] as typeof processes };
    current.processes.push(process);
    byContainer.set(key, current);
    return byContainer;
  }, new Map<string, { container?: NpuProcess['container']; processes: typeof processes }>()).values());

  return <section className="panel process-panel"><header className="panel-heading"><div><p className="section-kicker">NPU workloads</p><h2>NPU 进程归属</h2><span>默认显示所属容器；展开查看 PID、PWD 与完整启动命令</span></div><span className="status-label status-label--busy">{processes.length} 个进程</span></header><div className="process-groups">{groups.map(({ container, processes: containerProcesses }) => {
    const label = container?.name || container?.short_id || '宿主机进程';
    const npuIds = Array.from(new Set(containerProcesses.flatMap((process) => process.npuIds))).sort((a, b) => a - b);
    const ownershipLabels = Array.from(new Map(containerProcesses.flatMap((process) => process.ownership_labels || []).map((item) => [`${item.kind}:${item.value.toLowerCase()}`, item])).values());
    const copyableLabels = ownershipLabels.map((item) => item.value).join(' ');
    return <details className="process-group" key={container?.id || label}><summary><span className={`process-container-icon ${container ? 'is-container' : ''}`}>▣</span><span><strong className="process-container-name">{label}</strong><small>{containerProcesses.length} 个进程 · NPU {npuIds.join(', ')}</small></span><span className="process-summary-actions"><CopyButton value={label} label="复制容器" /><i>›</i></span></summary>{ownershipLabels.length > 0 && <div className="ownership-labels"><span>归属标签</span><div>{ownershipLabels.map((item) => <CopyButton key={`${item.kind}:${item.value}`} value={item.value} label={`${item.kind === 'employee_id' ? '工号' : '缩写?'} · ${item.value}`} className={`ownership-chip ownership-chip--${item.kind}`} />)}<CopyButton value={copyableLabels} label="复制全部" className="copy-all-labels" /></div></div>}<div className="process-details">{containerProcesses.sort((a, b) => a.pid - b.pid).map((process) => <article key={process.pid}><header><strong>{process.name || process.npu_process_name || '未知进程'}</strong><span>PID {process.pid} · {process.user || '未知用户'} · NPU {process.npuIds.join(', ')}</span></header><dl><div><dt>所属容器</dt><dd className="copy-value"><code>{label}</code><CopyButton value={label} /></dd></div><div><dt>PWD</dt><dd className="copy-value"><code>{process.cwd || '无法读取'}</code>{process.cwd && <CopyButton value={process.cwd} />}</dd></div><div><dt>启动命令</dt><dd><code>{process.command || process.npu_process_name || '无法读取'}</code></dd></div>{process.executable && <div><dt>可执行文件</dt><dd><code>{process.executable}</code></dd></div>}{container?.image && <div><dt>容器镜像</dt><dd><code>{container.image}</code></dd></div>}</dl></article>)}</div></details>;
  })}</div></section>;
}

function ServerDetail({ server, onCollect, onHistory }: { server: Server; onCollect: () => void; onHistory: () => void }) {
  const snapshot = server.snapshot;
  const summary = snapshot?.summary;
  const devices = snapshot?.devices || [];
  const dies = physicalDies(devices);
  const disks = snapshot?.disks || [];
  const containers = snapshot?.docker?.containers || [];
  const memoryPercent = percent(summary?.memory_used_bytes, summary?.memory_total_bytes);
  const hbmPercent = percent(summary?.hbm_used_mb, summary?.hbm_total_mb);
  return <div className="page-stack server-detail"><section className="server-hero panel"><div className="server-hero__identity"><span className="server-glyph">S</span><div><span className={`status-label status-label--${server.status}`}><StatusDot status={server.status} />{server.status === 'online' ? '在线' : '离线'}</span><h2>{server.name}</h2><p>{snapshot?.hostname || server.host} · {server.username}@{server.host}:{server.port}</p><div>{server.tags.map((tag) => <span className={tag === LOW_PRIORITY_TAG ? 'is-low-priority' : ''} key={tag}>{tag}</span>)}</div></div></div><div className="server-hero__actions"><button className="secondary-button" onClick={onHistory}>查看历史</button><button className="primary-button" onClick={onCollect}>立即采集</button></div></section><section className="resource-metrics"><article><header><span>CPU 利用率</span><strong>{number(summary?.cpu_percent, '%')}</strong></header><MiniBar value={summary?.cpu_percent} /><p>Load 1m {precise(summary?.load1)}</p></article><article><header><span>系统内存</span><strong>{number(memoryPercent, '%')}</strong></header><MiniBar value={memoryPercent} tone="purple" /><p>{bytes(summary?.memory_used_bytes)} / {bytes(summary?.memory_total_bytes)}</p></article><article><header><span>HBM 占用</span><strong>{number(hbmPercent, '%')}</strong></header><MiniBar value={hbmPercent} tone="green" /><p>{mb(summary?.hbm_used_mb)} / {mb(summary?.hbm_total_mb)}</p></article><article><header><span>磁盘峰值</span><strong>{number(summary?.disk_max_percent, '%')}</strong></header><MiniBar value={summary?.disk_max_percent} tone="orange" /><p>{disks.length} 个文件系统</p></article></section><section className="panel device-panel"><header className="panel-heading"><div><p className="section-kicker">Accelerators</p><h2>NPU 设备</h2><span>{devices.length} 张 NPU · {dies.length} 个 die · {dies.filter(({ device, chip }) => clampPercent((chip || device).aicore_percent) > 0).length} 个活跃 · {timeAgo(snapshot?.collected_at)}</span></div><span className="status-label">采集耗时 {snapshot?.duration_ms || 0} ms</span></header>{devices.length ? <div className="device-grid">{devices.map((device) => { const deviceHbm = percent(device.hbm?.used_mb, device.hbm?.total_mb); return <article className={`device-card ${clampPercent(device.aicore_percent) > 0 ? 'is-busy' : ''}`} key={device.npu_id}><header><span className="device-die-pair">{device.chips?.length ? device.chips.map((chip) => <NpuDie key={chip.phy_id ?? chip.chip_id} device={device} chip={chip} />) : <NpuDie device={device} detail />}</span><div><h3>NPU {device.npu_id}</h3><p>{device.name || 'Ascend NPU'} · {device.health || '状态未知'}</p></div><span className={`status-label ${clampPercent(device.aicore_percent) > 0 ? 'status-label--busy' : 'status-label--idle'}`}>{clampPercent(device.aicore_percent) > 0 ? 'AICore 活跃' : '可用'}</span></header><div className="device-stat"><span>AICore</span><strong>{number(device.aicore_percent, '%')}</strong><MiniBar value={device.aicore_percent} tone="purple" /></div><div className="device-stat"><span>HBM</span><strong>{number(deviceHbm, '%')}</strong><MiniBar value={deviceHbm} tone="green" /></div><footer><span>{number(device.temperature_c, '°C')}</span><span>{number(device.power_w, 'W')}</span><span>{device.processes?.length || 0} 进程</span></footer></article>; })}</div> : <div className="empty-state"><strong>暂无 NPU 数据</strong><p>{snapshot?.error || server.last_error || '等待下一次采集'}</p></div>}</section><>{snapshot?.recording?.state === 'failed' && <p className="panel" role="alert">当前观测已获得，历史保存失败：{snapshot.recording.error_type} · {snapshot.recording.diagnostic_ref}</p>}{snapshot?.process_details?.state === 'unavailable' && <p className="panel" role="alert">进程详情不可用：{snapshot.process_details.error || snapshot.process_details.error_type || `exit ${snapshot.process_details.exit_code}`}</p>}<NpuProcessPanel devices={devices} /></><section className="detail-columns"><article className="panel storage-panel"><header className="panel-heading"><div><p className="section-kicker">Storage</p><h2>磁盘与挂载</h2></div></header><div className="storage-list">{disks.map((disk) => <div key={`${disk.filesystem}-${disk.mount}`}><header><span><strong>{disk.mount}</strong><small>{disk.filesystem}</small></span><b>{disk.used_percent}%</b></header><MiniBar value={disk.used_percent} tone={disk.used_percent >= 85 ? 'orange' : 'blue'} /><p>{bytes(disk.available_bytes)} 可用 · 共 {bytes(disk.total_bytes)}</p></div>)}{!disks.length && <p className="muted-copy">暂无磁盘采样</p>}</div></article><article className="panel docker-panel"><header className="panel-heading"><div><p className="section-kicker">Containers</p><h2>Docker</h2><span>{snapshot?.docker?.running || 0} 个运行中</span></div></header><div className="container-list">{containers.slice(0, 8).map((container) => <div key={container.id || container.name}><i className={container.state === 'running' ? 'is-running' : ''} /><span><strong>{container.name || 'unnamed'}</strong><small>{container.image || container.status || '—'}</small></span><b>{container.stats?.cpu_percent || container.state || '—'}</b></div>)}{!containers.length && <p className="muted-copy">Docker 不可用或暂无运行容器</p>}</div></article></section></div>;
}
