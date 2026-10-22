/* Панель управления videofetch: Alpine.js + Chart.js, опрос API раз в 2 секунды. */

const STATUS_RU = {
  created: "создано", validating: "проверка", running: "выполняется", pausing: "пауза…", paused: "на паузе",
  stopping: "остановка…", stopped: "остановлено", completed: "завершено", failed: "ошибка",
  online: "онлайн", offline: "офлайн", degraded: "деградация", disabled: "отключён",
};
const ITEM_RU = {
  pending: "ожидает", checking: "проверка", not_found: "нет видео", found: "найдено", download_queued: "в очереди",
  downloading: "загрузка", completed: "скачано", skipped: "пропущено", forbidden: "доступ закрыт",
  rate_limited: "лимит (429)", retry_wait: "повтор", failed: "ошибка", cancelled: "отменено",
};

const ROLE_LEVEL = { viewer: 1, operator: 2, administrator: 3 };
let CSRF = "";                 // токен защиты от CSRF, выдаётся при входе
let onUnauthorized = () => {}; // вызывается, когда сессия закончилась

async function api(method, path, body) {
  const headers = body !== undefined ? { "Content-Type": "application/json" } : {};
  if (CSRF) headers["X-CSRF-Token"] = CSRF;
  const res = await fetch(path, {
    method, headers, credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* пустое тело */ }
  if (res.status === 401 && path !== "/api/auth/login") onUnauthorized();
  if (!res.ok) {
    const msg = data && (data.message || data.error) ? `${data.error || res.status}: ${data.message || ""}` : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

// Графики хранятся вне реактивного состояния Alpine: Proxy ломает внутренности Chart.js
const CHARTS = {};

function app() {
  return {
    tab: "jobs", online: true, toast: "", toastTimer: null,
    jobs: [], workers: [], job: null, chunks: [], items: { total: 0, items: [] }, files: { total: 0, files: [] }, events: [],
    itemFilter: "", itemOffset: 0, edit: { threads: "", rps: "" }, editJobId: null,
    form: { name: "", range: "[0,299]", chunk_size: 50, threads: "", rps: "", workers: [] },
    wform: { name: "", host: "", ssh_port: 22, ssh_user: "downloader", ssh_key_ref: "/run/secrets/worker-key", storage_path: "/data/videos", threads: 4 },
    metricWorker: null, tick: 0, lastErr: "",
    me: null, authChecked: false, login: { username: "", password: "", error: "", busy: false },
    users: [], audit: [], auditAction: "", uform: { username: "", password: "", role: "viewer" }, pollStarted: false,

    async init() {
      onUnauthorized = () => { this.me = null; CSRF = ""; };
      try {
        const me = await api("GET", "/api/auth/me");
        this.setMe(me);
      } catch (_) { this.me = null; }
      this.authChecked = true;
      if (this.me) await this.startPolling();
    },
    setMe(me) { this.me = me; CSRF = me.csrf_token; },
    async startPolling() {
      await this.refresh();
      if (!this.pollStarted) { this.pollStarted = true; setInterval(() => this.refresh(), 2000); }
    },
    can(role) { return !!this.me && Math.max(0, ...this.me.roles.map((r) => ROLE_LEVEL[r] || 0)) >= ROLE_LEVEL[role]; },
    async doLogin() {
      const l = this.login; l.error = ""; l.busy = true;
      try {
        this.setMe(await api("POST", "/api/auth/login", { username: l.username, password: l.password }));
        l.password = ""; this.tab = "jobs"; await this.startPolling();
      } catch (e) { l.error = e.message.replace(/^[a-z_]+: /, ""); }
      l.busy = false;
    },
    async doLogout() {
      try { await api("POST", "/api/auth/logout"); } catch (_) { /* сессия уже закончилась */ }
      this.me = null; CSRF = ""; this.job = null; this.jobs = []; this.workers = [];
    },
    async openUsers() {
      this.tab = "users";
      await this.guard(async () => { await this.loadUsers(); });
    },
    async loadUsers() {
      [this.users, this.audit] = await Promise.all([
        api("GET", "/api/users"), api("GET", "/api/audit?limit=200" + (this.auditAction ? `&action=${encodeURIComponent(this.auditAction)}` : "")),
      ]);
    },
    async createUser() {
      await this.guard(async () => {
        const f = this.uform;
        await api("POST", "/api/users", { username: f.username, password: f.password, roles: [f.role] });
        this.say(`Пользователь ${f.username} создан`);
        f.username = ""; f.password = "";
        await this.loadUsers();
      });
    },
    async setUserRole(u, role) {
      await this.guard(async () => { await api("PATCH", `/api/users/${u.id}`, { roles: [role] }); await this.loadUsers(); });
      await this.loadUsers().catch(() => {});
    },
    async toggleUser(u) {
      await this.guard(async () => { await api("PATCH", `/api/users/${u.id}`, { is_active: !u.is_active }); await this.loadUsers(); });
    },
    async resetPassword(u) {
      const pw = prompt(`Новый пароль для ${u.username} (не менее 10 символов, буквы обоих регистров и цифры):`);
      if (!pw) return;
      await this.guard(async () => { await api("PATCH", `/api/users/${u.id}`, { password: pw }); this.say("Пароль изменён"); });
    },
    async revokeToken(w) {
      if (!confirm(`Отозвать токен агента ${w.name}? Агент перестанет получать работу до следующего «Запустить».`)) return;
      await this.guard(async () => { await api("POST", `/api/workers/${w.id}/token/revoke`); this.say("Токен отозван"); });
    },

    // ---------- утилиты отображения
    statusRu(s) { return STATUS_RU[s] || s; },
    itemRu(s) { return ITEM_RU[s] || s; },
    rangeText(j) { return `[${j.range_from}, ${j.range_to ?? "∞"}]`; },
    fmtTime(t) { return t ? new Date(t).toLocaleString("ru-RU") : "—"; },
    fmtBytes(n) { const u = ["Б", "КБ", "МБ", "ГБ", "ТБ"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + " " + u[i]; },
    ago(t) { const s = Math.max(0, Math.round((Date.now() - new Date(t)) / 1000)); return s < 60 ? `${s} с назад` : `${Math.round(s / 60)} мин назад`; },
    workerName(id) { const w = this.workers.find((x) => x.id === id); return w ? w.name : id ? `#${id}` : "—"; },
    pct() { const p = this.job && this.job.stats.progress; return p == null ? 0 : Math.min(100, p * 100); },
    say(msg) { this.toast = msg; clearTimeout(this.toastTimer); this.toastTimer = setTimeout(() => (this.toast = ""), 4500); },

    async guard(fn) {
      try { return await fn(); } catch (e) { this.say(e.message); }
    },

    // ---------- загрузка данных
    async refresh() {
      if (!this.me) return;
      this.tick++;
      try {
        const [jobs, workers] = await Promise.all([api("GET", "/api/jobs"), api("GET", "/api/workers")]);
        this.jobs = jobs; this.workers = workers; this.online = true;
      } catch (_) { if (this.me) this.online = false; return; }  // «нет связи» — только если не отвечают основные запросы
      // ошибки остального показываем сообщением, а не ложной потерей связи
      if (this.job) await this.guardQuiet(() => this.refreshJob(), "задание");
      if (this.tab === "workers" && this.metricWorker && this.tick % 2 === 0) await this.guardQuiet(() => this.loadMetrics(), "метрики");
    },
    async guardQuiet(fn, what) {
      try { await fn(); this.lastErr = ""; } catch (e) {
        if (this.lastErr !== e.message) { this.lastErr = e.message; this.say(`Ошибка (${what}): ${e.message}`); console.error(what, e); }
      }
    },
    async openJob(id) {
      this.job = null; this.itemFilter = ""; this.itemOffset = 0; this.editJobId = null;
      this.job = await api("GET", `/api/jobs/${id}`);
      await this.refreshJob();
    },
    async refreshJob() {
      const id = this.job.id;
      const [job, chunks, events, files] = await Promise.all([
        api("GET", `/api/jobs/${id}`), api("GET", `/api/jobs/${id}/chunks?limit=200`), api("GET", `/api/jobs/${id}/events?limit=100`),
        api("GET", `/api/jobs/${id}/files?limit=100`),
      ]);
      this.job = job; this.chunks = chunks; this.events = events; this.files = files;
      if (this.editJobId !== id) {  // поля редактирования не затираем при каждом опросе
        this.edit = { threads: job.config?.threads ?? "", rps: job.global_rate_limit_rps ?? "" };
        this.editJobId = id;
      }
      await this.loadItems();
    },
    async loadItems() {
      const q = new URLSearchParams({ limit: 100, offset: this.itemOffset });
      if (this.itemFilter) q.set("status", this.itemFilter);
      this.items = await api("GET", `/api/jobs/${this.job.id}/items?${q}`);
    },
    setFilter(s) { this.itemFilter = s; this.itemOffset = 0; this.loadItems(); },

    // ---------- задания
    async createJob() {
      await this.guard(async () => {
        const f = this.form;
        const body = { name: f.name, range: f.range, workers: f.workers.map((id) => ({ worker_id: id })) };
        if (f.chunk_size) body.chunk_size = f.chunk_size;
        if (f.threads) body.threads = f.threads;
        if (f.rps) body.global_rate_limit_rps = f.rps;
        const job = await api("POST", "/api/jobs", body);
        this.say(`Задание #${job.id} создано`);
        await this.refresh();
        await this.openJob(job.id);
      });
    },
    async jobAction(action) {
      await this.guard(async () => { await api("POST", `/api/jobs/${this.job.id}/${action}`); await this.refresh(); });
    },
    confirmStop() {
      if (confirm("Остановить задание? Запустить его заново будет нельзя.")) this.jobAction("stop");
    },
    async patchJob() {
      await this.guard(async () => {
        const body = {};
        if (this.edit.threads) body.threads = this.edit.threads;
        if (this.edit.rps) body.global_rate_limit_rps = this.edit.rps;
        await api("PATCH", `/api/jobs/${this.job.id}`, body);
        this.say("Параметры применены, агенты подхватят их автоматически");
        await this.refreshJob();
      });
    },
    async setWorkerThreads(w, value) {
      await this.guard(async () => {
        await api("PUT", `/api/jobs/${this.job.id}/workers/${w.worker_id}`, { threads: Number(value) });
        await this.refreshJob();
      });
    },
    async toggleWorker(w) {
      await this.guard(async () => {
        await api("PUT", `/api/jobs/${this.job.id}/workers/${w.worker_id}`, { enabled: !w.enabled });
        await this.refreshJob();
      });
    },

    // ---------- серверы
    async createWorker() {
      await this.guard(async () => {
        const body = { ...this.wform };
        if (!body.ssh_key_ref) delete body.ssh_key_ref;
        await api("POST", "/api/workers", body);
        this.say(`Сервер ${body.name} добавлен`);
        this.wform.name = ""; this.wform.host = "";
        await this.refresh();
      });
    },
    async toggleEnabled(w) {
      await this.guard(async () => { await api("PATCH", `/api/workers/${w.id}`, { enabled: !w.enabled }); await this.refresh(); });
    },
    async agentOp(w, op) {
      await this.guard(async () => {
        this.say(`${w.name}: выполняется «${op}»…`);
        const r = await api("POST", `/api/workers/${w.id}/agent/${op}`);
        this.say(`${w.name}: ${op} — ${r.state || r.python || (r.deployed ? "развёрнуто" : "ок")}`);
        await this.refresh();
      });
    },

    // ---------- метрики
    async showMetrics(id) {
      this.metricWorker = id;
      await this.$nextTick();
      this.makeCharts();
      await this.loadMetrics();
    },
    makeCharts() {
      if (CHARTS.ids || typeof Chart === "undefined") return;
      const mk = (id, color) => new Chart(document.getElementById(id), {
        type: "line",
        data: { labels: [], datasets: [{ data: [], borderColor: color, backgroundColor: color + "33", fill: true, tension: .3, pointRadius: 0 }] },
        options: { animation: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } },
      });
      Object.assign(CHARTS, { ids: mk("chIds", "#2f6fed"), bps: mk("chBps", "#1e9e5a"), cpu: mk("chCpu", "#d98a00"), act: mk("chAct", "#9b59b6") });
    },
    async loadMetrics() {
      if (!CHARTS.ids) return;
      const rows = (await api("GET", `/api/workers/${this.metricWorker}/metrics?limit=120`)).reverse();
      const labels = rows.map((r) => new Date(r.collected_at).toLocaleTimeString("ru-RU"));
      const series = {
        ids: rows.map((r) => r.ids_per_sec ?? 0),
        bps: rows.map((r) => (r.download_bps ?? 0) / 1048576),
        cpu: rows.map((r) => r.cpu_percent ?? 0),
        act: rows.map((r) => (r.active_checks ?? 0) + (r.active_downloads ?? 0)),
      };
      for (const [k, chart] of Object.entries(CHARTS)) {
        chart.data.labels = labels; chart.data.datasets[0].data = series[k]; chart.update();
      }
    },
  };
}
