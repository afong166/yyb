<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api.js'
import ProxySelector from '../components/ProxySelector.vue'

// 组件存活标志：离开页面后中断后台轮询循环，避免僵尸轮询持续打服务器/写已销毁组件
const alive = ref(true)
onBeforeUnmount(() => { alive.value = false })

const route = useRoute()
const router = useRouter()
const project = ref(null)
const running = ref(false)
const runOut = ref('')

const builtin = computed(() => project.value?.runConfig?.builtin || '')
// 内置项目共用同一套运行界面：登录换 Cookie/Token（京东/饿了么/蜜雪冰城/美团）+ 执行类（脉动扫码/浓五的酒馆/益禾堂/红色火箭）
// 开盖扫码抽奖类：运行前需粘贴瓶盖码/SN（脉动 + 冰红茶/康师傅/乐虎/农夫山泉/王老吉）
const SCAN_CODE = [
  'maidong-scan', 'binghongcha-scan', 'ksf-scan', 'lehu-scan', 'nongfu-scan', 'wanglaoji-scan'
]
const CODE_LOGIN = [
  'jd-code-login', 'eleme-code-login', 'mxbc-code-login', 'meituan-code-login',
  'maidong-scan', 'nongwu-tavern', 'yihetang-sign', 'hongse-huojian', 'luckin-draw',
  'yihetang-lottery', 'hsay-sign',
  'binghongcha-scan', 'ksf-scan', 'lehu-scan', 'nongfu-scan', 'wanglaoji-scan'
]
// 执行类项目：服务端跑任务、产出运行日志（而非可提交面板的 Cookie）
const ACTION = [
  'maidong-scan', 'nongwu-tavern', 'yihetang-sign', 'hongse-huojian', 'luckin-draw',
  'yihetang-lottery', 'hsay-sign',
  'binghongcha-scan', 'ksf-scan', 'lehu-scan', 'nongfu-scan', 'wanglaoji-scan'
]
const isCodeLogin = computed(() => CODE_LOGIN.includes(builtin.value))
const isMaidong = computed(() => builtin.value === 'maidong-scan')  // 脉动：专属 90 秒防风控提示
const needsCode = computed(() => SCAN_CODE.includes(builtin.value))  // 扫码类：需要瓶盖码/SN 输入
const isNongwu = computed(() => builtin.value === 'nongwu-tavern')
const isHongse = computed(() => builtin.value === 'hongse-huojian')  // 红色火箭：可选填口令红包口令
const isLuckin = computed(() => builtin.value === 'luckin-draw')  // 瑞幸：额外可选活动期数
const isAction = computed(() => ACTION.includes(builtin.value))
// 瑞幸活动期数列表（来自 run_config.activities，最新一期在前）；activityNo 作为选项 value
const luckinActivities = computed(() => project.value?.runConfig?.activities || [])
const BRANDS = {
  'jd-code-login': '京东',
  'eleme-code-login': '饿了么',
  'mxbc-code-login': '蜜雪冰城',
  'meituan-code-login': '美团',
  'maidong-scan': '脉动',
  'nongwu-tavern': '浓五的酒馆',
  'yihetang-sign': '益禾堂',
  'hongse-huojian': '红色火箭',
  'luckin-draw': '瑞幸咖啡',
  'yihetang-lottery': '益禾堂抽奖',
  'hsay-sign': '沪上阿姨',
  'binghongcha-scan': '冰红茶',
  'ksf-scan': '康师傅',
  'lehu-scan': '乐虎',
  'nongfu-scan': '农夫山泉',
  'wanglaoji-scan': '王老吉'
}
const brand = computed(() => BRANDS[builtin.value] || '')

