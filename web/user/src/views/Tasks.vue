<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api.js'
import ProxySelector from '../components/ProxySelector.vue'

// 组件存活标志：离开页面后中断「立即运行」的日志轮询循环
const alive = ref(true)
onBeforeUnmount(() => { alive.value = false })

const CODE_LOGIN = ['jd-code-login', 'eleme-code-login', 'mxbc-code-login', 'meituan-code-login']
const ACTION = [
  'maidong-scan', 'nongwu-tavern', 'yihetang-sign', 'hongse-huojian', 'luckin-draw',
  'yihetang-lottery', 'hsay-sign',
  'binghongcha-scan', 'ksf-scan', 'lehu-scan', 'nongfu-scan', 'wanglaoji-scan'
]
// 开盖扫码抽奖类：定时任务里同样需要填瓶盖码/SN
const SCAN_CODE = ['maidong-scan', 'binghongcha-scan', 'ksf-scan', 'lehu-scan', 'nongfu-scan', 'wanglaoji-scan']
const RUNNABLE = new Set([...CODE_LOGIN, ...ACTION])
const CRON_PRESETS = [
  { label: '每天 08:00', v: '0 0 8 * * *' },
  { label: '每天 12:00', v: '0 0 12 * * *' },
  { label: '每 6 小时', v: '0 0 */6 * * *' },
  { label: '每 30 分钟', v: '0 */30 * * * *' },
  { label: '每周一 09:00', v: '0 0 9 * * 1' }
]

const tasks = ref([])
const projects = ref([])
const accounts = ref([])
const panels = ref([])
const loading = ref(false)

const dialog = ref(false)
const editing = ref(null)      // 编辑时的任务 id
const saving = ref(false)
const projDetail = ref(null)   // 选中项目的完整 runConfig
const form = ref(emptyForm())

const logDialog = ref(false)
const logText = ref('')
const logTitle = ref('')
const runningTaskId = ref(0)

function emptyForm() {
  return {
    name: '', taskType: 'project', projectId: null, appid: '',
    openids: [], proxy: { proxyMode: 'account', proxyUrl: '', proxyRegionCode: '', proxyRegionName: '' },
    sn: '', envName: '', panels: [],
    cron: '0 0 8 * * *', enabled: true
  }
}

const runnableProjects = computed(() => projects.value.filter((p) => RUNNABLE.has(p.builtin)))
const selBuiltin = computed(() => projDetail.value?.runConfig?.builtin || '')
const isCodeLoginSel = computed(() => CODE_LOGIN.includes(selBuiltin.value))
const needsSn = computed(() => SCAN_CODE.includes(selBuiltin.value))
const submitPanels = computed(() => projDetail.value?.runConfig?.submitPanels || [])
const configuredPanels = computed(() => panels.value.filter((p) => p.hasSecret).map((p) => p.panelType))
const panelLabel = (t) => (t === 'qinglong' ? '青龙面板' : t === 'daidai' ? '呆呆面板' : t)

const STATUS = {
  ok: { t: '成功', type: 'success' }, partial: { t: '部分成功', type: 'warning' },
  fail: { t: '失败', type: 'danger' }, running: { t: '运行中', type: 'info' }
}
const statusOf = (s) => STATUS[s] || { t: '未运行', type: 'info' }

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—'
}

