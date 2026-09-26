"use client";

import { type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";

type Role = "admin" | "quote";
type ActiveView = "tasks" | "new" | "customers" | "history" | "assets" | "accounts";
type RowFilter = "all" | "pending" | "suggested" | "review" | "unmatched" | "unit_conflict" | "parameter_conflict" | "price_anomaly" | "score_tie" | "low_confidence" | "confirmed";
type LineSort = "score_asc" | "score_desc" | "source_row";

type User = { id: number; username: string; display_name: string; role: Role };
type AccountUser = { id: number; username: string; display_name: string; role: Role; active: boolean; created_at: string };
type Requirement = {
  id: number;
  attribute_name: string;
  operator: string;
  value: string;
  unit: string;
  required: boolean;
  notes: string;
};
type Customer = {
  id: number;
  name: string;
  customer_type: "ordinary" | "special" | "vip";
  discount_percent: number;
  minimum_margin_percent: number;
  preferred_manufacturers: string[];
  notes: string;
  created_at: string;
  requirements: Requirement[];
};
type Job = {
  id: string;
  file_name: string;
  display_name: string;
  status: string;
  progress: number;
  requested_option_count: number;
  tax_rate: number;
  database_key: string;
  database_name: string;
  total_lines: number;
  matched_lines: number;
  review_lines: number;
  unmatched_lines: number;
  confirmed_lines: number;
  error_message: string;
  created_at: string;
  updated_at: string;
  customer: Customer | null;
  created_by: User;
};
type HistoryRecord = {
  id: string;
  name: string;
  spec: string;
  model: string;
  brand: string;
  manufacturer: string;
  unit: string;
  price: number;
  quote_date: string;
  source_file?: string;
  source_sheet?: string;
  source_row?: number;
};
type QuoteOption = {
  id: number;
  rank: number;
  score: number;
  confidence: string;
  component_scores: Record<string, number>;
  reasons: string[];
  warnings: string[];
  unit_status: string;
  normalized_price: number | null;
  selected: boolean;
  final_price: number;
  manual_note: string;
  record: HistoryRecord;
};
type QuoteLine = {
  id: number;
  source_row: number;
  sheet_name: string;
  name: string;
  spec: string;
  product_code: string;
  model: string;
  brand: string;
  manufacturer: string;
  unit: string;
  quantity: number | null;
  pricing_quantity: number | null;
  status: string;
  confidence: string;
  recommended_score: number;
  warnings: string[];
  confirmed: boolean;
  selected_option_count: number;
  primary_option: QuoteOption | null;
  options?: QuoteOption[];
};
type PageResult = { items: QuoteLine[]; total: number; page: number; page_size: number; page_count: number };
type Governance = {
  record_count: number;
  unique_name_count: number;
  duplicate_name_groups: number;
  unit_conflict_groups: number;
  price_conflict_groups: number;
  missing_manufacturer_count: number;
  audit_event_count: number;
  matching_weights: Record<string, number>;
};
type HistoryResult = HistoryRecord & { source: string };
type DatabaseEntry = {
  key: string;
  name: string;
  note: string;
  tags: string[];
  status: "active" | "trashed";
  exists: boolean;
  size_mb: number;
  history_count: number;
  job_count: number;
  is_system: boolean;
  created_at: string;
  updated_at: string;
  last_used_at: string;
  use_count: number;
};
type DatabaseList = { system: string; databases: DatabaseEntry[]; trashed: DatabaseEntry[] };
type DatabaseSearchResult = { system: string; databases: DatabaseEntry[] };
type AuditEvent = {
  id: number;
  action: string;
  entity_type: string;
  entity_id: string;
  detail: Record<string, unknown>;
  created_at: string;
  user: User;
};

// ---- 价目表格化编辑 --------------------------------------------------------

type EditableField =
  | "name" | "spec" | "model" | "brand" | "manufacturer"
  | "unit" | "product_code" | "price" | "quantity" | "quote_date";

type HistoryRow = {
  id: string;
  name: string;
  spec: string;
  model: string;
  brand: string;
  manufacturer: string;
  unit: string;
  product_code: string;
  price: number;
  quantity: number | null;
  quote_date: string;
  source_file: string;
  source_sheet: string;
  source_row: number;
  source_priority: number;
  data_quality: number;
  revision: number;
};

type HistoryRowsPage = {
  database_key: string;
  total: number;
  page: number;
  page_size: number;
  pages: number;
  editable_fields: string[];
  readonly_fields: string[];
  max_bulk_rows: number;
  rows: HistoryRow[];
};

type EditConflict = {
  id?: string;
  name?: string;
  location?: string;
  actual_revision?: number;
  expected_revision?: number;
};

type BulkEditRowResult = {
  op: "create" | "update" | "delete";
  id: string | null;
  status: "ok" | "conflict" | "rejected";
  reason?: "revision_mismatch" | "duplicate_key" | "invalid" | "missing";
  message?: string;
  changed?: string[];
  revision?: number;
  conflict?: EditConflict;
};

type BulkEditResponse = {
  ok: boolean;
  database_key: string;
  applied: { created: number; updated: number; deleted: number };
  unchanged: number;
  skipped_duplicates: number;
  rejected: number;
  results: BulkEditRowResult[];
  backup: string;
};

// ---- 打开本地价目本：先解析比对，用户确认后再落库 --------------------------

type OpenFileAction = "create" | "update" | "unchanged" | "ambiguous" | "duplicate" | "invalid";

type OpenFileValues = Record<string, string | number | null>;

// 库里被匹配到的那一条（候选或唯一目标）。values 是"文件覆盖到库里现值上"的
// 结果，前端直接拿它当 bulk-edit 的 changes，不再自己合并一遍。
type OpenFileTarget = {
  id: string;
  revision: number;
  price: number | null;
  location: string;
  current?: OpenFileValues;
  values?: OpenFileValues;
  changed?: string[];
};

type OpenFileRow = {
  seq: number;
  source: string;
  action: OpenFileAction;
  reason: string;
  target: OpenFileTarget | null;
  targets: OpenFileTarget[];
  warnings: string[];
  match_fields: string[];
  match_labels: string[];
  current?: OpenFileValues;
  values?: OpenFileValues;
  changed?: string[];
};

type OpenFilePreview = {
  database_key: string;
  source: string;
  total: number;
  summary: Record<OpenFileAction, number>;
  max_bulk_rows: number;
  rows: OpenFileRow[];
};

// 预览分组：顺序即用户该关心的顺序，"将改动"放最前。
const OPEN_FILE_GROUPS: Array<{ key: string; title: string; actions: OpenFileAction[] }> = [
  { key: "change", title: "将改动", actions: ["update", "create", "ambiguous"] },
  { key: "unchanged", title: "无需改动", actions: ["unchanged"] },
  { key: "skipped", title: "不载入", actions: ["duplicate", "invalid"] },
];

const OPEN_FILE_ACTION_LABEL: Record<OpenFileAction, string> = {
  create: "新增",
  update: "修改",
  unchanged: "无需改动",
  ambiguous: "需指定",
  duplicate: "重复",
  invalid: "不载入",
};

// 变更集里只发可编辑字段。预览返回的 values 可能带只读字段（id 之类）吗？
// 不会——它来自 history_rows.editable_payload，但为了不把服务端的字段集合
// 当成前端的契约，这里仍按 EDIT_FIELDS 过滤一遍。
function pickChanges(values: OpenFileValues | undefined): Record<string, string | number | null> {
  const changes: Record<string, string | number | null> = {};
  if (!values) return changes;
  for (const field of EDIT_FIELDS) {
    if (field.key in values) changes[field.key] = values[field.key];
  }
  return changes;
}

// 前后对比只展示真正变化的字段，避免 10 列全铺开。
function changeSummary(current: OpenFileValues | undefined, values: OpenFileValues | undefined): string[] {
  if (!values) return [];
  const lines: string[] = [];
  for (const field of EDIT_FIELDS) {
    if (!(field.key in values)) continue;
    const next = values[field.key] ?? "";
    if (current === undefined) {
      if (String(next).trim() === "") continue;
      lines.push(`${field.label}：${next}`);
      continue;
    }
    const before = current[field.key] ?? "";
    if (String(before) === String(next)) continue;
    lines.push(`${field.label}：${before} → ${next}`);
  }
  return lines;
}

// 预览里"将改动"的那些行里，用户有权逐行排除：一份价目本可能混着暂时不想动的新品，
// 全有或全无的确认不算确认。这里算出"实际会写什么"，计数和提交都走同一份结果，
// 免得按钮上的数字和真正提交的行数对不上。
function planFromPreview(
  preview: OpenFilePreview,
  excluded: ReadonlySet<number>,
  picks: Record<number, string>,
) {
  const created: Array<{ changes: Record<string, string | number | null> }> = [];
  const updated: Array<{ id: string; revision: number; changes: Record<string, string | number | null> }> = [];
  let unpicked = 0;
  for (const row of preview.rows) {
    if (row.action !== "create" && row.action !== "update" && row.action !== "ambiguous") continue;
    if (excluded.has(row.seq)) continue;
    if (row.action === "create") {
      created.push({ changes: pickChanges(row.values) });
    } else if (row.action === "update" && row.target) {
      updated.push({ id: row.target.id, revision: row.target.revision, changes: pickChanges(row.values) });
    } else if (row.action === "ambiguous") {
      const chosen = row.targets.find((item) => item.id === picks[row.seq]);
      if (!chosen) { unpicked += 1; continue; }
      updated.push({ id: chosen.id, revision: chosen.revision, changes: pickChanges(chosen.values) });
    }
  }
  return { created, updated, unpicked, writable: created.length + updated.length };
}

// 网格列定义。派生列（normalized_*、data_quality）与溯源列（来源、版本号）
// 不在其中——服务端不接受它们作为可编辑字段。
const EDIT_FIELDS: Array<{ key: EditableField; label: string; kind: "text" | "number"; width: number }> = [
  { key: "name", label: "产品名称", kind: "text", width: 200 },
  { key: "spec", label: "参数 / 规格", kind: "text", width: 210 },
  { key: "model", label: "型号", kind: "text", width: 110 },
  { key: "brand", label: "品牌", kind: "text", width: 100 },
  { key: "manufacturer", label: "制造商", kind: "text", width: 130 },
  { key: "unit", label: "单位", kind: "text", width: 70 },
  { key: "product_code", label: "产品编码", kind: "text", width: 120 },
  { key: "quantity", label: "数量", kind: "number", width: 80 },
  // 价目本里的「含税单价」在导入时已按固定税率折成税前基准价，导出报价时再乘回税率。
  // 这里必须写清楚，否则用户会按含税价改，报价就会整体偏低一个税点。
  { key: "price", label: "单价（不含税）", kind: "number", width: 120 },
  { key: "quote_date", label: "报价日期", kind: "text", width: 110 },
];

const ROWS_PAGE_SIZES = [50, 100, 200, 500];

function rowValues(row: HistoryRow): Record<string, string> {
  const values: Record<string, string> = {};
  for (const field of EDIT_FIELDS) {
    const value = row[field.key];
    values[field.key] = value === null || value === undefined ? "" : String(value);
  }
  return values;
}

function rowSource(row: HistoryRow): string {
  const parts = [row.source_file || "（无来源）"];
  if (row.source_sheet) parts.push(row.source_sheet);
  if (row.source_row) parts.push(`第${row.source_row}行`);
  return parts.join(" · ");
}

// 变更集里只发这 10 个字段，派生列由服务端重算。
function toChanges(values: Record<string, string>): Record<string, string | number | null> {
  const changes: Record<string, string | number | null> = {};
  for (const field of EDIT_FIELDS) {
    const raw = values[field.key];
    if (raw === undefined) continue;
    if (field.kind === "number") {
      changes[field.key] = raw.trim() === "" ? null : Number(raw);
    } else {
      changes[field.key] = raw;
    }
  }
  return changes;
}

function csvCell(value: unknown): string {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

const STATUS_COPY: Record<string, string> = {
  queued: "排队中",
  reserved: "准备处理",
  matching: "匹配中",
  review: "待复核",
  confirmed: "已确认",
  exported: "已导出",
  failed: "处理失败",
  suggested: "建议匹配",
  unmatched: "无可靠匹配",
  pending: "待确认",
};

const CUSTOMER_COPY = { ordinary: "普通客户", special: "特殊要求", vip: "VIP客户" };
const ATTRIBUTE_SUGGESTIONS = ["产品名称", "型号", "制造商", "品牌", "参数", "单位"];
const CUSTOMER_GROUPS: Array<{ type: Customer["customer_type"]; title: string; detail: string }> = [
  { type: "ordinary", title: "普通客户", detail: "标准价与常规策略" },
  { type: "vip", title: "VIP 客户", detail: "协议折扣与最低毛利线" },
  { type: "special", title: "特殊要求客户", detail: "结构化要求逐条校验" },
];
const ROW_FILTER_TABS: Array<[RowFilter, string]> = [
  ["all", "全部"], ["pending", "待确认"], ["suggested", "高置信"], ["review", "需复核"], ["unmatched", "无匹配"], ["unit_conflict", "单位冲突"], ["parameter_conflict", "参数冲突"], ["price_anomaly", "价格异常"], ["score_tie", "同分多价"], ["low_confidence", "低置信"], ["confirmed", "已确认"],
];
const SORT_CYCLE: Record<LineSort, LineSort> = { score_asc: "score_desc", score_desc: "source_row", source_row: "score_asc" };
const SORT_LABEL: Record<LineSort, string> = { score_asc: "评分 ↑", score_desc: "评分 ↓", source_row: "评分 ⇅" };
const CONFIDENCE_COPY: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
  review: "需复核",
  unreliable: "不可靠",
  manual: "手工",
};
const STATUS_HINT: Record<string, string> = {
  review: "有候选但置信度不足或存在阻断风险",
  unmatched: "无候选或最佳匹配低于55分",
};

function optionKey(option: QuoteOption): string {
  return [
    (option.record.manufacturer || option.record.brand).trim(),
    String(option.final_price),
    option.record.spec.trim(),
    option.record.model.trim(),
  ].join("|");
}

function dedupeOptions(options: QuoteOption[]): QuoteOption[] {
  const seen = new Set<string>();
  return options.filter((item) => {
    const key = optionKey(item);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function isManualOption(option: QuoteOption): boolean {
  return option.confidence === "manual" || option.record.source_file === "手工方案";
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "include", ...init });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // Keep the HTTP fallback message when the response is not JSON.
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

function money(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

function formatDate(value: string): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function blockingWarnings(warnings: string[]): string[] {
  return warnings.filter((item) => item.startsWith("BLOCK:"));
}

export function QuoteApp() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [activeView, setActiveView] = useState<ActiveView>("tasks");
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [governance, setGovernance] = useState<Governance | null>(null);
  const [showPasswordModal, setShowPasswordModal] = useState(false);
  const [notice, setNotice] = useState("");
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const notify = useCallback((message: string) => {
    setNotice(message);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(""), 4200);
  }, []);

  const loadAppData = useCallback(async () => {
    const [customerData, jobData, governanceData] = await Promise.all([
      api<Customer[]>("/api/customers"),
      api<Job[]>("/api/quote-jobs"),
      api<Governance>("/api/governance/summary"),
    ]);
    setCustomers(customerData);
    setJobs(jobData);
    setGovernance(governanceData);
  }, []);

  useEffect(() => {
    api<User>("/api/auth/me")
      .then((value) => {
        setUser(value);
        return loadAppData();
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 401) setUser(null);
        else {
          setUser(null);
          notify(error instanceof Error ? error.message : "系统连接失败");
        }
      });
  }, [loadAppData, notify]);

  const selectedJob = jobs.find((job) => job.id === selectedJobId) ?? null;

  async function handleLogin(username: string, password: string) {
    const loggedIn = await api<User>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    setUser(loggedIn);
    await loadAppData();
  }

  async function handleLogout() {
    await api<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
    setUser(null);
    setJobs([]);
    setSelectedJobId(null);
  }

  async function refreshJobs(focusId?: string) {
    const nextJobs = await api<Job[]>("/api/quote-jobs");
    setJobs(nextJobs);
    if (focusId) setSelectedJobId(focusId);
  }

  if (user === undefined) {
    return <div className="boot-screen"><div className="boot-mark">价</div><strong>正在连接报价工作台…</strong></div>;
  }
  if (!user) return <LoginScreen onLogin={handleLogin} />;

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">价</div>
          <div><p className="eyebrow">QUOTATION CONTROL DESK</p><h1>智能报价工作台</h1></div>
        </div>
        <div className="topbar-actions">
          <span className="system-state"><i />服务器数据已同步</span>
          <div className="user-chip"><span>{user.display_name}</span><b>{user.role === "admin" ? "管理员" : "报价员"}</b></div>
          <button className="ghost-button" onClick={() => setShowPasswordModal(true)}>修改密码</button>
          <button className="ghost-button" onClick={handleLogout}>退出</button>
        </div>
      </header>

      <section className="workspace">
        <aside className="sidebar" aria-label="功能导航">
          <nav>
            <NavButton active={activeView === "tasks"} number="01" title="报价任务" detail="进度、复核、导出" onClick={() => setActiveView("tasks")} />
            <NavButton active={activeView === "new"} number="02" title="新建报价" detail="客户、文件、多方案" onClick={() => setActiveView("new")} />
            <NavButton active={activeView === "customers"} number="03" title="客户档案" detail="普通、特殊、VIP" onClick={() => setActiveView("customers")} />
            <NavButton active={activeView === "history"} number="04" title="历史报价" detail="价格、厂家、来源" onClick={() => setActiveView("history")} />
            <NavButton active={activeView === "assets"} number="05" title="数据资产" detail="价目库、表格编辑、治理" onClick={() => setActiveView("assets")} />
            {user.role === "admin" && <NavButton active={activeView === "accounts"} number="06" title="账号管理" detail="用户、角色、状态" onClick={() => setActiveView("accounts")} />}
          </nav>
          <div className="sidebar-stat">
            <span>历史报价库</span>
            <strong>{governance?.record_count.toLocaleString("zh-CN") ?? "—"}</strong>
            <small>{governance?.unique_name_count.toLocaleString("zh-CN") ?? "—"} 个产品名称</small>
            <div><b>{governance?.unit_conflict_groups ?? "—"}</b><em>单位冲突组</em></div>
          </div>
        </aside>

        <section className="content-area">
          {activeView === "tasks" && !selectedJob && (
            <TaskCenter jobs={jobs} onOpen={(id) => setSelectedJobId(id)} onCreate={() => setActiveView("new")} onChanged={loadAppData} notify={notify} />
          )}
          {activeView === "tasks" && selectedJob && (
            <JobWorkspace
              job={selectedJob}
              onBack={() => setSelectedJobId(null)}
              onRefresh={() => refreshJobs(selectedJob.id)}
              notify={notify}
            />
          )}
          {activeView === "new" && (
            <NewQuote
              customers={customers}
              onCreated={async (job) => {
                await refreshJobs(job.id);
                setActiveView("tasks");
                notify("报价任务已创建，系统正在匹配历史记录。");
              }}
            />
          )}
          {activeView === "customers" && <CustomerDirectory customers={customers} user={user} onUpdated={loadAppData} notify={notify} />}
          {activeView === "history" && <HistorySearch user={user} notify={notify} />}
          {activeView === "assets" && <DataAssetsView data={governance} user={user} onChanged={loadAppData} notify={notify} />}
          {activeView === "accounts" && user.role === "admin" && <AccountAdmin notify={notify} />}
        </section>
      </section>

      {showPasswordModal && <ChangePasswordModal onClose={() => setShowPasswordModal(false)} notify={notify} />}
      {notice && <div className="global-toast" role="status">{notice}</div>}
    </main>
  );
}