// 运行日志按行解析日志级别（[SUCCESS]/[INFO]/[WARNING]/[ERROR]/[DEBUG]），供终端样式着色
const LEVEL_RE = /^\[(SUCCESS|INFO|WARNING|WARN|ERROR|DEBUG)\]\s?([\s\S]*)$/
function parseLog(text) {
  return (text || '').split('\n').map((raw) => {
    const m = raw.match(LEVEL_RE)
    if (m) {
      const lvl = m[1] === 'WARN' ? 'WARNING' : m[1]
      return { level: lvl, tag: `[${m[1]}]`, rest: m[2] }
    }
    return { level: '', tag: '', rest: raw }
  })
}
const logLines = computed(() => parseLog(resultCookie.value))
// 实时运行日志（运行中流式追加）
const streaming = ref(false)
const liveLog = ref('')
const liveLines = computed(() => parseLog(liveLog.value))
const liveBody = ref(null)
watch(liveLog, () => nextTick(() => { if (liveBody.value) liveBody.value.scrollTop = liveBody.value.scrollHeight }))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// code 登录用
const accounts = ref([])
const runForm = ref({ openid: '', sn: '', activityNo: '', watchword: '' })
const runProxy = ref({ proxyMode: 'account', proxyUrl: '', proxyRegionCode: '', proxyRegionName: '' })
// 当前选中的活动期数（瑞幸），随下拉选择联动 activityNo/activityId
const selectedActivity = computed(() =>
  luckinActivities.value.find((a) => a.activityNo === runForm.value.activityNo) || null
)
const codeResult = ref(null)
// 不同项目后端返回的 cookie 字段名不同（jdCookie / elemeCookie / cookie），统一取用
const resultCookie = computed(
  () => codeResult.value?.cookie || codeResult.value?.jdCookie || codeResult.value?.elemeCookie || ''
)
const resultAccount = computed(
  () => codeResult.value?.ptPin || codeResult.value?.account || codeResult.value?.userId || ''
)
const accountLabel = computed(() => (builtin.value === 'jd-code-login' ? '京东账号 (pt_pin)' : '账号'))
const selectedWxAccount = computed(() => accounts.value.find((a) => a.openid === runForm.value.openid))
const selectedAccountRemark = computed(() => {
  const openid = runForm.value.openid || ''
  const nick = selectedWxAccount.value?.nickname || ''
  if (!openid) return ''
  return nick ? `${nick}-${openid.slice(-6)}` : `微信账号-${openid.slice(-6)}`
})
// 登录换取物：美团/蜜雪冰城拿到的是 Token，其余是 Cookie
const resultKind = computed(() => (['meituan-code-login', 'mxbc-code-login'].includes(builtin.value) ? 'Token' : 'Cookie'))

// 提交面板
const userPanels = ref([])
const submitForm = ref({ envName: '', panels: [] })
const submitting = ref(false)
const submitResults = ref(null)

const allowedPanels = computed(() => project.value?.runConfig?.submitPanels || [])
const configuredTypes = computed(() =>
  userPanels.value.filter((p) => p.hasSecret).map((p) => p.panelType)
)
const availablePanels = computed(() => allowedPanels.value.filter((t) => configuredTypes.value.includes(t)))
const panelLabel = (t) => (t === 'qinglong' ? '青龙面板' : t === 'daidai' ? '呆呆面板' : t)

async function load() {
  try {
    project.value = (await api.project(route.params.id)).project
  } catch (e) {
    ElMessage.error(e.message)
    router.push('/projects')
    return
  }
  if (isCodeLogin.value) {
    try {
      accounts.value = (await api.accounts()).accounts || []
    } catch {
      /* ignore */
    }
    // 瑞幸：默认选中最新一期活动（列表首项），或 run_config 里的默认 activityNo
    if (isLuckin.value && luckinActivities.value.length) {
      const def = project.value?.runConfig?.activityNo
      runForm.value.activityNo =
        (def && luckinActivities.value.some((a) => a.activityNo === def) ? def : luckinActivities.value[0].activityNo)
    }
    if (allowedPanels.value.length) {
      try {
        userPanels.value = (await api.panels()).panels || []
      } catch {
        /* ignore */
      }
    }
    submitForm.value.envName = project.value?.runConfig?.envName || 'COOKIE'
    submitForm.value.panels = [...availablePanels.value]
  }
}