async function loadAll() {
  loading.value = true
  try {
    const [t, p, a] = await Promise.all([api.tasks(), api.projects(), api.accounts()])
    tasks.value = t.tasks || []
    projects.value = p.projects || []
    accounts.value = a.accounts || []
    try { panels.value = (await api.panels()).panels || [] } catch { /* 面板可选 */ }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function onProjectChange(id) {
  projDetail.value = null
  form.value.envName = ''
  form.value.panels = []
  if (!id) return
  try {
    projDetail.value = (await api.project(id)).project
    if (CODE_LOGIN.includes(projDetail.value?.runConfig?.builtin)) {
      form.value.envName = projDetail.value.runConfig.envName || ''
      form.value.panels = (projDetail.value.runConfig.submitPanels || []).filter((t) => configuredPanels.value.includes(t))
    }
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function openCreate() {
  editing.value = null
  form.value = emptyForm()
  projDetail.value = null
  dialog.value = true
}

async function openEdit(t) {
  editing.value = t.id
  form.value = {
    name: t.name, taskType: t.taskType, projectId: t.projectId || null, appid: t.appid || '',
    openids: [...t.openids],
    proxy: {
      proxyMode: t.params.proxyMode || (t.params.proxyUrl ? 'long' : 'account'),
      proxyUrl: t.params.proxyUrl || '', proxyRegionCode: t.params.proxyRegionCode || '',
      proxyRegionName: t.params.proxyRegionName || ''
    },
    sn: t.params.sn || '',
    envName: t.params.envName || '', panels: [...(t.params.panels || [])],
    cron: t.cron, enabled: t.enabled
  }
  projDetail.value = null
  dialog.value = true
  if (t.taskType === 'project' && t.projectId) {
    try { projDetail.value = (await api.project(t.projectId)).project } catch { /* ignore */ }
  }
}

async function save() {
  const f = form.value
  if (f.taskType === 'project' && !f.projectId) return ElMessage.warning('请选择项目')
  if (f.taskType === 'code' && !f.appid.trim()) return ElMessage.warning('请填写小程序 appid')
  if (!f.openids.length) return ElMessage.warning('请至少选择一个微信账号')
  if (needsSn.value && !f.sn.trim()) return ElMessage.warning('该项目需填写瓶盖码 / SN')
  if (!f.cron.trim()) return ElMessage.warning('请填写 Cron 表达式')
  const payload = {
    name: f.name.trim(), taskType: f.taskType, projectId: f.projectId, appid: f.appid.trim(),
    openids: f.openids, cron: f.cron.trim(), enabled: f.enabled,
    params: { ...f.proxy, sn: f.sn.trim(), envName: f.envName.trim(), panels: f.panels }
  }
  saving.value = true
  try {
    if (editing.value) await api.updateTask(editing.value, payload)
    else await api.createTask(payload)
    ElMessage.success(editing.value ? '已保存' : '已创建定时任务')
    dialog.value = false
    await loadAll()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function toggle(t) {
  try {
    await api.toggleTask(t.id)
    await loadAll()
  } catch (e) {
    ElMessage.error(e.message)
    await loadAll()   // 失败时回读，纠正 el-switch 已视觉翻转但实际未变的错位状态
  }
}

async function runNow(t) {
  if (runningTaskId.value) return ElMessage.warning('已有任务正在运行，请等待结束')
  runningTaskId.value = t.id
  logText.value = '[INFO] 正在提交后台运行请求…'
  logTitle.value = t.name || '任务'
  logDialog.value = true
  try {
    const start = await api.runTaskStart(t.id)
    let cursor = 0
    logText.value = ''
    while (true) {
      if (!alive.value) return   // 已离开页面：静默停止轮询
      const r = await api.runTaskPoll(t.id, start.runId, cursor)
      cursor = r.cursor || cursor
      if (r.lines?.length) {
        logText.value += (logText.value ? '\n' : '') + r.lines.join('\n')
      }
      if (r.done) {
        const st = r.result?.status || 'fail'
        ElMessage[st === 'ok' ? 'success' : st === 'fail' ? 'error' : 'warning'](`运行完成：${statusOf(st).t}`)
        break
      }
      // 多账号任务容易跑很久，必须轮询日志，不能让 HTTP 请求一直挂着等网关超时。
      await new Promise((resolve) => setTimeout(resolve, 1000))
    }
    if (!alive.value) return
    await loadAll()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    runningTaskId.value = 0
  }
}

function viewLog(t) {
  logText.value = t.lastResult || '（暂无运行日志）'
  logTitle.value = t.name || '任务'
  logDialog.value = true
}

async function remove(t) {
  try {
    await ElMessageBox.confirm(`确定删除定时任务「${t.name || '未命名'}」？`, '删除确认', { type: 'warning' })
  } catch { return }
  try {
    await api.deleteTask(t.id)
    ElMessage.success('已删除')
    await loadAll()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// 日志按级别解析（终端着色）
const LEVEL_RE = /^\[(SUCCESS|INFO|WARNING|WARN|ERROR|DEBUG)\]\s?([\s\S]*)$/
const logLines = computed(() =>
  (logText.value || '').split('\n').map((raw) => {
    const m = raw.match(LEVEL_RE)
    if (m) return { level: m[1] === 'WARN' ? 'WARNING' : m[1], tag: `[${m[1]}]`, rest: m[2] }
    return { level: '', tag: '', rest: raw }
  })
)
async function copyLog() {
  try { await navigator.clipboard.writeText(logText.value); ElMessage.success('已复制') }
  catch { ElMessage.warning('复制失败') }
}

onMounted(loadAll)
</script>

<template>
  <div>
    <div class="row">
      <div>
        <h2 class="ph">定时任务</h2>
        <p class="pd">按 Cron 定时运行项目或获取 Code，支持多账号批量；登录换 Cookie/Token 的项目可自动提交到面板。</p>
      </div>
      <div class="spacer" />
      <el-button type="primary" @click="openCreate">+ 新建定时任务</el-button>
    </div>

    <el-empty v-if="!loading && !tasks.length" description="还没有定时任务，点右上角新建一个" :image-size="90" />

    <div class="list">
      <div v-for="t in tasks" :key="t.id" class="card task rise">
        <div class="t-head">
          <span class="t-ico">{{ t.taskType === 'code' ? '🔑' : (t.project?.icon || '🧩') }}</span>
          <div class="t-name">
            <div class="nm">{{ t.name || '未命名任务' }}</div>
            <div class="sub">
              <el-tag size="small" :type="t.taskType === 'code' ? 'warning' : 'primary'" effect="light">
                {{ t.taskType === 'code' ? '获取 Code' : '运行项目' }}
              </el-tag>
              <span class="tgt">{{ t.taskType === 'code' ? t.appid : (t.project?.name || '项目已删除') }}</span>
            </div>
          </div>
          <div class="spacer" />
          <el-switch :model-value="t.enabled" @change="toggle(t)" />
        </div>

        <div class="t-grid">
          <div class="kv"><span class="k">账号</span><span class="v">{{ t.accountNames.join('、') || '—' }}<span class="cnt">×{{ t.openids.length }}</span></span></div>
          <div class="kv"><span class="k">调度</span><span class="v">{{ t.cronText }} <code class="cron">{{ t.cron }}</code></span></div>
          <div class="kv"><span class="k">下次运行</span><span class="v">{{ t.enabled ? fmt(t.nextRunAt) : '已停用' }}</span></div>
          <div class="kv"><span class="k">上次结果</span><span class="v">
            <el-tag size="small" :type="statusOf(t.lastStatus).type" effect="plain">{{ statusOf(t.lastStatus).t }}</el-tag>
            <span class="ago">{{ fmt(t.lastRunAt) }}</span>
          </span></div>
        </div>

        <div class="t-act">
          <el-button size="small" type="primary" plain :loading="runningTaskId === t.id" @click="runNow(t)">立即运行</el-button>
          <el-button size="small" @click="viewLog(t)">查看日志</el-button>
          <el-button size="small" @click="openEdit(t)">编辑</el-button>
          <el-button size="small" text type="danger" @click="remove(t)">删除</el-button>
        </div>
      </div>
    </div>

    <!-- 新建 / 编辑 -->
    <el-dialog v-model="dialog" :title="editing ? '编辑定时任务' : '新建定时任务'" width="600px" top="6vh" class="tk-dialog">
      <div class="fg">
        <label>任务名称</label>
        <el-input v-model="form.name" placeholder="给任务起个名字，如「每早签到」" maxlength="60" />
      </div>

      <div class="fg">
        <label>任务类型</label>
        <el-radio-group v-model="form.taskType">
          <el-radio-button value="project">运行项目</el-radio-button>
          <el-radio-button value="code">获取 Code</el-radio-button>
        </el-radio-group>
      </div>

      <template v-if="form.taskType === 'project'">
        <div class="fg">
          <label>选择项目</label>
          <el-select v-model="form.projectId" placeholder="选择要定时运行的项目" style="width:100%" @change="onProjectChange">
            <el-option v-for="p in runnableProjects" :key="p.id" :label="p.name" :value="p.id">
              <span>{{ p.icon }} {{ p.name }}</span>
            </el-option>
          </el-select>
        </div>
        <div v-if="needsSn" class="fg">
          <label>瓶盖码 / SN<span class="opt">（一行一个，可批量）</span></label>
          <el-input v-model="form.sn" type="textarea" :rows="4" placeholder="一行一个瓶盖码/SN 链接" />
        </div>
        <div v-if="isCodeLoginSel && submitPanels.length" class="fg box">
          <label>自动提交到面板<span class="opt">（跑完把 Cookie/Token 写入面板环境变量）</span></label>
          <el-input v-model="form.envName" placeholder="环境变量名，如 JD_COOKIE" style="margin-bottom:10px" />
          <el-checkbox-group v-model="form.panels">
            <el-checkbox v-for="pt in submitPanels" :key="pt" :value="pt" border
                         :disabled="!configuredPanels.includes(pt)">
              {{ panelLabel(pt) }}<span v-if="!configuredPanels.includes(pt)" class="undone">未配置</span>
            </el-checkbox>
          </el-checkbox-group>
        </div>
      </template>

      <div v-else class="fg">
        <label>小程序 appid</label>
        <el-input v-model="form.appid" placeholder="如 wx21c7506e98a2fe75" />
      </div>

      <div class="fg">
        <label>微信账号<span class="opt">（可多选，批量执行）</span></label>
        <el-select v-model="form.openids" multiple filterable placeholder="选择账号" style="width:100%">
          <el-option v-for="a in accounts" :key="a.openid" :label="a.nickname || a.openid" :value="a.openid" />
        </el-select>
      </div>

      <div class="fg">
        <label>SOCKS5 代理<span class="opt">（可选，留空用账号绑定的代理）</span></label>
        <ProxySelector v-model="form.proxy" :allow-account="true" />
      </div>

      <div class="fg">
        <label>调度（Cron）<span class="opt">秒 分 时 日 月 周</span></label>
        <el-input v-model="form.cron" placeholder="0 0 8 * * *" class="mono-in" />
        <div class="presets">
          <el-button v-for="c in CRON_PRESETS" :key="c.v" size="small" text bg @click="form.cron = c.v">{{ c.label }}</el-button>
        </div>
      </div>

      <div class="fg fg-row">
        <label>启用</label>
        <el-switch v-model="form.enabled" />
      </div>

      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ editing ? '保存' : '创建' }}</el-button>
      </template>
    </el-dialog>

    <!-- 运行日志（终端风格） -->
    <el-dialog v-model="logDialog" :title="`运行日志 · ${logTitle}`" width="640px" top="6vh" class="tk-dialog">
      <div class="term">
        <div class="term-bar">
          <span class="dots"><i class="d r" /><i class="d y" /><i class="d g" /></span>
          <span class="term-title">运行日志</span>
          <el-button size="small" text class="term-copy" @click="copyLog">复制日志</el-button>
        </div>
        <div class="term-body">
          <div v-for="(ln, i) in logLines" :key="i" class="tline" :class="ln.level ? 'lv-' + ln.level.toLowerCase() : ''">
            <span class="gutter">{{ i + 1 }}</span>
            <span class="content"><span v-if="ln.tag" class="lvl">{{ ln.tag }}</span><span v-if="ln.tag"> </span>{{ ln.rest }}</span>
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.ph { margin: 0 0 4px; }
.pd { color: var(--ink-2); margin: 0; font-size: 13.5px; max-width: 640px; line-height: 1.6; }
.spacer { flex: 1; }

.list { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
@media (max-width: 760px) { .list { grid-template-columns: 1fr; } }
.task { padding: 16px 18px; }
.t-head { display: flex; align-items: center; gap: 12px; }
.t-ico { font-size: 26px; flex: none; }
.t-name { min-width: 0; }
.t-name .nm { font-weight: 600; font-size: 15px; color: var(--ink); }
.t-name .sub { display: flex; align-items: center; gap: 8px; margin-top: 3px; }
.t-name .tgt { color: var(--ink-3); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 160px; }

.t-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; margin: 14px 0; }
.kv { display: flex; gap: 8px; font-size: 13px; min-width: 0; }
.kv .k { color: var(--ink-3); flex: none; width: 56px; }
.kv .v { color: var(--ink); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kv .cnt { color: var(--ink-3); margin-left: 4px; }
.kv .cron { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; color: var(--ink-3); background: #f3f5fa; padding: 1px 5px; border-radius: 4px; }
.kv .ago { color: var(--ink-3); font-size: 12px; margin-left: 6px; }
.t-act { display: flex; gap: 8px; flex-wrap: wrap; border-top: 1px dashed var(--line-2); padding-top: 12px; }

/* 弹窗表单 */
.fg { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
.fg > label { font-size: 13px; color: var(--ink-2); font-weight: 600; }
.fg .opt { color: var(--ink-3); font-weight: 400; margin-left: 4px; }
.fg.fg-row { flex-direction: row; align-items: center; gap: 14px; }
.fg.box { background: #f7f9fd; border: 1px solid var(--line); border-radius: 10px; padding: 14px; }
.mono-in :deep(input) { font-family: ui-monospace, Consolas, monospace; }
.presets { display: flex; flex-wrap: wrap; gap: 6px; }
.undone { color: #e6a23c; font-size: 11px; margin-left: 4px; }

/* 终端风格日志 */
.term { border-radius: 12px; overflow: hidden; border: 1px solid #1f2430; background: #0d1117; }
.term-bar { display: flex; align-items: center; gap: 10px; padding: 9px 14px; background: linear-gradient(180deg, #1b2130, #161b26); border-bottom: 1px solid #232a38; }
.term-bar .dots { display: inline-flex; gap: 7px; }
.term-bar .d { width: 11px; height: 11px; border-radius: 50%; display: inline-block; }
.term-bar .d.r { background: #ff5f56; }
.term-bar .d.y { background: #febc2e; }
.term-bar .d.g { background: #28c840; }
.term-title { color: #9aa4b2; font-size: 12.5px; font-family: ui-monospace, Consolas, monospace; }
.term-copy { margin-left: auto; color: #7d8da5 !important; }
.term-copy:hover { color: #cbd5e1 !important; }
.term-body { max-height: 60vh; overflow: auto; padding: 12px 0; font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12.5px; line-height: 1.85; background: #0d1117; }
.tline { display: flex; padding: 0 16px; white-space: pre-wrap; word-break: break-word; border-left: 2px solid transparent; }
.tline:hover { background: rgba(255, 255, 255, 0.03); }
.tline .gutter { flex: 0 0 34px; text-align: right; padding-right: 14px; color: #4b5563; user-select: none; }
.tline .content { flex: 1; color: #c9d1d9; }
.tline .lvl { font-weight: 700; }
.tline.lv-success .lvl { color: #3fb950; }
.tline.lv-success { border-left-color: #238636; }
.tline.lv-info .lvl { color: #58a6ff; }
.tline.lv-warning .lvl { color: #d29922; }
.tline.lv-warning .content { color: #e3c07b; }
.tline.lv-warning { border-left-color: #9e6a03; }
.tline.lv-error .lvl { color: #ff7b72; }
.tline.lv-error .content { color: #ffa198; }
.tline.lv-error { border-left-color: #da3633; }
.tline.lv-debug .lvl, .tline.lv-debug .content { color: #8b949e; }
</style>