function LoginScreen({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin123");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onLogin(username, password);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <section className="login-story">
        <div className="brand-mark large">价</div>
        <p className="eyebrow">PRIVATE QUOTATION SYSTEM</p>
        <h1>让每一次报价<br />都有依据，也有边界。</h1>
        <p>参数优先、单位校验、多制造商方案与完整审计，服务内部报价人员的正式工作流。</p>
        <div className="login-features"><span>参数权重 45%</span><span>单位冲突阻断</span><span>任务持久化</span></div>
      </section>
      <form className="login-card" onSubmit={submit}>
        <div><p className="eyebrow">STAFF ACCESS</p><h2>登录工作台</h2><p>仅限公司内部授权人员使用</p></div>
        <label><span>用户名</span><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></label>
        <label><span>密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
        {error && <div className="form-error">{error}</div>}
        <button className="primary-button large-button" disabled={busy}>{busy ? "正在登录…" : "进入报价工作台"}</button>
        <small>演示账号 admin / admin123；正式部署必须在环境配置中修改默认密码。</small>
      </form>
    </main>
  );
}

function NavButton({ active, number, title, detail, onClick }: { active: boolean; number: string; title: string; detail: string; onClick: () => void }) {
  return <button className={`nav-item ${active ? "active" : ""}`} onClick={onClick}><span>{number}</span><div><strong>{title}</strong><small>{detail}</small></div></button>;
}

function PageHeading({ eyebrow, title, detail, action }: { eyebrow: string; title: string; detail: string; action?: ReactNode }) {
  return <div className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2><p>{detail}</p></div>{action}</div>;
}

function TaskCenter({ jobs, onOpen, onCreate, onChanged, notify }: { jobs: Job[]; onOpen: (id: string) => void; onCreate: () => void; onChanged: () => Promise<void>; notify: (message: string) => void }) {
  const activeJobs = jobs.filter((job) => ["queued", "reserved", "matching", "review"].includes(job.status)).length;
  const needReview = jobs.reduce((sum, job) => sum + job.review_lines + job.unmatched_lines, 0);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  async function submitRename(job: Job) {
    const value = renameDraft.trim();
    try {
      if (value && value !== (job.display_name || job.file_name)) {
        await api<Job>(`/api/quote-jobs/${job.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ display_name: value }),
        });
        notify("任务已重命名。");
      }
      setRenamingId(null);
      await onChanged();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "重命名失败");
    }
  }

  async function removeJob(job: Job) {
    if (!window.confirm(`确定删除任务「${job.display_name || job.file_name}」吗？该任务的全部报价明细将一并删除，且不可恢复。`)) return;
    try {
      await api<{ ok: boolean }>(`/api/quote-jobs/${job.id}`, { method: "DELETE" });
      notify("任务及其明细已删除。");
      await onChanged();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  return (
    <section>
      <PageHeading eyebrow="QUOTE OPERATIONS" title="报价任务" detail="保存每次上传、匹配、复核与导出记录，可随时继续处理。" action={<button className="primary-button" onClick={onCreate}>＋ 新建报价任务</button>} />
      <div className="hero-metrics">
        <Metric label="进行中的任务" value={activeJobs} tone="teal" />
        <Metric label="待人工复核行" value={needReview} tone="amber" />
        <Metric label="累计任务" value={jobs.length} tone="slate" />
      </div>
      <div className="section-title"><div><h3>最近任务</h3><p>服务器会保留处理进度和人工选择</p></div></div>
      {jobs.length === 0 ? (
        <div className="empty-state"><strong>还没有报价任务</strong><span>选择客户并上传一份 Excel 询价单开始。</span><button className="primary-button" onClick={onCreate}>创建第一个任务</button></div>
      ) : (
        <div className="job-list">
          {jobs.map((job) => (
            <div className="job-card" key={job.id} role="button" tabIndex={0} onClick={() => onOpen(job.id)} onKeyDown={(event) => { if (event.key === "Enter" && renamingId !== job.id) onOpen(job.id); }}>
              <div className={`job-icon status-${job.status}`}>XLSX</div>
              <div className="job-main">
                {renamingId === job.id ? (
                  <div className="job-rename">
                    <input value={renameDraft} onChange={(event) => setRenameDraft(event.target.value)} onClick={(event) => event.stopPropagation()} onKeyDown={(event) => { if (event.key === "Enter") void submitRename(job); if (event.key === "Escape") setRenamingId(null); }} />
                    <button className="card-action-button" onClick={(event) => { event.stopPropagation(); void submitRename(job); }}>保存</button>
                    <button className="card-action-button" onClick={(event) => { event.stopPropagation(); setRenamingId(null); }}>取消</button>
                  </div>
                ) : (
                  <div><strong>{job.display_name || job.file_name}</strong><StatusPill status={job.status} /></div>
                )}
                <span>{job.customer ? `${job.customer.name} · ${CUSTOMER_COPY[job.customer.customer_type]} · ` : "一次性报价 · "}{job.database_name ? `价目库 ${job.database_name} · ` : ""}{formatDate(job.created_at)}</span>
              </div>
              <div className="job-count"><b>{job.total_lines || "—"}</b><span>产品行</span></div>
              <div className="job-count warning"><b>{job.review_lines + job.unmatched_lines || 0}</b><span>需复核</span></div>
              <div className="job-count success"><b>{job.confirmed_lines || 0}</b><span>已确认</span></div>
              <div className="job-card-actions">
                <button className="card-action-button" onClick={(event) => { event.stopPropagation(); setRenamingId(job.id); setRenameDraft(job.display_name || job.file_name); }}>重命名</button>
                <button className="card-action-button danger" onClick={(event) => { event.stopPropagation(); void removeJob(job); }}>删除</button>
              </div>
              <div className="job-arrow">→</div>
              {job.status === "matching" && <div className="job-progress"><i style={{ width: `${job.progress}%` }} /></div>}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className={`metric-card tone-${tone}`}><span>{label}</span><strong>{value.toLocaleString("zh-CN")}</strong></div>;
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status}`} title={STATUS_HINT[status]}>{STATUS_COPY[status] ?? status}</span>;
}

function NewQuote({ customers, onCreated }: { customers: Customer[]; onCreated: (job: Job) => void }) {
  const [customerId, setCustomerId] = useState<number | null>(null);
  const [customerGroup, setCustomerGroup] = useState<Customer["customer_type"]>("ordinary");
  const [customerSearch, setCustomerSearch] = useState("");
  const [databaseKey, setDatabaseKey] = useState("");
  const [databaseName, setDatabaseName] = useState("");
  const [optionCount, setOptionCount] = useState(() => {
    const saved = Number(window.localStorage.getItem("quote-option-count"));
    return Number.isInteger(saved) && saved >= 1 && saved <= 5 ? saved : 3;
  });
  const [taxPercent, setTaxPercent] = useState(() => {
    const saved = Number(window.localStorage.getItem("quote-tax-rate"));
    return Number.isFinite(saved) && saved >= 0 && saved <= 100 ? saved : 10;
  });
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selectedCustomer = customers.find((customer) => customer.id === customerId) ?? null;
  const keyword = customerSearch.trim().toLowerCase();
  const groupCustomers = customers.filter((customer) => customer.customer_type === customerGroup && (!keyword || customer.name.toLowerCase().includes(keyword)));
  function pickOptionCount(value: number) {
    setOptionCount(value);
    window.localStorage.setItem("quote-option-count", String(value));
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const data = new FormData();
      if (customerId != null) data.set("customer_id", String(customerId));
      if (databaseKey) data.set("database_key", databaseKey);
      data.set("requested_option_count", String(optionCount));
      data.set("tax_rate", String(taxPercent / 100));
      data.set("file", file);
      const response = await api<Job>("/api/quote-jobs", { method: "POST", body: data });
      onCreated(response);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务创建失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section>
      <PageHeading eyebrow="NEW QUOTATION" title="新建报价任务" detail="先确定客户与价目库，再上传询价单。匹配工作在服务器后台完成。" />
      <form className="new-quote-layout" onSubmit={submit}>
        <div className="form-card">
          <div className="step-label"><b>01</b><span>选择客户</span></div>
          <div className="customer-group-tabs">{CUSTOMER_GROUPS.map((item) => <button type="button" key={item.type} className={customerGroup === item.type ? "active" : ""} onClick={() => setCustomerGroup(item.type)}>{item.title}</button>)}</div>
          <div className="row-search customer-picker-search"><span>⌕</span><input value={customerSearch} onChange={(event) => setCustomerSearch(event.target.value)} placeholder="按客户名称过滤" /></div>
          <div className="customer-picker-list">
            <button type="button" className={customerId === null ? "active" : ""} onClick={() => setCustomerId(null)}><strong>不关联客户（一次性报价）</strong><span>不套用任何客户折扣与特殊要求</span></button>
            {groupCustomers.map((customer) => <button type="button" key={customer.id} className={customerId === customer.id ? "active" : ""} onClick={() => setCustomerId(customer.id)}><strong>{customer.name}</strong><span>{customer.notes || "暂无说明"}</span></button>)}
            {groupCustomers.length === 0 && <small className="picker-empty">该分组下没有匹配的客户</small>}
          </div>
          {selectedCustomer && <CustomerPolicy customer={selectedCustomer} />}
          <DatabasePicker
            value={databaseKey}
            onChange={(key, name) => { setDatabaseKey(key); setDatabaseName(name); }}
          />
          <div className="step-label"><b>03</b><span>多报价设置</span></div>
          <div className="option-picker"><div><strong>每个商品推荐几个制造商方案？</strong><span>候选不足时按实际可用数量展示</span></div><div>{[1, 2, 3, 4, 5].map((value) => <button type="button" className={optionCount === value ? "active" : ""} key={value} onClick={() => pickOptionCount(value)}>{value}</button>)}</div></div>
          <div className="option-picker"><div><strong>税后价格税率？（%）</strong><span>导出时按 确认单价×(1+税率) 计算 税后单价 / 税后总价</span></div><div><input className="tax-rate-input" type="number" min={0} max={100} step={0.1} value={taxPercent} onChange={(event) => { const value = Number(event.target.value); if (Number.isFinite(value)) { setTaxPercent(value); window.localStorage.setItem("quote-tax-rate", String(value)); } }} /></div></div>
        </div>
        <div className="form-card upload-section">
          <div className="step-label"><b>04</b><span>上传询价单</span></div>
          <label className={`upload-card ${file ? "has-file" : ""}`}>
            <input type="file" accept=".xlsx,.xlsm,.xls,.csv" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
            <span className="upload-icon">↑</span>
            <strong>{file ? file.name : "选择待报价 Excel"}</strong>
            <span>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB · 可随时重新选择` : "支持 .xlsx / .xlsm / .xls / .csv，单文件不超过 80MB"}</span>
            <em>{file ? "更换文件" : "浏览文件"}</em>
          </label>
          {error && <div className="form-error">{error}</div>}
          {databaseKey && <p className="picker-selected muted">价目来源：{databaseName || databaseKey}</p>}
          <button className="primary-button submit-task" disabled={!file || busy}>{busy ? "正在上传…" : customerId != null ? `创建任务并推荐 ${optionCount} 个方案` : `创建一次性报价并推荐 ${optionCount} 个方案`}</button>
        </div>
      </form>
    </section>
  );
}

function CustomerPolicy({ customer }: { customer: Customer }) {
  return (
    <div className={`customer-policy type-${customer.customer_type}`}>
      <div><span>{CUSTOMER_COPY[customer.customer_type]}</span><strong>{customer.name}</strong></div>
      <p>{customer.notes || "暂无客户策略说明"}</p>
      {customer.discount_percent > 0 && <small>协议折扣 {customer.discount_percent}% · 需核对最低毛利</small>}
      {customer.requirements.length > 0 && <ul>{customer.requirements.map((item) => <li key={item.id}><b>{item.required ? "必选" : "偏好"}</b>{item.attribute_name} {item.operator} {item.value}{item.unit}</li>)}</ul>}
    </div>
  );
}

function JobWorkspace({ job, onBack, onRefresh, notify }: { job: Job; onBack: () => void; onRefresh: () => Promise<void>; notify: (message: string) => void }) {
  const [result, setResult] = useState<PageResult>({ items: [], total: 0, page: 1, page_size: 50, page_count: 1 });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [rowFilter, setRowFilter] = useState<RowFilter>("all");
  const [sort, setSort] = useState<LineSort>("score_asc");
  const [filterCounts, setFilterCounts] = useState<Partial<Record<RowFilter, number>>>({});
  const [query, setQuery] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [pageDraft, setPageDraft] = useState("1");
  const [loading, setLoading] = useState(false);
  const [selectedLineId, setSelectedLineId] = useState<number | null>(null);
  const [taxDraft, setTaxDraft] = useState(() => String(Math.round((job.tax_rate ?? 0.1) * 1000) / 10));

  const saveTax = useCallback(async () => {
    const value = Number(taxDraft);
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      setTaxDraft(String(Math.round((job.tax_rate ?? 0.1) * 1000) / 10));
      return;
    }
    const rate = value / 100;
    if (Math.abs(rate - (job.tax_rate ?? 0.1)) < 1e-9) return;
    await api(`/api/quote-jobs/${job.id}`, { method: "PATCH", body: JSON.stringify({ tax_rate: rate }) });
    await onRefresh();
  }, [job.id, job.tax_rate, taxDraft, onRefresh]);

  const loadLines = useCallback(async () => {
    if (["queued", "reserved", "matching"].includes(job.status)) return;
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), row_filter: rowFilter, query });
      if (sort !== "source_row") params.set("sort", sort);
      const data = await api<PageResult>(`/api/quote-jobs/${job.id}/lines?${params}`);
      setResult(data);
      if (data.page !== page) setPage(data.page);
      setPageDraft(String(data.page));
    } finally {
      setLoading(false);
    }
  }, [job.id, job.status, page, pageSize, query, rowFilter, sort]);

  const loadFilterCounts = useCallback(async () => {
    if (["queued", "reserved", "matching"].includes(job.status)) return;
    const entries = await Promise.all(
      ROW_FILTER_TABS.map(async ([value]) => {
        const data = await api<PageResult>(`/api/quote-jobs/${job.id}/lines?page=1&page_size=25&row_filter=${value}`);
        return [value, data.total] as const;
      }),
    );
    setFilterCounts(Object.fromEntries(entries));
  }, [job.id, job.status]);

  // Loading the selected server page is the synchronization purpose of this effect.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void loadLines(); }, [loadLines]);
  // Filter counts refresh whenever the job record changes on the server (updated_at bump).
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void loadFilterCounts(); }, [loadFilterCounts, job.updated_at]);
  useEffect(() => {
    if (!["queued", "reserved", "matching"].includes(job.status)) return;
    const timer = setInterval(() => void onRefresh(), 1500);
    return () => clearInterval(timer);
  }, [job.status, onRefresh]);

  async function batchConfirm(scope: "current" | "filtered") {
    const response = await api<{ confirmed: number; skipped: number }>(`/api/quote-jobs/${job.id}/batch-confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope, page, page_size: pageSize, row_filter: rowFilter, query }),
    });
    notify(`已安全确认 ${response.confirmed} 行；${response.skipped} 行因置信度或风险被跳过。`);
    await Promise.all([loadLines(), onRefresh()]);
  }

  async function autoExport(variant: "internal" | "customer") {
    const response = await fetch(`/api/quote-jobs/${job.id}/auto-confirm-and-export/${variant}`, { method: "POST", credentials: "include" });
    if (!response.ok) {
      const payload = (await response.json()) as { detail?: string };
      throw new Error(payload.detail || "一键导出失败");
    }
    await onRefresh();
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") ?? "";
    const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i)?.[1];
    const name = encoded ? decodeURIComponent(encoded) : `${job.file_name}_${variant}_auto.xlsx`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
    notify("一键导出完成：自动带价行已确认，未带价行留待复核。");
  }

  async function download(variant: "internal" | "customer") {
    const response = await fetch(`/api/quote-jobs/${job.id}/export/${variant}`, { method: "POST", credentials: "include" });
    if (!response.ok) {
      const payload = (await response.json()) as { detail?: string };
      throw new Error(payload.detail || "导出失败");
    }
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") ?? "";
    const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i)?.[1];
    const name = encoded ? decodeURIComponent(encoded) : `${job.file_name}_${variant}.xlsx`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
    notify(variant === "internal" ? "内部复核版已导出。" : "客户版已导出，内部评分与来源已隐藏。");
    await onRefresh();
  }

  async function reprocess() {
    await api<{ id: string; status: string }>(`/api/quote-jobs/${job.id}/reprocess`, { method: "POST" });
    notify("已重新提交匹配任务，原文件无需重复上传。");
    await onRefresh();
  }

  function applySearch(event: FormEvent) {
    event.preventDefault();
    setPage(1);
    setQuery(searchDraft.trim());
  }

  function jumpPage() {
    const parsed = Number.parseInt(pageDraft, 10);
    const safe = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), result.page_count) : 1;
    setPage(safe);
    setPageDraft(String(safe));
  }

  if (["queued", "reserved", "matching"].includes(job.status)) {
    return (
      <section><button className="back-button" onClick={onBack}>← 返回任务列表</button><div className="processing-card"><div className="processing-orbit"><span>{job.progress}%</span></div><p className="eyebrow">SERVER MATCHING</p><h2>正在进行参数与单位校验</h2><p>页面可以关闭，服务器会继续处理，完成后可从任务中心继续复核。</p><div className="wide-progress"><i style={{ width: `${Math.max(job.progress, 4)}%` }} /></div></div></section>
    );
  }

  if (job.status === "failed") {
    return <section><button className="back-button" onClick={onBack}>← 返回任务列表</button><div className="failure-card"><strong>任务处理失败</strong><p>{job.error_message}</p><div className="review-actions"><button className="primary-button" onClick={() => void reprocess()}>重新处理</button><button className="secondary-button" onClick={() => onRefresh()}>刷新状态</button></div></div></section>;
  }

  return (
    <section>
      <button className="back-button" onClick={onBack}>← 返回任务列表</button>
      <PageHeading eyebrow="QUOTATION REVIEW" title={job.display_name || job.file_name} detail={`${job.customer?.name ?? "一次性报价"} · 价目库 ${job.database_name || "系统默认库"} · 每商品最多 ${job.requested_option_count} 个制造商方案· 税后税率 ${Math.round((job.tax_rate ?? 0.1) * 1000) / 10}%`} action={<StatusPill status={job.status} />} />
      <div className="tax-notice"><label htmlFor="job-tax-rate">税后价格税率</label><input id="job-tax-rate" type="number" min={0} max={100} step={0.1} value={taxDraft} onChange={(event) => setTaxDraft(event.target.value)} onBlur={() => void saveTax()} onKeyDown={(event) => { if (event.key === "Enter") void saveTax(); }} /><span>% · 导出时按 确认单价×(1+税率) 计算 税后单价 / 税后总价（回车或失焦保存）</span></div>
      <div className="confirm-progress">
        <span>已确认 {job.confirmed_lines} / {job.total_lines} 行</span>
        <i><b style={{ width: `${job.total_lines ? Math.min(100, (job.confirmed_lines / job.total_lines) * 100) : 0}%` }} /></i>
      </div>
      <div className="summary-grid">
        <Metric label="产品行" value={job.total_lines} tone="slate" />
        <Metric label="高置信建议" value={job.matched_lines} tone="teal" />
        <Metric label="需人工复核" value={job.review_lines} tone="amber" />
        <Metric label="无可靠匹配" value={job.unmatched_lines} tone="red" />
        <Metric label="已确认" value={job.confirmed_lines} tone="blue" />
      </div>

      <div className="review-card">
        <div className="review-toolbar">
          <form className="row-search" onSubmit={applySearch}><span>⌕</span><input value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="名称、编码、型号或原表行号" /><button>定位</button></form>
          <div className="review-actions">
            <button className="secondary-button" onClick={() => void batchConfirm("current")}>安全确认当前页</button>
            <button className="secondary-button" onClick={() => void batchConfirm("filtered")}>安全确认筛选结果</button>
            <div className="export-menu"><button className="primary-button">导出 ▾</button><div><button onClick={() => void download("internal")}>内部复核版</button><button onClick={() => void download("customer")}>客户版</button><button onClick={() => void autoExport("customer")}>一键导出客户版（自动确认）</button></div></div>
          </div>
        </div>
        <div className="exception-tabs" aria-label="报价行筛选">
          {ROW_FILTER_TABS.map(([value, label]) => (
            <button key={value} title={STATUS_HINT[value]} className={rowFilter === value ? "active" : ""} onClick={() => { setRowFilter(value); setPage(1); }}>
              {label}{filterCounts[value] != null && <b className="filter-badge">{filterCounts[value]}</b>}
            </button>
          ))}
        </div>
        <div className={`table-scroll ${loading ? "loading" : ""}`}>
          <table className="quote-table">
            <thead><tr><th>原表行</th><th>产品 / 参数</th><th>型号 / 单位</th><th>数量</th><th>首选报价</th><th><button type="button" className="sort-button" title="点击切换排序：评分升序 → 评分降序 → 原表行号" onClick={() => { setSort(SORT_CYCLE[sort]); setPage(1); }}>{SORT_LABEL[sort]}</button></th><th>风险状态</th><th>方案</th></tr></thead>
            <tbody>
              {result.items.map((line) => {
                const unitConflict = line.warnings.some((item) => item.includes("单位不可直接换算"));
                return (
                  <tr key={line.id} className={unitConflict ? "row-blocked" : line.status === "unmatched" ? "row-unmatched" : ""}>
                    <td className="mono">{line.source_row}</td>
                    <td className="product-cell"><strong>{line.name}</strong><small title={line.spec}>{line.spec || "无参数描述"}</small></td>
                    <td><span>{line.model || "—"}</span><small className={unitConflict ? "unit-danger" : ""}>{line.unit || "无单位"}{unitConflict ? " · 冲突" : ""}</small></td>
                    <td>{line.pricing_quantity ?? "—"}</td>
                    <td><strong className="price">¥ {money(line.primary_option?.final_price)}</strong><small>{line.primary_option?.record.manufacturer || "尚未选择"}</small></td>
                    <td><span className={`confidence confidence-${line.confidence}`}>{CONFIDENCE_COPY[line.confidence] ?? line.confidence}</span><small>{line.recommended_score.toFixed(1)} 分</small></td>
                    <td>{line.confirmed ? <StatusPill status="confirmed" /> : <StatusPill status={line.status} />}<small className={blockingWarnings(line.warnings).length ? "danger-text" : ""}>{line.warnings[0]?.replace("BLOCK: ", "") || "无阻断风险"}</small></td>
                    <td><button className="text-button" onClick={() => setSelectedLineId(line.id)}>{line.selected_option_count ? `${line.selected_option_count} 个方案` : "选择方案"} →</button></td>
                  </tr>
                );
              })}
              {!loading && result.items.length === 0 && <tr><td colSpan={8}><div className="table-empty">当前筛选条件下没有报价行</div></td></tr>}
            </tbody>
          </table>
        </div>
        <div className="pagination">
          <div><span>共 {result.total.toLocaleString("zh-CN")} 条</span><label>每页<select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}>{[25, 50, 100].map((size) => <option key={size}>{size}</option>)}</select></label></div>
          <div className="page-controls"><button disabled={page <= 1} onClick={() => setPage(1)}>首页</button><button disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button><label>第<input aria-label="跳转页码" value={pageDraft} onChange={(event) => setPageDraft(event.target.value)} onBlur={jumpPage} onKeyDown={(event) => { if (event.key === "Enter") jumpPage(); }} />页 / {result.page_count}</label><button disabled={page >= result.page_count} onClick={() => setPage((value) => value + 1)}>下一页</button><button disabled={page >= result.page_count} onClick={() => setPage(result.page_count)}>末页</button></div>
        </div>
      </div>
      {selectedLineId != null && <CandidateDrawer lineId={selectedLineId} maxOptions={job.requested_option_count} onClose={() => setSelectedLineId(null)} onChanged={async () => { await Promise.all([loadLines(), onRefresh()]); }} />}
    </section>
  );
}