async function submitToPanels() {
  if (!resultCookie.value) return
  if (!submitForm.value.envName.trim()) return ElMessage.warning('请填写环境变量名')
  if (!submitForm.value.panels.length) return ElMessage.warning('请选择要提交的面板')
  submitting.value = true
  submitResults.value = null
  try {
    const r = await api.submitProject(route.params.id, {
      panels: submitForm.value.panels,
      envName: submitForm.value.envName.trim(),
      value: resultCookie.value,
      // 非京东项目没有 pt_pin，后端/面板按这个备注区分多个账号，避免覆盖上一个变量。
      openid: runForm.value.openid,
      account: resultAccount.value || selectedAccountRemark.value,
      remarks: resultAccount.value || selectedAccountRemark.value
    })
    submitResults.value = r.results || []
    if (submitResults.value.every((x) => x.ok)) ElMessage.success('已提交到面板')
    else ElMessage.warning('部分面板提交失败，见下方结果')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    submitting.value = false
  }
}

// 面板类项目
async function run() {
  running.value = true
  runOut.value = ''
  try {
    const r = await api.runProject(route.params.id, {})
    runOut.value = JSON.stringify(r.result || r, null, 2)
    ElMessage.success(r.result?.message || '已触发')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    running.value = false
  }
}

// code 登录换 Cookie / 执行类项目：后台启动 + 轮询增量日志，实时输出运行过程
async function runCodeLogin() {
  if (!runForm.value.openid) return ElMessage.warning('请选择微信账号')
  if (needsCode.value && !runForm.value.sn.trim()) return ElMessage.warning('请输入瓶盖码/SN')
  running.value = true
  streaming.value = true
  codeResult.value = null
  liveLog.value = ''
  try {
    let payload = needsCode.value
      ? { openid: runForm.value.openid, sn: runForm.value.sn.trim(), ...runProxy.value }
      : { openid: runForm.value.openid, ...runProxy.value }
    // 瑞幸：把选中的活动期数一并传给后端（未选则后端用 run_config 默认活动）
    if (isLuckin.value && selectedActivity.value) {
      payload = { ...payload, activityNo: selectedActivity.value.activityNo, activityId: selectedActivity.value.activityId }
    }
    // 红色火箭：填了口令则一并传给后端做口令红包兑换（留空则只跑签到/ROE/领红包）
    if (isHongse.value && runForm.value.watchword.trim()) {
      payload = { ...payload, watchword: runForm.value.watchword.trim() }
    }
    const { runId } = await api.runProjectStart(route.params.id, payload)
    let cursor = 0
    let fails = 0
    // 轮询拉取实时日志，直到 done；单次网络抖动容错，连续多次失败才中止
    for (;;) {
      await sleep(700)
      if (!alive.value) return   // 已离开页面：静默停止轮询
      let r
      try {
        r = await api.runProjectPoll(route.params.id, runId, cursor)
        fails = 0
      } catch (err) {
        if (!alive.value) return
        if (++fails >= 6) throw new Error('运行日志拉取失败：' + err.message)
        continue
      }
      if (r.lines && r.lines.length) {
        liveLog.value += (liveLog.value ? '\n' : '') + r.lines.join('\n')
      }
      if (typeof r.cursor === 'number') cursor = r.cursor
      if (r.done) {
        const result = r.result || {}
        if (!result.ok) throw new Error(result.error || '运行失败')
        codeResult.value = result
        ElMessage.success(isAction.value ? `${brand.value} 运行完成` : `${brand.value} ${resultKind.value} 获取成功`)
        break
      }
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    running.value = false
    streaming.value = false
  }
}

async function copy(t) {
  try {
    await navigator.clipboard.writeText(t)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败')
  }
}
onMounted(load)
</script>

<template>
  <div v-if="project">
    <el-button text @click="router.push('/projects')">← 返回项目广场</el-button>
    <div class="card mt head">
      <div class="ico">{{ project.icon || '🧩' }}</div>
      <div>
        <h2 class="name">{{ project.name }}</h2>
        <p class="sum">{{ project.summary }}</p>
        <el-tag v-if="isCodeLogin" size="small" type="warning">内置项目</el-tag>
        <el-tag v-else-if="project.panelType" size="small" type="info">{{ project.panelType === 'qinglong' ? '青龙面板' : '呆呆面板' }}运行</el-tag>
      </div>
      <div class="spacer" />
      <el-button v-if="project.panelType && !isCodeLogin" type="primary" :loading="running" @click="run">通过我的面板运行</el-button>
    </div>

    <!-- 内置项目运行界面：登录换 Cookie（京东/饿了么/蜜雪冰城）+ 执行类（脉动扫码/浓五的酒馆）-->
    <div v-if="isCodeLogin" class="card mt">
      <h3>{{ isMaidong ? '脉动扫码 / 抽奖' : isNongwu ? '浓五的酒馆 · 签到抽奖' : needsCode ? brand + ' · 开盖扫码抽奖' : isAction ? brand + ' · 运行' : '获取' + brand + ' Cookie' }}</h3>


      <!-- 脉动专属注意事项：防风控节奏 / 间隔 / 上限 -->
      <div v-if="isMaidong" class="tip-box">
        <div class="tb-head">⚠️ 脉动扫码注意事项（务必先看）</div>
        <ul class="tb-list">
          <li><b>扫码间隔约 90 秒</b>：多个码会在服务端自动逐个扫，每个之间等待约 90 秒防风控。贴 <b>N</b> 个码大约需要 <b>N × 90 秒</b>，运行期间请勿关闭页面、耐心等待结果。</li>
          <li><b>异地务必填代理</b>：脉动有天御风控，异地 IP 很容易被拦。留空则默认用该微信号扫码时绑定的地区代理，异地强烈建议手动填 SOCKS5。</li>
          <li><b>单次别贴太多</b>：建议单账号单次 <b>5~7 个码</b>以内；活动对每个微信号有每日次数 / 领奖上限，达到后会提示「次数用尽」。</li>
          <li><b>被风控的码会保留</b>：命中风控不代表码作废，换号或换代理稍后可再试。</li>
        </ul>
      </div>

      <div class="jd-form" :class="{ 'maidong-form': needsCode || isLuckin || isHongse }">
        <div class="fg">
          <label>选择微信账号</label>
          <el-select v-model="runForm.openid" placeholder="选择已登录的微信账号" size="large" style="width:100%">
            <el-option v-for="a in accounts" :key="a.openid" :label="a.nickname || a.openid" :value="a.openid" />
          </el-select>
        </div>
        <div v-if="isLuckin && luckinActivities.length" class="fg">
          <label>活动期数<span class="opt">（默认最新一期）</span></label>
          <el-select v-model="runForm.activityNo" placeholder="选择要运行的活动期数" size="large" style="width:100%">
            <el-option v-for="a in luckinActivities" :key="a.activityNo" :label="a.label" :value="a.activityNo">
              <span>{{ a.label }}</span>
              <span style="float:right;color:var(--ink-3);font-size:12px">ID {{ a.activityId }}</span>
            </el-option>
          </el-select>
        </div>
        <div v-if="isHongse" class="fg">
          <label>口令红包<span class="opt">（可选，填了才兑换，如「中证半导」；留空只跑签到/ROE/领红包）</span></label>
          <el-input v-model="runForm.watchword" size="large" placeholder="输入本期口令，如 中证半导" clearable />
        </div>
        <div v-if="needsCode" class="fg">
          <label>瓶盖码 / SN<span class="opt">（一行一个，可批量粘贴）</span></label>
          <el-input v-model="runForm.sn" type="textarea" :rows="6" size="large" placeholder="一行一个瓶盖码/SN 链接，可批量粘贴" />
        </div>
        <div class="fg">
          <label>SOCKS5 代理<span class="opt">{{ isAction ? '（异地强烈建议填，留空则用该账号绑定的代理）' : '（可选，异地建议填）' }}</span></label>
          <ProxySelector v-model="runProxy" :allow-account="true" />
        </div>
        <div class="fg fg-btn">
          <el-button type="primary" size="large" :loading="running" @click="runCodeLogin" style="width:100%">{{ isAction ? '运行' + brand : '获取' + brand + ' Cookie' }}</el-button>
        </div>
      </div>
      <el-empty v-if="!accounts.length" description="还没有微信账号，请先到「控制台」扫码登录添加" :image-size="70" />

      <!-- 实时运行日志（运行中流式输出）-->
      <div v-if="streaming" class="jd-result">
        <div class="run-badge"><span class="pulse" />运行中…（实时日志）</div>
        <div class="term">
          <div class="term-bar">
            <span class="dots"><i class="d r" /><i class="d y" /><i class="d g" /></span>
            <span class="term-title">运行日志 · {{ brand }}</span>
          </div>
          <div ref="liveBody" class="term-body">
            <div v-if="!liveLog" class="tline"><span class="gutter">·</span><span class="content wait">等待服务端输出…</span></div>
            <div v-for="(ln, i) in liveLines" :key="i" class="tline" :class="ln.level ? 'lv-' + ln.level.toLowerCase() : ''">
              <span class="gutter">{{ i + 1 }}</span>
              <span class="content"><span v-if="ln.tag" class="lvl">{{ ln.tag }}</span><span v-if="ln.tag"> </span>{{ ln.rest }}</span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="codeResult" class="jd-result">
        <div class="ok-badge">{{ isAction ? '✓ 运行完成' : '✓ 获取成功' }}</div>
        <div v-if="resultAccount" class="kv"><span class="k">{{ accountLabel }}</span><code class="v">{{ resultAccount }}</code></div>
        <!-- 执行类：终端风格运行日志（按日志级别着色）-->
        <div v-if="isAction" class="term">
          <div class="term-bar">
            <span class="dots"><i class="d r" /><i class="d y" /><i class="d g" /></span>
            <span class="term-title">运行日志 · {{ brand }}</span>
            <el-button size="small" text class="term-copy" @click="copy(resultCookie)">复制日志</el-button>
          </div>
          <div class="term-body">
            <div v-for="(ln, i) in logLines" :key="i" class="tline" :class="ln.level ? 'lv-' + ln.level.toLowerCase() : ''">
              <span class="gutter">{{ i + 1 }}</span>
              <span class="content"><span v-if="ln.tag" class="lvl">{{ ln.tag }}</span><span v-if="ln.tag"> </span>{{ ln.rest }}</span>
            </div>
          </div>
        </div>
        <!-- 登录换 Cookie / Token -->
        <div v-else class="kv col">
          <div class="krow"><span class="k">{{ resultKind }}</span><el-button size="small" text @click="copy(resultCookie)">复制 {{ resultKind }}</el-button></div>
          <el-input :model-value="resultCookie" type="textarea" :rows="3" readonly class="mono-area" />
        </div>
        <!-- 提交到面板 -->
        <div v-if="allowedPanels.length" class="submit-box">
          <div class="sb-title">提交到面板</div>
          <div class="sb-grid">
            <div class="fg">
              <label>环境变量名</label>
              <el-input v-model="submitForm.envName" size="large" placeholder="如 ELEME_COOKIE" />
            </div>
            <div class="fg">
              <label>提交到</label>
              <el-checkbox-group v-model="submitForm.panels" class="pchk">
                <el-checkbox v-for="t in availablePanels" :key="t" :value="t" border>{{ panelLabel(t) }}</el-checkbox>
              </el-checkbox-group>
            </div>
            <div class="fg fg-btn">
              <el-button type="success" size="large" :loading="submitting" :disabled="!availablePanels.length" @click="submitToPanels" style="width:100%">提交到面板</el-button>
            </div>
          </div>
          <p v-if="allowedPanels.length && !availablePanels.length" class="warn">
            本项目支持提交到 {{ allowedPanels.map(panelLabel).join(' / ') }}，但你还没在「面板设置」里配置。请先去配置并测试。
          </p>
          <p v-else-if="availablePanels.length < allowedPanels.length" class="warn">
            未配置的面板：{{ allowedPanels.filter(t => !availablePanels.includes(t)).map(panelLabel).join('、') }}（去「面板设置」配置后可提交）
          </p>
          <div v-if="submitResults" class="sb-results">
            <div v-for="r in submitResults" :key="r.panel" class="sb-r" :class="r.ok ? 'ok' : 'fail'">
              {{ panelLabel(r.panel) }}：{{ r.ok ? (r.message || '成功') : (r.error || '失败') }}
            </div>
          </div>
        </div>
        <p v-else-if="!isAction" class="hint">把这段 {{ resultKind }} 填到你的青龙 / 呆呆面板的{{ brand }}环境变量里即可跑对应脚本。</p>
      </div>
    </div>

    <div v-if="runOut" class="card mt"><h3>运行结果</h3><pre class="out">{{ runOut }}</pre></div>

    <div class="card mt"><h3>项目简介</h3><div class="md">{{ project.intro || '（暂无简介）' }}</div></div>
    <div class="card mt"><h3>使用教程</h3><div class="md">{{ project.tutorial || '（暂无教程）' }}</div></div>
    <div v-if="project.panelType && !isCodeLogin" class="tip">提示：运行前请先在「面板设置」配置并测试你的{{ project.panelType === 'qinglong' ? '青龙' : '呆呆' }}面板。</div>
  </div>
</template>

<style scoped>
.mt { margin-top: 16px; }
.head { display: flex; align-items: center; gap: 16px; }
.ico { font-size: 44px; }
.name { margin: 0 0 6px; }
.sum { color: var(--ink-2); margin: 0 0 8px; }
.spacer { flex: 1; }
.md { white-space: pre-wrap; line-height: 1.7; color: #333; font-size: 14px; }
.out { background: #f7f8fa; padding: 12px; border-radius: 8px; overflow: auto; font-size: 12px; }
.tip { color: #e6a23c; font-size: 13px; margin-top: 12px; }
h3 { margin: 0 0 16px; }
.tip-box { background: #fff8ec; border: 1px solid #f5debf; border-radius: 12px; padding: 14px 16px; margin-bottom: 18px; }
.tb-head { font-weight: 700; color: #c77a12; font-size: 14px; margin-bottom: 8px; }
.tb-list { margin: 0; padding-left: 20px; color: #7a5a2e; font-size: 13px; line-height: 1.85; }
.tb-list li { margin: 3px 0; }
.tb-list b { color: #b45309; }
.jd-form { display: grid; grid-template-columns: 1fr 1fr 180px; gap: 16px; align-items: end; }
.jd-form.maidong-form { grid-template-columns: 1fr; }  /* 脉动：账号/SN/代理/按钮竖排，SN 文本域占满 */
.fg { display: flex; flex-direction: column; gap: 8px; }
.fg label { font-size: 13px; color: var(--ink-2); font-weight: 600; }
.fg .opt { color: var(--ink-3); font-weight: 400; }
@media (max-width: 760px) { .jd-form { grid-template-columns: 1fr; } }
.jd-result { margin-top: 20px; border: 1px solid var(--line); border-radius: 12px; padding: 18px; background: #fafcff; }
.ok-badge { color: #2f9e44; font-weight: 700; margin-bottom: 14px; }
.run-badge { display: flex; align-items: center; gap: 8px; color: var(--brand); font-weight: 700; margin-bottom: 14px; }
.run-badge .pulse { width: 9px; height: 9px; border-radius: 50%; background: var(--brand); animation: pulse 1.1s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.35; transform: scale(0.7); } }
.term-body .wait { color: #7d8da5; }
.kv { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.kv.col { flex-direction: column; align-items: stretch; gap: 8px; }
.krow { display: flex; align-items: center; justify-content: space-between; }
.k { font-size: 13px; color: var(--ink-2); font-weight: 600; min-width: 130px; }
.v { font-family: ui-monospace, Consolas, monospace; color: var(--ink); }
.mono-area :deep(textarea) { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; }

/* 终端风格运行日志 */
.term {
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid #1f2430;
  box-shadow: 0 10px 30px rgba(15, 20, 35, 0.28);
  background: #0d1117;
}
.term-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 14px;
  background: linear-gradient(180deg, #1b2130, #161b26);
  border-bottom: 1px solid #232a38;
}
.term-bar .dots { display: inline-flex; gap: 7px; }
.term-bar .d { width: 11px; height: 11px; border-radius: 50%; display: inline-block; }
.term-bar .d.r { background: #ff5f56; }
.term-bar .d.y { background: #febc2e; }
.term-bar .d.g { background: #28c840; }
.term-title {
  color: #9aa4b2;
  font-size: 12.5px;
  font-family: ui-monospace, Consolas, monospace;
  letter-spacing: .3px;
}
.term-copy { margin-left: auto; color: #7d8da5 !important; }
.term-copy:hover { color: #cbd5e1 !important; }
.term-body {
  max-height: 460px;
  overflow: auto;
  padding: 12px 0;
  font-family: ui-monospace, "SF Mono", Consolas, "Cascadia Code", monospace;
  font-size: 12.5px;
  line-height: 1.85;
  background:
    radial-gradient(1200px 200px at 20% -10%, rgba(88, 166, 255, 0.06), transparent),
    #0d1117;
}
.tline {
  display: flex;
  padding: 0 16px;
  white-space: pre-wrap;
  word-break: break-word;
  border-left: 2px solid transparent;
}
.tline:hover { background: rgba(255, 255, 255, 0.03); }
.tline .gutter {
  flex: 0 0 34px;
  text-align: right;
  padding-right: 14px;
  color: #4b5563;
  user-select: none;
}
.tline .content { flex: 1; color: #c9d1d9; }
.tline .lvl { font-weight: 700; }
/* 各级别配色 */
.tline.lv-success .lvl { color: #3fb950; }
.tline.lv-success { border-left-color: #238636; }
.tline.lv-info .lvl { color: #58a6ff; }
.tline.lv-warning .lvl { color: #d29922; }
.tline.lv-warning .content { color: #e3c07b; }
.tline.lv-warning { border-left-color: #9e6a03; }
.tline.lv-error .lvl { color: #ff7b72; }
.tline.lv-error .content { color: #ffa198; }
.tline.lv-error { border-left-color: #da3633; }
.tline.lv-debug .lvl { color: #8b949e; }
.tline.lv-debug .content { color: #8b949e; }
.hint { color: var(--ink-3); font-size: 12.5px; margin: 12px 0 0; line-height: 1.7; }
.submit-box { margin-top: 18px; padding-top: 16px; border-top: 1px dashed var(--line-2); }
.sb-title { font-weight: 700; margin-bottom: 14px; }
.sb-grid { display: grid; grid-template-columns: 1fr 1fr 160px; gap: 16px; align-items: end; }
.pchk { display: flex; flex-wrap: wrap; gap: 8px; }
.warn { color: #e6a23c; font-size: 12.5px; margin: 12px 0 0; line-height: 1.7; }
.sb-results { margin-top: 14px; display: flex; flex-direction: column; gap: 6px; }
.sb-r { font-size: 13px; padding: 8px 12px; border-radius: 8px; }
.sb-r.ok { background: #eafaf0; color: #2f9e44; }
.sb-r.fail { background: #fdeeed; color: #e6534d; }
@media (max-width: 760px) { .sb-grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) {
  .head { flex-wrap: wrap; gap: 12px; }
  .ico { font-size: 38px; }
  .head > .spacer { display: none; }
  .head > .el-button { width: 100%; }
  .kv { flex-wrap: wrap; gap: 6px 12px; }
  .k { min-width: 0; }
}
</style>