function CandidateDrawer({ lineId, maxOptions, onClose, onChanged }: { lineId: number; maxOptions: number; onClose: () => void; onChanged: () => Promise<void> }) {
  const [line, setLine] = useState<QuoteLine | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [prices, setPrices] = useState<Record<string, number>>({});
  const [note, setNote] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [drawerNotice, setDrawerNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [showManualForm, setShowManualForm] = useState(false);
  const [manualManufacturer, setManualManufacturer] = useState("");
  const [manualBrand, setManualBrand] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [manualSpec, setManualSpec] = useState("");
  const [manualUnit, setManualUnit] = useState("");
  const [manualPrice, setManualPrice] = useState("");
  const [manualError, setManualError] = useState("");
  const [manualBusy, setManualBusy] = useState(false);

  const load = useCallback(async () => {
    const data = await api<QuoteLine>(`/api/quote-lines/${lineId}`);
    setLine(data);
    const options = data.options ?? [];
    setSelected(options.filter((item) => item.selected).map((item) => item.id));
    setPrices(Object.fromEntries(options.map((item) => [String(item.id), item.final_price])));
  }, [lineId]);
  // Fetching the server-owned candidate state is the synchronization purpose of this effect.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void load(); }, [load]);

  const options = dedupeOptions(line?.options ?? []);
  const selectedOptions = options.filter((item) => selected.includes(item.id));
  const hasBlocking = selectedOptions.some((item) => blockingWarnings(item.warnings).length > 0);
  const historicalPrices = options.map((item) => item.normalized_price ?? item.record.price).filter((price) => price > 0);
  const priceRange = historicalPrices.length
    ? `¥${money(Math.min(...historicalPrices))} — ¥${money(Math.max(...historicalPrices))}`
    : "暂无有效价格";
  const medianPrice = historicalPrices.length
    ? (() => { const s = [...historicalPrices].sort((a, b) => a - b); const m = Math.floor(s.length / 2); return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; })()
    : null;
  const sourceGroups = Object.entries(
    options.reduce<Record<string, number[]>>((acc, item) => {
      const price = item.normalized_price ?? item.record.price;
      if (!(price > 0)) return acc;
      const key = item.record.manufacturer || item.record.source_file || "其他";
      (acc[key] ??= []).push(price);
      return acc;
    }, {}),
  )
    .map(([name, prices]) => ({ name: String(name).slice(0, 12), count: prices.length, min: Math.min(...prices), max: Math.max(...prices) }))
    .sort((a, b) => b.count - a.count);

  function toggle(option: QuoteOption) {
    setDrawerNotice("");
    setSelected((current) => {
      if (current.includes(option.id)) return current.filter((id) => id !== option.id);
      if (current.length >= maxOptions) {
        setDrawerNotice(`每个商品最多选择 ${maxOptions} 个方案。`);
        return current;
      }
      const key = optionKey(option);
      if (options.some((item) => current.includes(item.id) && optionKey(item) === key)) {
        setDrawerNotice("不能重复选择完全相同的方案。");
        return current;
      }
      return [...current, option.id];
    });
  }

  async function submitManual(event: FormEvent) {
    event.preventDefault();
    const parsed = Number(manualPrice);
    if (!manualManufacturer.trim() && !manualBrand.trim()) { setManualError("制造商和品牌至少填写一项。"); return; }
    if (!Number.isFinite(parsed) || parsed <= 0) { setManualError("请填写有效的价格（大于 0 的数字）。"); return; }
    setManualBusy(true);
    setManualError("");
    try {
      const payload: Record<string, string | number> = { price: parsed };
      if (manualManufacturer.trim()) payload.manufacturer = manualManufacturer.trim();
      if (manualBrand.trim()) payload.brand = manualBrand.trim();
      if (manualModel.trim()) payload.model = manualModel.trim();
      if (manualSpec.trim()) payload.spec = manualSpec.trim();
      if (manualUnit.trim()) payload.unit = manualUnit.trim();
      const created = await api<QuoteOption>(`/api/quote-lines/${lineId}/options`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const previousSelected = selected;
      await Promise.all([load(), onChanged()]);
      // load() resets the selection from the server; keep local unsaved picks and add the new option.
      setSelected(Array.from(new Set([...previousSelected, created.id])));
      setPrices((current) => ({ ...current, [String(created.id)]: created.final_price }));
      setManualManufacturer("");
      setManualBrand("");
      setManualModel("");
      setManualSpec("");
      setManualUnit("");
      setManualPrice("");
      setShowManualForm(false);
      setDrawerNotice("手工方案已创建并选用；该行已回到待复核状态。");
    } catch (reason) {
      setManualError(reason instanceof Error ? reason.message : "手工方案创建失败");
    } finally {
      setManualBusy(false);
    }
  }

  async function removeManualOption(option: QuoteOption) {
    const maker = option.record.manufacturer || option.record.brand || "未命名";
    if (!window.confirm(`确定删除手工方案「${maker} · ¥${money(option.final_price)}」吗？`)) return;
    try {
      await api<{ ok: boolean }>(`/api/quote-options/${option.id}`, { method: "DELETE" });
      setSelected((current) => current.filter((id) => id !== option.id));
      setDrawerNotice("手工方案已删除。");
      await Promise.all([load(), onChanged()]);
    } catch (reason) {
      setDrawerNotice(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  async function save(confirm: boolean) {
    if (!selected.length) { setDrawerNotice("请至少选择一个制造商方案。"); return; }
    setBusy(true);
    setDrawerNotice("");
    try {
      await api<QuoteLine>(`/api/quote-lines/${lineId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected_option_ids: selected, final_prices: prices, manual_note: note }),
      });
      if (confirm) {
        await api<QuoteLine>(`/api/quote-lines/${lineId}/confirm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ override_reason: overrideReason }),
        });
        setDrawerNotice("当前商品已确认，所有操作已写入审计记录。");
      } else {
        setDrawerNotice("方案已保存，可稍后继续确认。");
      }
      await Promise.all([load(), onChanged()]);
      if (confirm) setTimeout(onClose, 900);
    } catch (reason) {
      setDrawerNotice(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-backdrop">
      <button className="drawer-dismiss" aria-label="关闭候选对比" onClick={onClose} />
      <aside className="candidate-drawer" role="dialog" aria-modal="true" aria-label="报价候选对比">
        <header><div><p className="eyebrow">CANDIDATE COMPARISON</p><h2>{line?.name ?? "正在加载…"}</h2><span>最多选择 {maxOptions} 个不同制造商方案</span></div><button onClick={onClose} aria-label="关闭">×</button></header>
        {line && <div className="target-brief"><div><span>待报价参数</span><p>{line.spec || "无参数描述"}</p></div><div><span>目标型号 / 单位</span><strong>{line.model || "—"} / {line.unit || "无单位"}</strong></div><div><span>候选历史价区间{medianPrice != null ? ` · 中位 ¥${money(medianPrice)}` : ""}</span><strong>{priceRange}</strong><small className="price-chips">{sourceGroups.map((group) => <em key={group.name} title={`${group.name}：${group.count} 条候选`}>{group.name}×{group.count} ¥{money(group.min)}-{money(group.max)}</em>)}</small></div></div>}
        {line && line.warnings.length > 0 && <div className={`drawer-alert ${blockingWarnings(line.warnings).length ? "danger" : "warning"}`}><strong>{blockingWarnings(line.warnings).length ? "存在阻断风险" : "请人工核对"}</strong><span>{line.warnings.join("；").replaceAll("BLOCK: ", "")}</span></div>}
        <div className="candidate-list">
          {options.map((option) => {
            const isSelected = selected.includes(option.id);
            const blocked = blockingWarnings(option.warnings).length > 0;
            const manual = isManualOption(option);
            return (
              <article className={`candidate-card ${isSelected ? "selected" : ""} ${blocked ? "blocked" : ""} ${manual ? "manual" : ""}`} key={option.id}>
                <button className="candidate-select" onClick={() => toggle(option)} aria-pressed={isSelected}>{isSelected ? "✓" : "+"}</button>
                <div className="candidate-head"><div>{manual ? <span className="manual-badge">手工方案</span> : <span className="rank">#{option.rank}</span>}<strong>{option.record.manufacturer || "未记录制造商"}</strong><small>{option.record.brand || "无品牌"}</small></div><div>{manual && <button className="candidate-delete" title="删除该手工方案" aria-label="删除该手工方案" onClick={() => void removeManualOption(option)}>×</button>}<b>¥ {money(prices[String(option.id)])}</b><span>{manual ? CONFIDENCE_COPY.manual : `${option.score.toFixed(1)} 分`}</span></div></div>
                <div className="candidate-facts"><span><b>型号</b>{option.record.model || "—"}</span><span className={`unit-${option.unit_status}`}><b>单位</b>{option.record.unit || "—"}{option.unit_status === "convertible" ? ` → ${line?.unit}` : ""}</span><span><b>报价日期</b>{option.record.quote_date || "未记录"}</span></div>
                <p className="candidate-spec">{option.record.spec || "无参数描述"}</p>
                {Object.keys(option.component_scores).length > 0 && <div className="score-bars">{Object.entries(option.component_scores).map(([label, score]) => <div key={label}><span>{label}</span><i><b style={{ width: `${Math.min(100, score / ({ 参数: 45, 型号: 15, 名称: 15, "制造商/品牌": 10, 单位: 10, 数据质量: 5 } as Record<string, number>)[label] * 100)}%` }} /></i><em>{score}</em></div>)}</div>}
                {option.reasons.length > 0 && <div className="reason-list">{option.reasons.map((reason) => <span key={reason}>{reason}</span>)}</div>}
                {option.warnings.length > 0 && <div className={`option-warning ${blocked ? "danger" : ""}`}>{option.warnings.join("；").replaceAll("BLOCK: ", "")}</div>}
                <footer><label>方案报价（元）<input type="number" min="0.01" step="0.01" value={prices[String(option.id)] ?? ""} onChange={(event) => setPrices((current) => ({ ...current, [String(option.id)]: Number(event.target.value) }))} /></label><span>{manual ? "手工录入" : `${option.record.source_file} · ${option.record.source_sheet} · 第${option.record.source_row}行`}</span></footer>
              </article>
            );
          })}
          {line && options.length === 0 && <div className="empty-state compact"><strong>没有可靠候选</strong><span>请补充历史数据或人工报价。</span></div>}
        </div>
        <div className="manual-option-block">
          {!showManualForm ? (
            <button className="secondary-button" onClick={() => { setShowManualForm(true); setManualError(""); }}>＋ 新建手工方案</button>
          ) : (
            <form className="manual-option-form" onSubmit={submitManual}>
              <label><span>制造商 *</span><input value={manualManufacturer} onChange={(event) => setManualManufacturer(event.target.value)} placeholder="与品牌至少填一项" /></label>
              <label><span>品牌 *</span><input value={manualBrand} onChange={(event) => setManualBrand(event.target.value)} placeholder="与制造商至少填一项" /></label>
              <label><span>型号</span><input value={manualModel} onChange={(event) => setManualModel(event.target.value)} /></label>
              <label><span>参数</span><input value={manualSpec} onChange={(event) => setManualSpec(event.target.value)} /></label>
              <label><span>单位</span><input value={manualUnit} onChange={(event) => setManualUnit(event.target.value)} /></label>
              <label><span>价格（元）*</span><input type="number" min="0.01" step="0.01" value={manualPrice} onChange={(event) => setManualPrice(event.target.value)} required /></label>
              <div className="manual-option-actions">
                <button type="button" className="secondary-button" onClick={() => setShowManualForm(false)}>取消</button>
                <button className="primary-button" disabled={manualBusy}>{manualBusy ? "正在保存…" : "保存并选用"}</button>
              </div>
              {manualError && <div className="form-error">{manualError}</div>}
            </form>
          )}
        </div>
        <div className="drawer-footer">
          {drawerNotice && <div className="drawer-notice" role="status">{drawerNotice}</div>}
          <div className="drawer-fields"><label><span>调整说明</span><input value={note} onChange={(event) => setNote(event.target.value)} placeholder="选填：改价或选厂家的原因" /></label>{hasBlocking && <label><span>阻断风险覆盖原因（选填）</span><input value={overrideReason} onChange={(event) => setOverrideReason(event.target.value)} placeholder="说明为何仍可确认" /></label>}</div>
          <div className="drawer-actions"><button className="secondary-button" onClick={() => void save(false)} disabled={busy}>保存方案</button><button className="primary-button confirm-action" onClick={() => void save(true)} disabled={busy || !selected.length}>{busy ? "正在保存…" : "确认当前商品"}</button></div>
        </div>
      </aside>
    </div>
  );
}

function CustomerDirectory({ customers, user, onUpdated, notify }: { customers: Customer[]; user: User; onUpdated: () => Promise<void>; notify: (message: string) => void }) {
  const isAdmin = user.role === "admin";
  const [group, setGroup] = useState<Customer["customer_type"] | null>(null);
  const [search, setSearch] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<Customer | null>(null);

  async function removeCustomer(customer: Customer) {
    if (!window.confirm(`确定删除客户「${customer.name}」吗？删除后该客户不再出现在客户列表中。`)) return;
    try {
      await api<{ ok: boolean }>(`/api/customers/${customer.id}`, { method: "DELETE" });
      notify("客户档案已删除。");
      await onUpdated();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  if (group === null) {
    return (
      <section>
        <PageHeading eyebrow="CUSTOMER POLICIES" title="客户档案" detail="客户类型不仅是标签，还会影响特殊要求校验和报价策略。" />
        <div className="customer-grid">
          {CUSTOMER_GROUPS.map((item) => (
            <button className={`group-card type-${item.type}`} key={item.type} onClick={() => { setGroup(item.type); setSearch(""); setShowForm(false); setEditing(null); }}>
              <span>{item.title}</span>
              <strong>{customers.filter((customer) => customer.customer_type === item.type).length}</strong>
              <small>{item.detail} · 点击查看名单 →</small>
            </button>
          ))}
        </div>
      </section>
    );
  }

  const groupMeta = CUSTOMER_GROUPS.find((item) => item.type === group);
  const groupTotal = customers.filter((customer) => customer.customer_type === group).length;
  const keyword = search.trim().toLowerCase();
  const visible = customers.filter((customer) => customer.customer_type === group && (!keyword || customer.name.toLowerCase().includes(keyword)));

  return (
    <section>
      <button className="back-button" onClick={() => { setGroup(null); setShowForm(false); setEditing(null); }}>← 返回分组</button>
      <PageHeading
        eyebrow="CUSTOMER POLICIES"
        title={groupMeta?.title ?? "客户档案"}
        detail={`共 ${groupTotal} 位客户 · ${groupMeta?.detail ?? ""}`}
        action={isAdmin ? <button className="primary-button" onClick={() => { setEditing(null); setShowForm((value) => !value); }}>{showForm && !editing ? "取消" : "＋ 新建客户"}</button> : undefined}
      />
      <div className="row-search customer-search"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="按客户名称过滤" /></div>
      {(showForm || editing) && (
        <CustomerForm
          key={editing ? `edit-${editing.id}` : `new-${group}`}
          initial={editing}
          defaultType={group}
          onSaved={async (message) => { setShowForm(false); setEditing(null); await onUpdated(); notify(message); }}
        />
      )}
      {visible.length === 0 ? (
        <div className="empty-state compact"><strong>{keyword ? "没有匹配的客户" : "该分组还没有客户"}</strong><span>{isAdmin ? "点击右上角「新建客户」添加。" : "请联系管理员添加客户档案。"}</span></div>
      ) : (
        <div className="customer-grid">{visible.map((customer) => (
          <div className={`customer-card type-${customer.customer_type}`} key={customer.id}>
            <header><span>{CUSTOMER_COPY[customer.customer_type]}</span><h3>{customer.name}</h3></header>
            <p>{customer.notes || "暂无说明"}</p>
            <dl><div><dt>协议折扣</dt><dd>{customer.discount_percent ? `${customer.discount_percent}%` : "标准价"}</dd></div><div><dt>最低毛利线</dt><dd>{customer.minimum_margin_percent ? `${customer.minimum_margin_percent}%` : "未设置"}</dd></div></dl>
            <div className="requirement-block"><strong>结构化要求</strong>{customer.requirements.length ? customer.requirements.map((item) => <span key={item.id}><b>{item.required ? "必选" : "偏好"}</b>{item.attribute_name} {item.operator} {item.value}{item.unit}</span>) : <small>暂无特殊要求</small>}</div>
            {isAdmin && (
              <footer className="customer-card-actions">
                <button className="card-action-button" onClick={() => { setEditing(customer); setShowForm(false); }}>编辑</button>
                <button className="card-action-button danger" onClick={() => void removeCustomer(customer)}>删除</button>
              </footer>
            )}
          </div>
        ))}</div>
      )}
    </section>
  );
}

function CustomerForm({ initial, defaultType, onSaved }: { initial: Customer | null; defaultType: Customer["customer_type"]; onSaved: (message: string) => void | Promise<void> }) {
  const editing = initial !== null;
  const [name, setName] = useState(initial?.name ?? "");
  const [type, setType] = useState<Customer["customer_type"]>(initial?.customer_type ?? defaultType);
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [attribute, setAttribute] = useState("");
  const [operator, setOperator] = useState("contains");
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState("");
  const [required, setRequired] = useState(true);
  const [requirements, setRequirements] = useState<Array<Omit<Requirement, "id">>>(initial?.requirements.map(({ attribute_name, operator: itemOperator, value: itemValue, unit: itemUnit, required: itemRequired, notes: itemNotes }) => ({ attribute_name, operator: itemOperator, value: itemValue, unit: itemUnit, required: itemRequired, notes: itemNotes })) ?? []);
  const [discount, setDiscount] = useState(initial?.discount_percent ?? 0);
  const [minimumMargin, setMinimumMargin] = useState(initial?.minimum_margin_percent ?? 0);
  const [preferredManufacturers, setPreferredManufacturers] = useState(initial?.preferred_manufacturers.join(", ") ?? "");
  const [error, setError] = useState("");

  function currentRequirement(): Omit<Requirement, "id"> | null {
    if (!attribute.trim() || !value.trim()) return null;
    return { attribute_name: attribute.trim(), operator, value: value.trim(), unit: unit.trim(), required, notes: "" };
  }

  function addRequirement() {
    const item = currentRequirement();
    if (!item) {
      setError("请先填写属性名和目标值。");
      return;
    }
    setRequirements((items) => [...items, item]);
    setAttribute("");
    setValue("");
    setUnit("");
    setError("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      const draft = currentRequirement();
      const allRequirements = [...requirements, ...(draft ? [draft] : [])];
      if (type === "special" && allRequirements.length === 0) {
        setError("特殊要求客户至少需要一条结构化要求。");
        return;
      }
      const payload = {
        name,
        customer_type: editing ? type : defaultType,
        notes,
        requirements: type === "ordinary" ? [] : allRequirements,
        discount_percent: type === "vip" ? discount : 0,
        minimum_margin_percent: type === "vip" ? minimumMargin : 0,
        preferred_manufacturers: type === "vip"
          ? preferredManufacturers.split(/[,，]/).map((item) => item.trim()).filter(Boolean)
          : [],
      };
      if (editing) {
        await api<Customer>(`/api/customers/${initial.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      } else {
        await api<Customer>("/api/customers", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      }
      await onSaved(editing ? "客户档案已更新。" : "客户档案已创建。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); }
  }
  return (
    <form className="inline-form customer-editor" onSubmit={submit}>
      <label><span>客户名称</span><input value={name} onChange={(event) => setName(event.target.value)} required /></label>
      {editing ? (
        <label><span>客户类型</span><select value={type} onChange={(event) => setType(event.target.value as Customer["customer_type"])}><option value="ordinary">普通客户</option><option value="special">特殊要求</option><option value="vip">VIP客户</option></select></label>
      ) : (
        <label><span>客户类型</span><b className="type-static">{CUSTOMER_COPY[defaultType]}</b></label>
      )}
      <label className="wide-field"><span>策略说明 / 自由文本要求</span><input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="选填，用于补充无法结构化的说明" /></label>

      {type !== "ordinary" && (
        <div className="requirement-editor">
          <label><span>参数名</span><input list="attribute-suggestions" value={attribute} onChange={(event) => setAttribute(event.target.value)} placeholder="例：容量" /><datalist id="attribute-suggestions">{ATTRIBUTE_SUGGESTIONS.map((item) => <option key={item} value={item} />)}</datalist></label>
          <label><span>比较关系</span><select value={operator} onChange={(event) => setOperator(event.target.value)}><option value="contains">包含</option><option value="≥">大于等于</option><option value="≤">小于等于</option><option value="=">等于</option></select></label>
          <label><span>目标值</span><input value={value} onChange={(event) => setValue(event.target.value)} placeholder="例：100" /></label>
          <label><span>单位</span><input value={unit} onChange={(event) => setUnit(event.target.value)} placeholder="例：ml" /></label>
          <label><span>约束级别</span><select value={required ? "required" : "preferred"} onChange={(event) => setRequired(event.target.value === "required")}><option value="required">必须满足</option><option value="preferred">偏好</option></select></label>
          <button type="button" className="secondary-button" onClick={addRequirement}>＋ 添加要求</button>
          {requirements.length > 0 && <div className="requirement-drafts">{requirements.map((item, index) => <span key={`${item.attribute_name}-${index}`}><b>{item.required ? "必选" : "偏好"}</b>{item.attribute_name} {item.operator} {item.value}{item.unit}<button type="button" aria-label={`删除 ${item.attribute_name}`} onClick={() => setRequirements((items) => items.filter((_, itemIndex) => itemIndex !== index))}>×</button></span>)}</div>}
        </div>
      )}

      {type === "vip" && <>
        <label><span>协议折扣（%）</span><input type="number" min="0" max="100" step="0.1" value={discount} onChange={(event) => setDiscount(Number(event.target.value))} /></label>
        <label><span>最低毛利线（%）</span><input type="number" min="0" max="100" step="0.1" value={minimumMargin} onChange={(event) => setMinimumMargin(Number(event.target.value))} /></label>
        <label className="wide-field"><span>偏好制造商</span><input value={preferredManufacturers} onChange={(event) => setPreferredManufacturers(event.target.value)} placeholder="多个制造商用逗号分隔" /></label>
      </>}

      <button className="primary-button">{editing ? "保存修改" : "保存客户"}</button>
      {error && <div className="form-error">{error}</div>}
    </form>
  );
}

function HistorySearch({ user, notify }: { user: User; notify: (message: string) => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<HistoryResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [showEntry, setShowEntry] = useState(false);

  async function runSearch(keyword: string) {
    if (!keyword.trim()) return;
    setLoading(true);
    try { setResults(await api<HistoryResult[]>(`/api/history/search?q=${encodeURIComponent(keyword.trim())}`)); } finally { setLoading(false); }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    await runSearch(query);
  }
  return (
    <section>
      <PageHeading eyebrow="HISTORICAL PRICES" title="历史报价查询" detail="从服务器数据库查询产品、参数、型号、品牌和制造商。" action={user.role === "admin" ? <button className="primary-button" onClick={() => setShowEntry(true)}>＋ 手工录入</button> : undefined} />
      <form className="large-search" onSubmit={submit}><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入产品名称、参数、型号、品牌或制造商" /><button className="primary-button">{loading ? "查询中…" : "查询"}</button></form>
      <div className="history-results">{results.map((item) => <article key={item.id}><div><h3>{item.name}</h3><strong>¥ {money(item.price)}</strong></div><span>{item.model || "无型号"} · {item.brand || "无品牌"} · {item.manufacturer || "无制造商"} · {item.unit || "无单位"}</span><p>{item.spec || "无参数描述"}</p><footer>{item.quote_date || "日期未记录"}<b>{item.source}</b></footer></article>)}{query && !loading && !results.length && <div className="empty-state compact"><strong>没有找到相关历史报价</strong><span>可缩短关键词或改用产品名称。</span></div>}</div>
      {showEntry && <HistoryEntryModal onClose={() => setShowEntry(false)} onSaved={async () => { setShowEntry(false); notify("历史报价已录入。"); await runSearch(query); }} />}
    </section>
  );
}

function HistoryEntryModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [price, setPrice] = useState("");
  const [spec, setSpec] = useState("");
  const [unit, setUnit] = useState("");
  const [manufacturer, setManufacturer] = useState("");
  const [brand, setBrand] = useState("");
  const [model, setModel] = useState("");
  const [quoteDate, setQuoteDate] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const parsed = Number(price);
    if (!name.trim()) { setError("请填写产品名称。"); return; }
    if (!Number.isFinite(parsed) || parsed <= 0) { setError("请填写有效的价格（大于 0 的数字）。"); return; }
    setBusy(true);
    setError("");
    try {
      const payload: Record<string, string | number> = { name: name.trim(), price: parsed };
      if (spec.trim()) payload.spec = spec.trim();
      if (unit.trim()) payload.unit = unit.trim();
      if (manufacturer.trim()) payload.manufacturer = manufacturer.trim();
      if (brand.trim()) payload.brand = brand.trim();
      if (model.trim()) payload.model = model.trim();
      if (quoteDate) payload.quote_date = quoteDate;
      await api<HistoryRecord>("/api/history", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await onSaved();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "录入失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <button className="drawer-dismiss" aria-label="关闭手工录入" onClick={onClose} />
      <form className="modal-card" onSubmit={submit}>
        <header><h3>手工录入历史报价</h3><button type="button" onClick={onClose} aria-label="关闭">×</button></header>
        <label><span>产品名称 *</span><input value={name} onChange={(event) => setName(event.target.value)} required /></label>
        <label><span>价格（元）*</span><input type="number" min="0.01" step="0.01" value={price} onChange={(event) => setPrice(event.target.value)} required /></label>
        <div className="modal-grid">
          <label><span>参数</span><input value={spec} onChange={(event) => setSpec(event.target.value)} /></label>
          <label><span>单位</span><input value={unit} onChange={(event) => setUnit(event.target.value)} /></label>
          <label><span>制造商</span><input value={manufacturer} onChange={(event) => setManufacturer(event.target.value)} /></label>
          <label><span>品牌</span><input value={brand} onChange={(event) => setBrand(event.target.value)} /></label>
          <label><span>型号</span><input value={model} onChange={(event) => setModel(event.target.value)} /></label>
          <label><span>报价日期</span><input type="date" value={quoteDate} onChange={(event) => setQuoteDate(event.target.value)} /></label>
        </div>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy}>{busy ? "正在保存…" : "保存记录"}</button></div>
      </form>
    </div>
  );
}

type AssetTab = "databases" | "table" | "governance" | "audit";

// 「数据库」与「价目导入」已合并为一个 Tab：库的增删改与往库里灌价目本本来就是
// 同一件事的两半，拆成两个 Tab 只会让人来回切。切换/激活库的入口已彻底移除。
const ASSET_TABS: Array<{ key: AssetTab; title: string; detail: string; adminOnly?: boolean }> = [
  { key: "databases", title: "价目库", detail: "新建、重命名、删除、导入价目本" },
  { key: "table", title: "表格编辑", detail: "像表格一样增删改价目并保存回库" },
  { key: "governance", title: "质量治理", detail: "冲突、权重、去重" },
  { key: "audit", title: "审计事件", detail: "操作留痕", adminOnly: true },
];

function sortByRecent(entries: DatabaseEntry[]): DatabaseEntry[] {
  return [...entries].sort((left, right) => {
    const leftTime = left.last_used_at ? Date.parse(left.last_used_at) : 0;
    const rightTime = right.last_used_at ? Date.parse(right.last_used_at) : 0;
    if (leftTime !== rightTime) return rightTime - leftTime;
    if (left.use_count !== right.use_count) return right.use_count - left.use_count;
    return left.name.localeCompare(right.name, "zh-CN");
  });
}

function formatRelative(value: string): string {
  if (!value) return "尚未使用";
  const diff = Date.now() - Date.parse(value);
  if (!Number.isFinite(diff) || diff < 60000) return "刚刚";
  const minutes = Math.floor(diff / 60000);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function DataAssetsView({ data, user, onChanged, notify }: { data: Governance | null; user: User; onChanged: () => Promise<void>; notify: (message: string) => void }) {
  const isAdmin = user.role === "admin";
  const [tab, setTab] = useState<AssetTab>("databases");
  const [databases, setDatabases] = useState<DatabaseEntry[]>([]);
  const [trashed, setTrashed] = useState<DatabaseEntry[]>([]);
  // 系统默认库的键。它是个常量（改配置才能换），不是"当前选中的库"——
  // 界面上只把它当作各处未显式选库时的兜底默认值。
  const [systemKey, setSystemKey] = useState("");
  const [loading, setLoading] = useState(true);

  const loadDatabases = useCallback(async () => {
    const payload = await api<DatabaseList>("/api/databases");
    setDatabases(payload.databases);
    setTrashed(payload.trashed);
    setSystemKey(payload.system);
    setLoading(false);
    return payload;
  }, []);

  // 库列表由服务端持有，拉取列表本身就是这个 effect 的同步目的。
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void loadDatabases(); }, [loadDatabases]);

  async function reloadAll() {
    await loadDatabases();
    await onChanged();
  }

  const visibleTabs = ASSET_TABS.filter((item) => !item.adminOnly || isAdmin);

  return (
    <section>
      <PageHeading
        eyebrow="DATA ASSETS"
        title="数据资产中心"
        detail="统一管理价目库、导入价目本、查看质量治理指标与审计留痕。"
      />
      <div className="exception-tabs asset-tabs" aria-label="数据资产分区">
        {visibleTabs.map((item) => (
          <button key={item.key} className={tab === item.key ? "active" : ""} onClick={() => setTab(item.key)}>
            {item.title}
          </button>
        ))}
      </div>
      {tab === "databases" && (
        <DatabasePanel
          databases={databases}
          trashed={trashed}
          loading={loading}
          defaultKey={systemKey}
          isAdmin={isAdmin}
          onReload={reloadAll}
          notify={notify}
        />
      )}
      {tab === "table" && (
        <TableEditPanel databases={databases} defaultKey={systemKey} isAdmin={isAdmin} onReload={reloadAll} notify={notify} />
      )}
      {tab === "governance" && (
        <GovernancePanel databases={databases} defaultKey={systemKey} fallback={data} isAdmin={isAdmin} onReload={reloadAll} notify={notify} />
      )}
      {tab === "audit" && isAdmin && <AuditPanel />}
    </section>
  );
}

function DatabasePanel({ databases, trashed, loading, defaultKey, isAdmin, onReload, notify }: {
  databases: DatabaseEntry[];
  trashed: DatabaseEntry[];
  loading: boolean;
  defaultKey: string;
  isAdmin: boolean;
  onReload: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<DatabaseEntry | null>(null);
  const [renamingKey, setRenamingKey] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  // 导入价目本（原「价目导入」Tab 的职责，已并入本 Tab）。
  const [importKey, setImportKey] = useState("");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importResult, setImportResult] = useState("");
  const renameRef = useRef<HTMLInputElement>(null);

  // 进入重命名态后把焦点移到输入框（不用 autoFocus，避免可访问性告警）。
  useEffect(() => { if (renamingKey) renameRef.current?.focus(); }, [renamingKey]);

  const keyword = search.trim().toLowerCase();
  const visible = sortByRecent(
    databases.filter((db) => !keyword
      || db.name.toLowerCase().includes(keyword)
      || db.key.toLowerCase().includes(keyword)
      || db.tags.some((tag) => tag.toLowerCase().includes(keyword))),
  );
  const importOptions = sortByRecent(databases);
  // 未选时落到系统默认库，避免在 effect 里同步改 state。
  const importTarget = importKey || defaultKey || importOptions[0]?.key || "";

  async function submitImport() {
    if (!importFile || !importTarget) return;
    setImportBusy(true);
    setImportResult("");
    try {
      const formData = new FormData();
      formData.set("file", importFile);
      const payload = await api<{ inserted: number; skipped_duplicates: number; skipped_invalid: number; database: string }>(
        `/api/databases/${encodeURIComponent(importTarget)}/import`,
        { method: "POST", body: formData },
      );
      const summary = `新增 ${payload.inserted} 条 / 重复跳过 ${payload.skipped_duplicates} 条 / 无效 ${payload.skipped_invalid} 条`;
      setImportResult(summary);
      notify(`价目本已导入「${payload.database}」：${summary}。`);
      setImportFile(null);
      await onReload();
    } catch (reason) {
      setImportResult(reason instanceof Error ? reason.message : "导入失败");
    } finally {
      setImportBusy(false);
    }
  }

  async function submitRename(entry: DatabaseEntry) {
    const value = renameDraft.trim();
    if (!value || value === entry.name) {
      setRenamingKey(null);
      return;
    }
    setBusy(true);
    try {
      await api(`/api/databases/${encodeURIComponent(entry.key)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: value }),
      });
      notify("数据库已重命名。");
      setRenamingKey(null);
      await onReload();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "重命名失败");
    } finally {
      setBusy(false);
    }
  }

  async function remove(entry: DatabaseEntry) {
    if (!window.confirm(`确定删除数据库「${entry.name}」吗？\n该库有 ${entry.history_count.toLocaleString("zh-CN")} 条价目、${entry.job_count} 个报价任务。\n删除前会自动备份并移入回收站，可随时恢复。`)) return;
    setBusy(true);
    try {
      const result = await api<{ bound_jobs: number }>(`/api/databases/${encodeURIComponent(entry.key)}`, { method: "DELETE" });
      notify(result.bound_jobs
        ? `已移入回收站；有 ${result.bound_jobs} 个报价任务仍引用该库，历史记录保留。`
        : "已移入回收站，可在下方恢复。");
      await onReload();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "删除失败");
    } finally {
      setBusy(false);
    }
  }

  async function restore(entry: DatabaseEntry) {
    setBusy(true);
    try {
      await api(`/api/databases/${encodeURIComponent(entry.key)}/restore`, { method: "POST" });
      notify(`已恢复「${entry.name}」。`);
      await onReload();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "恢复失败");
    } finally {
      setBusy(false);
    }
  }

  async function purge(entry: DatabaseEntry) {
    const typed = window.prompt(`彻底删除不可恢复。请输入库名「${entry.name}」确认：`);
    if (typed === null) return;
    setBusy(true);
    try {
      await api(`/api/databases/${encodeURIComponent(entry.key)}/purge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm_name: typed }),
      });
      notify("数据库已彻底删除。");
      await onReload();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "彻底删除失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {isAdmin && (
        <div className="asset-toolbar">
          <div className="row-search customer-search">
            <span>⌕</span>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="按库名、文件键或标签过滤" />
          </div>
          <button className="primary-button" onClick={() => { setEditing(null); setShowForm((value) => !value); }}>
            {showForm && !editing ? "取消" : "＋ 新建价目库"}
          </button>
        </div>
      )}
      {!isAdmin && (
        <div className="row-search customer-search">
          <span>⌕</span>
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="按库名或标签过滤" />
        </div>
      )}
      {(showForm || editing) && (
        <DatabaseForm
          key={editing ? `edit-${editing.key}` : "new"}
          initial={editing}
          onCancel={() => { setShowForm(false); setEditing(null); }}
          onReload={onReload}
          onSaved={async (message) => { setShowForm(false); setEditing(null); notify(message); await onReload(); }}
        />
      )}

      {isAdmin && importOptions.length > 0 && (
        <div className="form-card import-card">
          <div>
            <h3>导入价目本到已有库</h3>
            <p>给已经存在的库补传价目本。新建库时可以直接在「新建价目库」卡片里选文件，不必先建库再回来传。重复与无效行会自动跳过并计数，不影响库中已有记录。</p>
          </div>
          <label style={{ display: "block", marginBottom: 10 }}>
            <span className="field-label">导入到</span>
            <select value={importTarget} onChange={(event) => setImportKey(event.target.value)} style={{ width: "100%" }}>
              {importOptions.map((db) => (
                <option key={db.key} value={db.key}>
                  {db.name}（{db.history_count.toLocaleString("zh-CN")} 条{db.is_system ? " · 系统默认" : ""}）
                </option>
              ))}
            </select>
          </label>
          <label className="import-file">
            <input type="file" accept=".xlsx,.xlsm,.xls,.csv" onChange={(event) => { setImportFile(event.target.files?.[0] ?? null); setImportResult(""); }} />
            <span>{importFile ? importFile.name : "选择价目本 Excel"}</span>
          </label>
          <button className="primary-button" disabled={!importFile || !importTarget || importBusy} onClick={() => void submitImport()}>
            {importBusy ? "正在导入…" : "上传导入"}
          </button>
          {importResult && <span className="import-result">{importResult}</span>}
        </div>
      )}

      {loading ? (
        <div className="empty-state">正在加载数据库列表…</div>
      ) : visible.length === 0 ? (
        <div className="empty-state compact">
          <strong>{keyword ? "没有匹配的数据库" : "还没有数据库"}</strong>
          <span>{isAdmin ? "点击右上角「新建价目库」添加。" : "请联系管理员创建价目库。"}</span>
        </div>
      ) : (
        <div className="governance-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
          {visible.map((db) => (
            <article key={db.key} className="db-card" style={{ padding: 16, border: db.is_system ? "2px solid var(--teal)" : "1px solid var(--line)", borderRadius: 12, background: "white" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                <div style={{ minWidth: 0 }}>
                  {renamingKey === db.key ? (
                    <input
                      ref={renameRef}
                      value={renameDraft}
                      onChange={(event) => setRenameDraft(event.target.value)}
                      onKeyDown={(event) => { if (event.key === "Enter") void submitRename(db); if (event.key === "Escape") setRenamingKey(null); }}
                      style={{ width: "100%" }}
                    />
                  ) : (
                    <h3 style={{ margin: 0, wordBreak: "break-word" }}>
                      {db.name}
                      {db.is_system && <span style={{ color: "var(--teal)", fontSize: 10, marginLeft: 8 }}>● 系统默认</span>}
                    </h3>
                  )}
                  <small style={{ color: "var(--muted)", fontSize: 10 }}>{db.key}.db</small>
                </div>
                <strong style={{ color: "#994a10", whiteSpace: "nowrap" }}>{db.history_count.toLocaleString("zh-CN")} 条</strong>
              </div>
              <p style={{ margin: "8px 0 0", color: "var(--muted)", fontSize: 10 }}>
                {db.exists ? `${db.size_mb} MB · ${db.job_count} 个任务` : "库文件缺失"}
                {` · ${formatRelative(db.last_used_at)}`}
                {db.use_count ? ` · 累计使用 ${db.use_count} 次` : ""}
              </p>
              {db.note && <p style={{ margin: "6px 0 0", fontSize: 11 }}>{db.note}</p>}
              {db.tags.length > 0 && (
                <div className="asset-tags">{db.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
              )}
              {renamingKey === db.key ? (
                <div className="asset-card-actions">
                  <button className="card-action-button" disabled={busy} onClick={() => void submitRename(db)}>保存</button>
                  <button className="card-action-button" onClick={() => setRenamingKey(null)}>取消</button>
                </div>
              ) : (
                <div className="asset-card-actions">
                  {isAdmin && (
                    <>
                      <button className="card-action-button" onClick={() => { setRenamingKey(db.key); setRenameDraft(db.name); }}>重命名</button>
                      <button className="card-action-button" onClick={() => { setShowForm(false); setEditing(db); }}>编辑</button>
                      <button className="card-action-button danger" disabled={busy || db.is_system} title={db.is_system ? "系统默认库不能删除" : undefined} onClick={() => void remove(db)}>删除</button>
                    </>
                  )}
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      {isAdmin && trashed.length > 0 && (
        <div className="trash-block">
          <button className="text-button" onClick={() => setShowTrash((value) => !value)}>
            {showTrash ? "收起回收站" : `回收站（${trashed.length}）`}
          </button>
          {showTrash && (
            <div className="governance-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", marginTop: 10 }}>
              {trashed.map((db) => (
                <article key={db.key} className="db-card" style={{ padding: 14, border: "1px dashed var(--line)", borderRadius: 12, background: "#fafafa" }}>
                  <h3 style={{ margin: 0 }}>{db.name}</h3>
                  <small style={{ color: "var(--muted)", fontSize: 10 }}>{db.key}.db · {db.history_count.toLocaleString("zh-CN")} 条价目</small>
                  <div className="asset-card-actions">
                    <button className="card-action-button" disabled={busy} onClick={() => void restore(db)}>恢复</button>
                    <button className="card-action-button danger" disabled={busy} onClick={() => void purge(db)}>彻底删除</button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );
}

function DatabaseForm({ initial, onSaved, onCancel, onReload }: {
  initial: DatabaseEntry | null;
  onSaved: (message: string) => void | Promise<void>;
  onCancel: () => void;
  onReload: () => Promise<void>;
}) {
  const editing = initial !== null;
  const [name, setName] = useState(initial?.name ?? "");
  const [note, setNote] = useState(initial?.note ?? "");
  const [tags, setTags] = useState((initial?.tags ?? []).join(", "));
  // 价目本文件。留空就只建一个空库，与旧流程等价。
  const [file, setFile] = useState<File | null>(null);
  // 建库与导入是两次请求，但只在这一张卡片里完成。第一步成功后记住 key：
  // 万一第二步失败，用户可以只重试导入，不会把库建成两个。
  const [createdKey, setCreatedKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => { nameRef.current?.focus(); }, []);

  const locked = Boolean(createdKey);
  const primaryLabel = editing
    ? "保存修改"
    : createdKey
      ? (file ? "重试导入" : "完成")
      : (file ? "创建并导入" : "创建空库");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const trimmed = name.trim();
    if (!trimmed && !createdKey) {
      setError("请先输入数据库名称。");
      return;
    }
    setBusy(true);
    setError("");
    const meta = {
      name: trimmed,
      note: note.trim(),
      tags: tags.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
    };
    // 这一次提交里库是否刚被建出来。catch 里不能用 `createdKey` 这个 state 判断：
    // setCreatedKey() 在同一次 submit 里刚调用，闭包读到的还是旧值（""），
    // 于是"库已建好、导入失败"时会把错误说成纯粹的导入失败，用户根本不知道库已经建了。
    let createdNow = false;
    try {
      if (editing) {
        await api(`/api/databases/${encodeURIComponent(initial.key)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(meta),
        });
        await onSaved("数据库信息已更新。");
        return;
      }

      // 第一步：建库（已经建过就跳过，避免重试导入时撞重名）。
      let key = createdKey;
      let displayName = trimmed;
      if (!key) {
        const created = await api<{ database: { key: string; name: string } }>("/api/databases", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(meta),
        });
        key = created.database.key;
        displayName = created.database.name;
        createdNow = true;
        setCreatedKey(key);
        // 库这时已经存在了，先把列表刷新出来：即使紧接着导入失败，用户也能看到这个库。
        await onReload();
      }

      if (!file) {
        await onSaved(`数据库「${displayName}」已创建。`);
        return;
      }

      // 第二步：导入价目本。
      const formData = new FormData();
      formData.set("file", file);
      const payload = await api<{ inserted: number; skipped_duplicates: number; skipped_invalid: number }>(
        `/api/databases/${encodeURIComponent(key)}/import`,
        { method: "POST", body: formData },
      );
      const summary = `新增 ${payload.inserted} 条 / 重复跳过 ${payload.skipped_duplicates} 条 / 无效 ${payload.skipped_invalid} 条`;
      await onSaved(`数据库「${displayName}」已创建，价目本已导入：${summary}。`);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "保存失败";
      setError(createdNow || createdKey
        ? `数据库已创建，但价目导入失败：${message}。可点下方按钮重试导入，不用重新建库。`
        : message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-card db-create-card" onSubmit={submit}>
      <header className="db-create-head">
        <h3>{editing ? "编辑数据库信息" : "新建价目库"}</h3>
        <p>
          {editing
            ? "只改显示名、标签与备注。文件键不变，库文件与已绑定该库的报价任务都不受影响。"
            : "命名与上传在同一张卡片里完成：填好名称、选上价目本，一次提交就建库并导入。不选文件则只建一个空库，稍后再补传。"}
        </p>
      </header>

      <div className="db-create-grid">
        <label>
          <span>数据库名称</span>
          <input
            ref={nameRef}
            value={name}
            onChange={(event) => { setName(event.target.value); if (error) setError(""); }}
            placeholder="支持中文，如 赛特尔25年"
            disabled={locked}
            required
          />
        </label>
        <label>
          <span>标签（选填）</span>
          <input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="逗号分隔，如 普教, 高中" disabled={locked} />
        </label>
        <label className="db-create-wide">
          <span>备注 / 用途（选填）</span>
          <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="如：高中理化生主价目本" disabled={locked} />
        </label>
      </div>

      {!editing && (
        <label className={`import-file db-create-file${file ? " has-file" : ""}`}>
          <input
            type="file"
            accept=".xlsx,.xlsm,.xls,.csv"
            onChange={(event) => { setFile(event.target.files?.[0] ?? null); if (error) setError(""); }}
          />
          <span>{file ? `已选：${file.name}（约 ${Math.max(1, Math.round(file.size / 1024))} KB）` : "＋ 选择价目本 Excel（选填，.xlsx / .xlsm / .xls / .csv）"}</span>
        </label>
      )}

      <div className="form-actions">
        <button className="primary-button" type="submit" disabled={busy}>{busy ? "处理中…" : primaryLabel}</button>
        <button className="secondary-button" type="button" onClick={onCancel}>取消</button>
      </div>

      {!editing && (
        <small className="field-hint">
          一次提交＝先建库、再导入。文件名键由系统自动生成（ASCII），重命名只改显示名，不会动到库文件。
        </small>
      )}
      {error && <div className="form-error">{error}</div>}
    </form>
  );
}


function TableEditPanel({ databases, defaultKey, isAdmin, onReload, notify }: {
  databases: DatabaseEntry[];
  defaultKey: string;
  isAdmin: boolean;
  onReload: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [pickedKey, setPickedKey] = useState("");
  const [rows, setRows] = useState<HistoryRow[]>([]);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [maxBulkRows, setMaxBulkRows] = useState(2000);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(100);
  const [keyword, setKeyword] = useState("");
  const [sort, setSort] = useState("source");
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [edits, setEdits] = useState<Record<string, { revision: number; base: Record<string, string>; values: Record<string, string> }>>({});
  const [drafts, setDrafts] = useState<Array<{ tempId: string; values: Record<string, string> }>>([]);
  const [removed, setRemoved] = useState<Record<string, HistoryRow>>({});
  const [issues, setIssues] = useState<BulkEditRowResult[]>([]);
  const [summary, setSummary] = useState("");
  const [focusId, setFocusId] = useState("");
  const [opening, setOpening] = useState(false);
  const [applying, setApplying] = useState(false);
  const [preview, setPreview] = useState<OpenFilePreview | null>(null);
  // 需指定的行：seq → 用户选中的候选 id。
  const [picks, setPicks] = useState<Record<number, string>>({});
  // 用户主动排除掉、这次不写的行（seq）。
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  const [group, setGroup] = useState("change");
  const fileRef = useRef<HTMLInputElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const scrolledRef = useRef("");

  const options = sortByRecent(databases);
  const scopeKey = pickedKey || defaultKey;

  const load = useCallback(async (targetPage: number) => {
    if (!scopeKey) return null;
    setLoading(true);
    try {
      const params = new URLSearchParams({
        database_key: scopeKey,
        page: String(targetPage),
        page_size: String(pageSize),
        q: keyword.trim(),
        sort,
        order,
      });
      const payload = await api<HistoryRowsPage>(`/api/history/rows?${params}`);
      setRows(payload.rows);
      setTotal(payload.total);
      setPages(payload.pages);
      setMaxBulkRows(payload.max_bulk_rows);
      return payload;
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "加载价目行失败");
      setRows([]);
      setTotal(0);
      setPages(1);
      return null;
    } finally {
      setLoading(false);
    }
  }, [scopeKey, pageSize, keyword, sort, order, notify]);

  // 行数据由服务端持有，按当前筛选与页码拉取就是这个 effect 的同步目的。
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void load(page); }, [load, page]);

  // 跳到冲突行后滚动到它。只滚一次，避免每次翻页都重新滚动。
  useEffect(() => {
    if (!focusId || scrolledRef.current === focusId) return;
    const node = gridRef.current?.querySelector(`[data-row-id="${CSS.escape(focusId)}"]`);
    if (!node) return;
    scrolledRef.current = focusId;
    node.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focusId, rows]);

  const dirtyIds = Object.keys(edits);
  const removedIds = Object.keys(removed);
  const changeCount = dirtyIds.length + drafts.length + removedIds.length;
  const offPageChanges = dirtyIds.filter((id) => !rows.some((row) => row.id === id)).length;
  const visibleRows = rows.filter((row) => !removed[row.id]);

  // 本地先校验一遍，避免把必然被拒的行发出去——服务端仍会再校验一次。
  const localIssues: Array<{ key: string; label: string; message: string }> = [];
  function checkValues(key: string, label: string, values: Record<string, string>) {
    if (!values.name.trim()) localIssues.push({ key, label, message: "产品名称不能为空" });
    const price = values.price.trim();
    if (price === "") localIssues.push({ key, label, message: "单价不能为空" });
    else if (!Number.isFinite(Number(price)) || Number(price) < 0) {
      localIssues.push({ key, label, message: "单价必须是不小于 0 的数字" });
    }
    const quantity = values.quantity.trim();
    if (quantity !== "" && !Number.isFinite(Number(quantity))) {
      localIssues.push({ key, label, message: "数量必须是数字，或留空表示未知" });
    }
  }
  for (const id of dirtyIds) checkValues(id, edits[id].values.name || id, edits[id].values);
  drafts.forEach((draft, index) => checkValues(draft.tempId, `新增行 ${index + 1}`, draft.values));

  useEffect(() => {
    if (changeCount === 0) return;
    const handler = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [changeCount]);

  function valuesFor(row: HistoryRow): Record<string, string> {
    return edits[row.id]?.values ?? rowValues(row);
  }

  function setCell(row: HistoryRow, field: EditableField, value: string) {
    setEdits((prev) => {
      const entry = prev[row.id];
      const base = entry?.base ?? rowValues(row);
      const values = { ...(entry?.values ?? base), [field]: value };
      const next = { ...prev };
      if (EDIT_FIELDS.some((item) => values[item.key] !== base[item.key])) {
        next[row.id] = { revision: entry?.revision ?? row.revision, base, values };
      } else {
        // 改回原值就当没改过，别让"撤销"按钮无意义地亮着。
        delete next[row.id];
      }
      return next;
    });
  }

  function setDraftCell(tempId: string, field: EditableField, value: string) {
    setDrafts((prev) => prev.map((draft) => (
      draft.tempId === tempId ? { ...draft, values: { ...draft.values, [field]: value } } : draft
    )));
  }

  function addRow() {
    const values: Record<string, string> = {};
    for (const field of EDIT_FIELDS) values[field.key] = "";
    setDrafts((prev) => [...prev, { tempId: `draft-${Date.now()}-${prev.length}`, values }]);
  }

  function undoRow(id: string) {
    setEdits((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  function removeRow(row: HistoryRow) {
    undoRow(row.id);
    setRemoved((prev) => ({ ...prev, [row.id]: row }));
  }

  function undoRemove(id: string) {
    setRemoved((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  function discardAll() {
    if (!window.confirm(`确定放弃 ${changeCount} 处未保存的改动吗？`)) return;
    setEdits({});
    setDrafts([]);
    setRemoved({});
    setIssues([]);
    setSummary("");
  }

  function changeScope(key: string) {
    if (key === scopeKey) return;
    if (changeCount > 0 && !window.confirm(`改选其它库会放弃当前 ${changeCount} 处未保存的改动，确定继续吗？`)) return;
    setPickedKey(key);
    setEdits({});
    setDrafts([]);
    setRemoved({});
    setIssues([]);
    setSummary("");
    setFocusId("");
    setPage(1);
  }

  function resetView(next: () => void) {
    setFocusId("");
    next();
  }

  function toggleSort(field: string) {
    if (sort === field) setOrder((prev) => (prev === "asc" ? "desc" : "asc"));
    else {
      setSort(field);
      setOrder("asc");
    }
    resetView(() => setPage(1));
  }

  async function jumpTo(conflict: EditConflict) {
    if (!conflict.id) return;
    if (rows.some((row) => row.id === conflict.id)) {
      setFocusId(conflict.id);
      return;
    }
    // 冲突行不在当前页：用它自己的名称搜出来，再按 id 高亮。
    if (!conflict.name) {
      notify(`该行不在当前页（${conflict.location ?? "位置未知"}），请用搜索定位。`);
      return;
    }
    scrolledRef.current = "";
    setKeyword(conflict.name);
    setPage(1);
    setFocusId(conflict.id);
  }

  async function save() {
    if (localIssues.length > 0 || saving) return;
    setSaving(true);
    setSummary("");
    try {
      const payload = {
        database_key: scopeKey,
        created: drafts.map((draft) => ({ changes: toChanges(draft.values) })),
        updated: dirtyIds.map((id) => ({ id, revision: edits[id].revision, changes: toChanges(edits[id].values) })),
        deleted: removedIds.map((id) => ({ id, revision: removed[id].revision })),
      };
      const result = await api<BulkEditResponse>("/api/history/bulk-edit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const failed = result.results.filter((item) => item.status !== "ok");
      const okIds = new Set(result.results.filter((item) => item.status === "ok").map((item) => item.id));
      // 成功的行清掉草稿；被拒绝的留在原地，等用户改完再存一次。
      setEdits((prev) => {
        const next = { ...prev };
        for (const id of Object.keys(next)) if (okIds.has(id)) delete next[id];
        return next;
      });
      setRemoved((prev) => {
        const next = { ...prev };
        for (const id of Object.keys(next)) if (okIds.has(id)) delete next[id];
        return next;
      });
      // 新增行按提交顺序一一对应（服务端 results 里的 create 保持入参顺序）。
      const createResults = result.results.filter((item) => item.op === "create");
      const failedCreateIndexes = new Set(
        createResults.map((item, index) => (item.status === "ok" ? -1 : index)).filter((index) => index >= 0),
      );
      setDrafts((prev) => prev.filter((_, index) => failedCreateIndexes.has(index)));
      setIssues(failed);
      setSummary(
        `新增 ${result.applied.created} · 修改 ${result.applied.updated} · 删除 ${result.applied.deleted}`
        + ` · 无变化 ${result.unchanged} · 被拒绝 ${failed.length}`
        + (result.backup ? ` · 已自动备份 ${result.backup}` : ""),
      );
      const written = result.applied.created + result.applied.updated + result.applied.deleted;
      notify(failed.length ? `已写入 ${written} 处改动，${failed.length} 行被拒绝，请在问题列表里处理。` : `已写入 ${written} 处改动。`);
      const reloaded = await load(page);
      if (reloaded && reloaded.rows.length === 0 && reloaded.total > 0 && page > 1) setPage(page - 1);
      await onReload();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function exportCsv() {
    if (!scopeKey || exporting) return;
    setExporting(true);
    try {
      const cap = 20000;
      const collected: HistoryRow[] = [];
      let current = 1;
      for (;;) {
        const params = new URLSearchParams({
          database_key: scopeKey,
          page: String(current),
          page_size: "500",
          q: keyword.trim(),
          sort,
          order,
        });
        const payload = await api<HistoryRowsPage>(`/api/history/rows?${params}`);
        collected.push(...payload.rows);
        if (current >= payload.pages || collected.length >= cap) break;
        current += 1;
      }
      const scoped = collected.slice(0, cap);
      const lines = [[...EDIT_FIELDS.map((field) => field.label), "来源"].map(csvCell).join(",")];
      for (const row of scoped) {
        if (removed[row.id]) continue;
        const values = valuesFor(row);
        lines.push([...EDIT_FIELDS.map((field) => values[field.key] ?? ""), rowSource(row)].map(csvCell).join(","));
      }
      // 还没保存的新增行也要一起导出，否则用户会以为改动丢了。
      for (const draft of drafts) {
        lines.push([...EDIT_FIELDS.map((field) => draft.values[field.key] ?? ""), "表格编辑（未保存）"].map(csvCell).join(","));
      }
      const label = options.find((item) => item.key === scopeKey)?.name ?? scopeKey;
      const blob = new Blob([`\ufeff${lines.join("\r\n")}`], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${label}_价目_${new Date().toISOString().slice(0, 10)}.csv`;
      anchor.click();
      URL.revokeObjectURL(url);
      const truncated = collected.length >= cap;
      notify(`已导出 ${scoped.length} 行（含未保存改动）${truncated ? `，超过 ${cap} 行上限已截断` : ""}。`);
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "导出失败");
    } finally {
      setExporting(false);
    }
  }

  // ---- 打开本地价目本 ------------------------------------------------------
  // 分两步：① 打开时只解析比对、不写库，把"会发生什么"逐行摆出来；② 用户确认
  // 后才把"会改动"的行转成变更集，走和网格保存同一条 bulk-edit 通道——校验、
  // 乐观锁、去重、备份、审计都在那边，这里不另开第二条写入路径。

  async function openWorkbook(file: File) {
    if (!scopeKey || opening) return;
    setOpening(true);
    try {
      const form = new FormData();
      form.append("database_key", scopeKey);
      form.append("file", file);
      const payload = await api<OpenFilePreview>("/api/history/open-file", { method: "POST", body: form });
      setPreview(payload);
      setPicks({});
      setExcluded(new Set());
      const changes = payload.summary.update + payload.summary.create + payload.summary.ambiguous;
      setGroup(changes > 0 ? "change" : payload.summary.unchanged > 0 ? "unchanged" : "skipped");
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "打开价目本失败");
    } finally {
      setOpening(false);
      // 清空 input，否则同一个文件改完再选一次不会触发 onChange。
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function closePreview() {
    setPreview(null);
    setPicks({});
    setExcluded(new Set());
  }

  async function applyPreview() {
    if (!preview || applying) return;
    const { created, updated, unpicked, writable } = planFromPreview(preview, excluded, picks);
    if (writable === 0) {
      notify(unpicked
        ? `还有 ${unpicked} 行没指定要更新哪一条，这次没有可写入的改动。`
        : excluded.size > 0 ? "所有行都被排除了，这次没有可写入的改动。" : "这次没有需要写入的改动。");
      return;
    }
    if (unpicked > 0 && !window.confirm(`还有 ${unpicked} 行「需指定」没选，这些行会被跳过。确定继续吗？`)) return;
    if (writable > preview.max_bulk_rows) {
      notify(`本次要提交 ${writable} 行，超过单次上限 ${preview.max_bulk_rows.toLocaleString("zh-CN")} 行，请先拆分文件。`);
      return;
    }
    // 网格里的未保存改动走的是另一次提交，不会跟着一起写；先说清楚。
    if (changeCount > 0 && !window.confirm(`网格里还有 ${changeCount} 处未保存的改动，它们不会被这次应用一起提交。确定继续吗？`)) return;

    setApplying(true);
    try {
      const result = await api<BulkEditResponse>("/api/history/bulk-edit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          database_key: preview.database_key,
          created,
          updated,
          deleted: [],
        }),
      });
      const failed = result.results.filter((item) => item.status !== "ok");
      const written = result.applied.created + result.applied.updated;
      setIssues(failed);
      setSummary(
        `打开文件并应用：新增 ${result.applied.created} · 修改 ${result.applied.updated}`
        + ` · 无变化 ${result.unchanged} · 被拒绝 ${failed.length}`
        + (result.backup ? ` · 已自动备份 ${result.backup}` : ""),
      );
      notify(failed.length
        ? `已写入 ${written} 行，${failed.length} 行被拒绝，请在问题列表里处理。`
        : `已写入 ${written} 行。`);
      closePreview();
      const reloaded = await load(page);
      if (reloaded && reloaded.rows.length === 0 && reloaded.total > 0 && page > 1) setPage(page - 1);
      await onReload();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "应用失败");
    } finally {
      setApplying(false);
    }
  }

  const previewGroup = OPEN_FILE_GROUPS.find((item) => item.key === group) ?? OPEN_FILE_GROUPS[0];
  const previewRows = preview
    ? preview.rows.filter((row) => previewGroup.actions.includes(row.action))
    : [];
  const previewChanges = preview
    ? preview.summary.create + preview.summary.update + preview.summary.ambiguous
    : 0;
  const plan = preview ? planFromPreview(preview, excluded, picks) : null;
  // 「将改动」组的全部行：全选/全不选按这个集合来，跟当前在哪个分组无关。
  const changeSeqs = preview
    ? preview.rows
      .filter((row) => OPEN_FILE_GROUPS[0].actions.includes(row.action))
      .map((row) => row.seq)
    : [];
  const excludedCount = changeSeqs.filter((seq) => excluded.has(seq)).length;
  const unpicked = plan?.unpicked ?? 0;

  function toggleExcluded(seq: number, checked: boolean) {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (checked) next.delete(seq);
      else next.add(seq);
      return next;
    });
  }

  return (
    <>
      <div className="asset-toolbar">
        <label className="asset-scope">
          <span>编辑库</span>
          <select value={scopeKey} onChange={(event) => changeScope(event.target.value)}>
            {options.map((db) => (
              <option key={db.key} value={db.key}>
                {db.name}{db.is_system ? "（系统默认）" : ""} · {db.history_count.toLocaleString("zh-CN")} 条
              </option>
            ))}
          </select>
        </label>
        <div className="row-search">
          <input
            value={keyword}
            placeholder="按名称 / 参数 / 型号 / 品牌 / 制造商 / 编码 / 来源搜索"
            onChange={(event) => { setKeyword(event.target.value); resetView(() => setPage(1)); }}
          />
          {keyword && <button type="button" onClick={() => { setKeyword(""); resetView(() => setPage(1)); }}>清除</button>}
        </div>
        <div className="edit-actions">
          {isAdmin && <button className="secondary-button" onClick={addRow}>新增一行</button>}
          {isAdmin && (
            <>
              <input
                ref={fileRef}
                type="file"
                accept=".xlsx,.xls,.xlsm,.csv"
                className="edit-file-input"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void openWorkbook(file);
                }}
              />
              <button
                className="secondary-button"
                disabled={opening || !scopeKey}
                onClick={() => fileRef.current?.click()}
              >
                {opening ? "正在解析…" : "打开本地 Excel"}
              </button>
            </>
          )}
          <button className="secondary-button" disabled={exporting || !scopeKey} onClick={() => void exportCsv()}>
            {exporting ? "正在导出…" : "导出 CSV"}
          </button>
          {isAdmin && (
            <button className="secondary-button" disabled={!changeCount} onClick={discardAll}>放弃改动</button>
          )}
          {isAdmin && (
            <button
              className="primary-button"
              disabled={!changeCount || saving || localIssues.length > 0}
              onClick={() => void save()}
            >
              {saving ? "正在保存…" : `保存到数据库${changeCount ? `（${changeCount}）` : ""}`}
            </button>
          )}
        </div>
      </div>

      <div className="edit-hint">
        <span>直接在单元格里改；改动过的行会标黄，「保存到数据库」只提交改动过的行（新增 / 修改 / 删除），不做整表覆盖，因此不会影响别人并发导入的数据。保存前会自动备份整个库文件。</span>
        <span>也可以「打开本地 Excel」把一份价目本带进来：先逐行比对出「会改动 / 无需改动 / 不载入」，确认后才写库；比对时只按文件里确实有的列匹配，没带的列一律保留库里的原值。</span>
        <span>单价是不含税基准价：价目本里的「含税单价」在导入时已按固定税率折算，导出报价时会再乘回税率。</span>
        {!isAdmin && <span>当前账号为只读，修改价目需要管理员权限。</span>}
      </div>

      {summary && <div className="edit-banner">{summary}</div>}

      {removedIds.length > 0 && (
        <div className="edit-banner edit-banner-removed">
          <span>已标记删除 {removedIds.length} 行（保存后才会真正从库里删除）</span>
          <div className="edit-banner-actions">
            {removedIds.slice(0, 5).map((id) => (
              <button key={id} type="button" className="text-button" onClick={() => undoRemove(id)}>
                撤销「{removed[id].name || id}」
              </button>
            ))}
            {removedIds.length > 5 && <span>等 {removedIds.length} 行</span>}
          </div>
        </div>
      )}

      {localIssues.length > 0 && (
        <div className="edit-issues">
          <div className="edit-issues-head">
            <strong>还有 {localIssues.length} 处需要修正才能保存</strong>
          </div>
          <ul>
            {localIssues.map((issue, index) => (
              <li key={`${issue.key}-${index}`}>
                <span className="edit-issue-tag bad">校验</span>
                <span>{issue.label}：{issue.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {issues.length > 0 && (
        <div className="edit-issues">
          <div className="edit-issues-head">
            <strong>有 {issues.length} 行未写入数据库</strong>
            <button type="button" className="text-button" onClick={() => setIssues([])}>关闭</button>
          </div>
          <ul>
            {issues.map((issue, index) => (
              <li key={`${issue.op}-${issue.id ?? index}`}>
                <span className={`edit-issue-tag ${issue.reason === "duplicate_key" ? "warn" : "bad"}`}>
                  {issue.op === "create" ? "新增" : issue.op === "update" ? "修改" : "删除"}
                </span>
                <span>{issue.message ?? "被服务端拒绝"}</span>
                {issue.conflict?.location && <small>位置：{issue.conflict.location}</small>}
                {issue.conflict?.id && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => { if (issue.conflict) void jumpTo(issue.conflict); }}
                  >
                    跳到该行
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="edit-grid-wrap">
        <div className={`table-scroll edit-grid-scroll${loading ? " loading" : ""}`} ref={gridRef}>
          <table className="quote-table edit-grid">
            <thead>
              <tr>
                <th className="edit-index-col">#</th>
                {EDIT_FIELDS.map((field) => (
                  <th key={field.key} style={{ minWidth: field.width }}>
                    <button type="button" className="edit-sort" title="点击切换排序" onClick={() => toggleSort(field.key)}>
                      {field.label}{sort === field.key ? (order === "asc" ? " ↑" : " ↓") : ""}
                    </button>
                  </th>
                ))}
                <th style={{ minWidth: 180 }}>来源 / 版本</th>
                {isAdmin && <th style={{ minWidth: 96 }}>操作</th>}
              </tr>
            </thead>
            <tbody>
              {drafts.map((draft, index) => (
                <tr key={draft.tempId} className="row-new">
                  <td className="edit-index-col">新{index + 1}</td>
                  {EDIT_FIELDS.map((field) => (
                    <td key={field.key}>
                      <input
                        className="edit-input"
                        value={draft.values[field.key] ?? ""}
                        inputMode={field.kind === "number" ? "decimal" : undefined}
                        onChange={(event) => setDraftCell(draft.tempId, field.key, event.target.value)}
                      />
                    </td>
                  ))}
                  <td><small>尚未保存 · 保存后来源记为「表格编辑」</small></td>
                  {isAdmin && (
                    <td>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => setDrafts((prev) => prev.filter((item) => item.tempId !== draft.tempId))}
                      >
                        移除
                      </button>
                    </td>
                  )}
                </tr>
              ))}
              {visibleRows.map((row, index) => {
                const values = valuesFor(row);
                const className = [edits[row.id] ? "row-dirty" : "", focusId === row.id ? "row-focus" : ""]
                  .filter(Boolean).join(" ");
                return (
                  <tr key={row.id} data-row-id={row.id} className={className}>
                    <td className="edit-index-col">{(page - 1) * pageSize + index + 1}</td>
                    {EDIT_FIELDS.map((field) => (
                      <td key={field.key}>
                        {isAdmin ? (
                          <input
                            className="edit-input"
                            value={values[field.key] ?? ""}
                            inputMode={field.kind === "number" ? "decimal" : undefined}
                            onChange={(event) => setCell(row, field.key, event.target.value)}
                          />
                        ) : (
                          <span>{values[field.key]}</span>
                        )}
                      </td>
                    ))}
                    <td>
                      <small>{rowSource(row)}</small>
                      <small>版本 {row.revision} · 质量 {(row.data_quality * 100).toFixed(0)}%</small>
                    </td>
                    {isAdmin && (
                      <td>
                        {edits[row.id] && (
                          <button type="button" className="text-button" onClick={() => undoRow(row.id)}>撤销</button>
                        )}
                        <button type="button" className="text-button danger" onClick={() => removeRow(row)}>删除</button>
                      </td>
                    )}
                  </tr>
                );
              })}
              {visibleRows.length === 0 && drafts.length === 0 && (
                <tr>
                  <td colSpan={EDIT_FIELDS.length + (isAdmin ? 3 : 2)}>
                    <div className="edit-empty">
                      {loading
                        ? "正在加载价目行…"
                        : keyword
                          ? "当前筛选没有匹配的价目行。"
                          : "该库还没有价目数据。可先到「价目库」上传价目本，或点「新增一行」手工录入。"}
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="edit-pager">
        <span>
          共 {total.toLocaleString("zh-CN")} 行 · 第 {page} / {pages} 页
          {offPageChanges > 0 && ` · 另有 ${offPageChanges} 行改动不在本页`}
          {` · 单次最多提交 ${maxBulkRows.toLocaleString("zh-CN")} 行`}
        </span>
        <div>
          <label className="edit-page-size">
            <span>每页</span>
            <select
              value={pageSize}
              onChange={(event) => { setPageSize(Number(event.target.value)); resetView(() => setPage(1)); }}
            >
              {ROWS_PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
            </select>
          </label>
          <button className="secondary-button" disabled={page <= 1 || loading} onClick={() => resetView(() => setPage(1))}>首页</button>
          <button className="secondary-button" disabled={page <= 1 || loading} onClick={() => resetView(() => setPage(page - 1))}>上一页</button>
          <button className="secondary-button" disabled={page >= pages || loading} onClick={() => resetView(() => setPage(page + 1))}>下一页</button>
          <button className="secondary-button" disabled={page >= pages || loading} onClick={() => resetView(() => setPage(pages))}>末页</button>
        </div>
      </div>

      {preview && (
        <div className="modal-backdrop">
          <div className="openfile-card" role="dialog" aria-label="打开本地 Excel 预览">
            <header className="openfile-head">
              <div>
                <h3>打开「{preview.source}」</h3>
                <p>
                  共 {preview.total.toLocaleString("zh-CN")} 行 · 会改动 {previewChanges.toLocaleString("zh-CN")} 行
                  {plan && plan.writable !== previewChanges
                    ? ` · 将写入 ${plan.writable.toLocaleString("zh-CN")} 行`
                    : ""}
                  {` · 目标库：${options.find((item) => item.key === preview.database_key)?.name ?? preview.database_key}`}
                </p>
              </div>
              <button type="button" onClick={closePreview} disabled={applying} aria-label="关闭">×</button>
            </header>

            <div className="openfile-notice">
              这一步只是比对，<strong>没有改动数据库</strong>。确认无误后再点「应用并保存」。
            </div>

            <div className="openfile-groups">
              {OPEN_FILE_GROUPS.map((item) => {
                const count = item.actions.reduce((sum, action) => sum + preview.summary[action], 0);
                return (
                  <button
                    key={item.key}
                    type="button"
                    className={`openfile-group${group === item.key ? " active" : ""}`}
                    onClick={() => setGroup(item.key)}
                  >
                    {item.title}
                    <b>{count.toLocaleString("zh-CN")}</b>
                  </button>
                );
              })}
            </div>

            <div className="openfile-list">
              {previewGroup.key === "change" && previewRows.length > 0 && (
                <div className="openfile-selectbar">
                  <span>勾掉不想写的行，剩下的才会提交。</span>
                  <button type="button" className="text-button" onClick={() => setExcluded(new Set())}>全选</button>
                  <button type="button" className="text-button" onClick={() => setExcluded(new Set(changeSeqs))}>全不选</button>
                  {excludedCount > 0 && <b>已排除 {excludedCount} 行</b>}
                </div>
              )}
              {previewRows.length === 0 && (
                <div className="edit-empty">这一组没有行。</div>
              )}
              {previewRows.map((row) => {
                const diff = changeSummary(row.current, row.values);
                const selectable = previewGroup.key === "change";
                const dropped = selectable && excluded.has(row.seq);
                return (
                  <div key={row.seq} className={`openfile-row is-${row.action}${dropped ? " is-dropped" : ""}`}>
                    <div className="openfile-row-head">
                      {selectable && (
                        <input
                          type="checkbox"
                          className="openfile-pick"
                          checked={!dropped}
                          onChange={(event) => toggleExcluded(row.seq, event.target.checked)}
                          aria-label={`${dropped ? "恢复" : "排除"}第 ${row.seq} 行`}
                        />
                      )}
                      <span className={`edit-issue-tag ${row.action === "create" ? "good" : row.action === "update" ? "warn" : "bad"}`}>
                        {OPEN_FILE_ACTION_LABEL[row.action]}
                      </span>
                      <strong>{row.values?.name || row.current?.name || `第 ${row.seq} 行`}</strong>
                      <small>{row.source}</small>
                      <small className="openfile-match">按 {row.match_labels.join("/")} 匹配</small>
                      {dropped && <small className="openfile-dropped">已排除，这次不写</small>}
                    </div>
                    <div className="openfile-row-body">
                      <span className="openfile-reason">{row.reason}</span>
                      {diff.length > 0 && (
                        <>
                          {row.action === "ambiguous" && (
                            <small className="openfile-difflabel">文件里这一行是（选定候选后按这个改）：</small>
                          )}
                          <ul className="openfile-diff">
                            {diff.map((line, index) => <li key={index}>{line}</li>)}
                          </ul>
                        </>
                      )}
                      {row.action === "ambiguous" && (
                        <div className="openfile-candidates">
                          <span>库里有多条，选一条作为更新目标：</span>
                          {row.targets.map((target) => (
                            <label key={target.id} className={`openfile-candidate${picks[row.seq] === target.id ? " picked" : ""}`}>
                              <input
                                type="radio"
                                name={`pick-${row.seq}`}
                                checked={picks[row.seq] === target.id}
                                onChange={() => setPicks((prev) => ({ ...prev, [row.seq]: target.id }))}
                              />
                              <b>{target.price ?? "—"}</b>
                              <small>{target.location} · 版本 {target.revision}</small>
                              <small>{changeSummary(target.current, target.values).join("；") || "无差异"}</small>
                            </label>
                          ))}
                        </div>
                      )}
                      {row.warnings.length > 0 && (
                        <ul className="openfile-warnings">
                          {row.warnings.map((text, index) => <li key={index}>{text}</li>)}
                        </ul>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            <footer className="openfile-foot">
              <span>
                {[
                  excludedCount > 0 ? `已排除 ${excludedCount} 行。` : "",
                  unpicked > 0 ? `还有 ${unpicked} 行「需指定」没选，未选的行不会写入。` : "",
                  "「无需改动」和「不载入」的行不会写入。",
                ].filter(Boolean).join("")}
              </span>
              <div>
                <button className="secondary-button" onClick={closePreview} disabled={applying}>取消</button>
                <button
                  className="primary-button"
                  onClick={() => void applyPreview()}
                  disabled={applying || !plan || plan.writable === 0}
                >
                  {applying ? "正在写入…" : `应用并保存${plan?.writable ? `（${plan.writable}）` : ""}`}
                </button>
              </div>
            </footer>
          </div>
        </div>
      )}
    </>
  );
}

function GovernancePanel({ databases, defaultKey, fallback, isAdmin, onReload, notify }: {
  databases: DatabaseEntry[];
  defaultKey: string;
  fallback: Governance | null;
  isAdmin: boolean;
  onReload: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [pickedKey, setPickedKey] = useState("");
  const [data, setData] = useState<Governance | null>(fallback);
  const [dedupBusy, setDedupBusy] = useState(false);
  const options = sortByRecent(databases);
  const scopeKey = pickedKey || defaultKey;

  const loadSummary = useCallback(async (key: string) => {
    const query = key ? `?database_key=${encodeURIComponent(key)}` : "";
    setData(await api<Governance>(`/api/governance/summary${query}`));
  }, []);

  // 治理摘要由服务端持有，拉取所选库的摘要就是这个 effect 的同步目的。
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { if (scopeKey) void loadSummary(scopeKey); }, [loadSummary, scopeKey]);

  async function dedup() {
    setDedupBusy(true);
    try {
      const result = await api<{ removed: number }>("/api/governance/dedup-history", { method: "POST" });
      notify(`本次合并 ${result.removed} 条重复记录。`);
      await Promise.all([loadSummary(scopeKey), onReload()]);
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "去重失败");
    } finally {
      setDedupBusy(false);
    }
  }

  if (!data) return <section><div className="empty-state">正在加载治理摘要…</div></section>;

  return (
    <>
      <div className="asset-toolbar">
        <label className="asset-scope">
          <span>查看库</span>
          <select value={scopeKey} onChange={(event) => setPickedKey(event.target.value)}>
            {options.map((db) => (
              <option key={db.key} value={db.key}>{db.name}{db.is_system ? "（系统默认）" : ""}</option>
            ))}
          </select>
        </label>
        {isAdmin && (
          <button className="primary-button" disabled={dedupBusy} onClick={() => void dedup()}>
            {dedupBusy ? "正在去重…" : "一键去重历史报价"}
          </button>
        )}
      </div>
      <div className="governance-grid">
        <Metric label="历史记录" value={data.record_count} tone="teal" />
        <Metric label="同名重复组" value={data.duplicate_name_groups} tone="slate" />
        <Metric label="同名多单位组" value={data.unit_conflict_groups} tone="red" />
        <Metric label="同名多价格组" value={data.price_conflict_groups} tone="amber" />
        <Metric label="缺制造商记录" value={data.missing_manufacturer_count} tone="blue" />
        <Metric label="审计事件" value={data.audit_event_count} tone="slate" />
      </div>
      <div className="governance-panels">
        <article>
          <p className="eyebrow">MATCHING POLICY</p>
          <h3>当前匹配权重</h3>
          <div className="weight-list">
            {Object.entries(data.matching_weights).map(([label, value]) => (
              <div key={label}><span>{label}</span><i><b style={{ width: `${value * 2}%` }} /></i><strong>{value}%</strong></div>
            ))}
          </div>
          <p>编码精确命中优先；低于55分不作为可靠匹配；阻断风险不能被批量确认。</p>
        </article>
        <article>
          <p className="eyebrow">UNIT POLICY</p>
          <h3>单位校验边界</h3>
          <ul>
            <li><b>绿色</b>单位完全一致</li>
            <li><b>橙色</b>克/千克、毫升/升等可换算</li>
            <li><b>红色</b>瓶/盒/套与克等不可直接换算</li>
          </ul>
          <p>包装单位只有在记录净含量后才能折算，避免错误单价进入客户报价。</p>
        </article>
      </div>
    </>
  );
}

function AuditPanel() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api<AuditEvent[]>("/api/audit-events?limit=100")
      .then(setEvents)
      .catch(() => setEvents([]))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">正在加载审计事件…</div>;
  if (events.length === 0) return <div className="empty-state compact"><strong>暂无审计事件</strong><span>导入价目、去重、切库等操作都会留下记录。</span></div>;

  return (
    <div className="table-scroll">
      <table className="quote-table">
        <thead>
          <tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>详情</th></tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.id}>
              <td className="mono">{formatDate(event.created_at)}</td>
              <td>{event.user?.display_name ?? "—"}</td>
              <td className="mono">{event.action}</td>
              <td>{event.entity_type} · {event.entity_id}</td>
              <td><small>{JSON.stringify(event.detail)}</small></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ChangePasswordModal({ onClose, notify }: { onClose: () => void; notify: (message: string) => void }) {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (newPassword.length < 6) { setError("新密码至少 6 位。"); return; }
    if (newPassword !== confirmPassword) { setError("两次输入的新密码不一致。"); return; }
    setBusy(true);
    setError("");
    try {
      await api<{ ok: boolean }>("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      });
      notify("密码已修改，下次登录请使用新密码。");
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "修改失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <button className="drawer-dismiss" aria-label="关闭修改密码" onClick={onClose} />
      <form className="modal-card" onSubmit={submit}>
        <header><h3>修改密码</h3><button type="button" onClick={onClose} aria-label="关闭">×</button></header>
        <label><span>旧密码</span><input type="password" value={oldPassword} onChange={(event) => setOldPassword(event.target.value)} autoComplete="current-password" required /></label>
        <label><span>新密码（至少 6 位）</span><input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" required /></label>
        <label><span>确认新密码</span><input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" required /></label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy}>{busy ? "正在提交…" : "确认修改"}</button></div>
      </form>
    </div>
  );
}

function DatabasePicker({ value, onChange, label = "选择价目库" }: {
  value: string;
  onChange: (key: string, name: string) => void;
  label?: string;
}) {
  const [entries, setEntries] = useState<DatabaseEntry[]>([]);
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [systemKey, setSystemKey] = useState("");
  const [searched, setSearched] = useState<{ query: string; items: DatabaseEntry[] }>({ query: "", items: [] });

  useEffect(() => {
    const query = expanded ? "scope=all&limit=50" : "scope=recent&limit=5";
    api<DatabaseSearchResult>(`/api/databases/search?${query}`)
      .then((payload) => { setEntries(payload.databases); setSystemKey(payload.system); })
      .catch(() => setEntries([]))
      .finally(() => setLoading(false));
  }, [expanded]);

  const keyword = search.trim();
  useEffect(() => {
    if (!keyword) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      api<DatabaseSearchResult>(`/api/databases/search?q=${encodeURIComponent(keyword)}&limit=20`)
        .then((payload) => { if (!cancelled) setSearched({ query: keyword, items: payload.databases }); })
        .catch(() => { if (!cancelled) setSearched({ query: keyword, items: [] }); });
    }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [keyword]);

  // 只采用与当前关键词对应的结果，避免旧结果串场。
  const matched = searched.query === keyword ? searched.items : [];
  const list = keyword ? matched : entries;
  const selected = [...entries, ...matched].find((item) => item.key === value) ?? null;
  const systemName = [...entries, ...matched].find((item) => item.key === systemKey)?.name ?? "";

  return (
    <>
      <div className="step-label"><b>02</b><span>{label}</span></div>
      <div className="row-search customer-picker-search">
        <span>⌕</span>
        <input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="输入库名搜索，留空显示最近常用"
        />
      </div>
      <div className="customer-picker-list db-picker-list">
        <button type="button" className={value ? "" : "active"} onClick={() => onChange("", "")}>
          <strong>不指定，用系统默认库</strong>
          <span>{systemName ? `系统默认库是「${systemName}」` : "系统默认库（改配置才能换，界面上不切换）"}</span>
        </button>
        {loading && <small className="picker-empty">正在加载数据库…</small>}
        {!loading && list.length === 0 && (
          <small className="picker-empty">{keyword ? "没有匹配的数据库" : "还没有可用的价目库"}</small>
        )}
        {list.map((db) => (
          <button type="button" key={db.key} className={value === db.key ? "active" : ""} onClick={() => onChange(db.key, db.name)}>
            <strong>{db.name}{db.is_system ? " · 系统默认" : ""}</strong>
            <span>{db.history_count.toLocaleString("zh-CN")} 条价目 · {db.exists ? formatRelative(db.last_used_at) : "库文件缺失"}</span>
          </button>
        ))}
      </div>
      {!keyword && !expanded && (
        <button type="button" className="text-button" onClick={() => setExpanded(true)}>查看全部数据库 →</button>
      )}
      {!keyword && expanded && (
        <button type="button" className="text-button" onClick={() => setExpanded(false)}>只看最近常用 ←</button>
      )}
      {selected && (
        <p className="picker-selected">
          本次报价使用「{selected.name}」，共 {selected.history_count.toLocaleString("zh-CN")} 条价目记录。
        </p>
      )}
      {!selected && !value && (
        <p className="picker-selected muted">未指定价目库时，使用系统默认库{systemName ? `「${systemName}」` : ""}。</p>
      )}
    </>
  );
}

function AccountAdmin({ notify }: { notify: (message: string) => void }) {
  const [accounts, setAccounts] = useState<AccountUser[]>([]);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("quote");
  const [error, setError] = useState("");
  const [resetTarget, setResetTarget] = useState<AccountUser | null>(null);

  const loadAccounts = useCallback(async () => {
    setAccounts(await api<AccountUser[]>("/api/users"));
  }, []);
  // Fetching the server-owned account list is the synchronization purpose of this effect.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void loadAccounts(); }, [loadAccounts]);

  async function createAccount(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api<AccountUser>("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password, display_name: displayName.trim(), role }),
      });
      setUsername("");
      setDisplayName("");
      setPassword("");
      setRole("quote");
      notify("账号已创建。");
      await loadAccounts();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建失败");
    }
  }

  async function toggleActive(account: AccountUser) {
    try {
      await api<AccountUser>(`/api/users/${account.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: !account.active }),
      });
      notify(account.active ? `账号「${account.username}」已停用。` : `账号「${account.username}」已启用。`);
      await loadAccounts();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "操作失败");
    }
  }

  return (
    <section>
      <PageHeading eyebrow="ACCOUNT ADMINISTRATION" title="账号管理" detail="创建报价账号、重置密码、停用或启用访问权限。" />
      <form className="inline-form account-form" onSubmit={createAccount}>
        <label><span>用户名</span><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="off" required /></label>
        <label><span>显示名</span><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} required /></label>
        <label><span>初始密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" required /></label>
        <label><span>角色</span><select value={role} onChange={(event) => setRole(event.target.value as Role)}><option value="quote">报价员</option><option value="admin">管理员</option></select></label>
        <button className="primary-button">＋ 新建账号</button>
        {error && <div className="form-error">{error}</div>}
      </form>
      <div className="review-card">
        <div className="table-scroll account-table">
          <table className="quote-table">
            <thead><tr><th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>
              {accounts.map((account) => (
                <tr key={account.id}>
                  <td className="mono">{account.username}</td>
                  <td>{account.display_name}</td>
                  <td>{account.role === "admin" ? "管理员" : "报价员"}</td>
                  <td><span className={`status-pill ${account.active ? "status-confirmed" : "status-failed"}`}>{account.active ? "启用中" : "已停用"}</span></td>
                  <td className="mono">{formatDate(account.created_at)}</td>
                  <td>
                    <div className="job-card-actions">
                      <button className="card-action-button" onClick={() => setResetTarget(account)}>重置密码</button>
                      <button className={`card-action-button ${account.active ? "danger" : ""}`} onClick={() => void toggleActive(account)}>{account.active ? "停用" : "启用"}</button>
                    </div>
                  </td>
                </tr>
              ))}
              {accounts.length === 0 && <tr><td colSpan={6}><div className="table-empty">暂无账号数据</div></td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      {resetTarget && <ResetPasswordModal account={resetTarget} onClose={() => setResetTarget(null)} notify={notify} />}
    </section>
  );
}

function ResetPasswordModal({ account, onClose, notify }: { account: AccountUser; onClose: () => void; notify: (message: string) => void }) {
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (newPassword.length < 6) { setError("新密码至少 6 位。"); return; }
    setBusy(true);
    setError("");
    try {
      await api<AccountUser>(`/api/users/${account.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: newPassword }),
      });
      notify(`账号「${account.username}」的密码已重置。`);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重置失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <button className="drawer-dismiss" aria-label="关闭重置密码" onClick={onClose} />
      <form className="modal-card" onSubmit={submit}>
        <header><h3>重置密码 · {account.display_name || account.username}</h3><button type="button" onClick={onClose} aria-label="关闭">×</button></header>
        <label><span>新密码（至少 6 位）</span><input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" required /></label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy}>{busy ? "正在提交…" : "确认重置"}</button></div>
      </form>
    </div>
  );
}
