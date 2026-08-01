<script setup>
import { ref } from 'vue'

// 鉴权方式对应的请求头示例
function authHeaders(auth) {
  if (auth === 'session') {
    return 'Content-Type: application/json\nX-Requested-With: XMLHttpRequest\nCookie: yyb_sid=…   # 登录后浏览器自动携带'
  }
  if (auth === 'both') {
    return (
      'Content-Type: application/json\n' +
      '# 方式一（浏览器登录后）：自动带 Cookie: yyb_sid=… + X-Requested-With: XMLHttpRequest\n' +
      '# 方式二（程序 / 脚本调用）：X-License-Key: <你的授权码>'
    )
  }
  if (auth === 'license') {
    return 'Content-Type: application/json\nX-License-Key: <你的授权码>'
  }
  if (auth === 'wx') {
    return 'Content-Type: application/json\nX-License-Key: <你的授权码>   # 或在 body / query 传 auth=<你的授权码>'
  }
  return 'Content-Type: application/json'
}
const authLabel = {
  session: '登录会话（Cookie）',
  both: '登录会话 或 授权码',
  license: '授权码（机器令牌）',
  wx: '授权码（机器令牌）',
  none: '无'
}

const groups = [
  {
    title: '认证',
    items: [
      {
        m: 'POST', p: '/api/auth/register', auth: 'none', d: '注册账号（提交后进入待审核，管理员通过后方可登录）',
        body: '{\n  "username": "yourname",\n  "password": "yourpassword"\n}',
        resp: '{\n  "success": true,\n  "user": { "id": 6, "username": "yourname", "status": "pending" },\n  "message": "注册成功，请等待管理员审核后登录"\n}'
      },
      {
        m: 'POST', p: '/api/auth/login', auth: 'none', d: '登录，成功后服务端下发 httpOnly 会话 Cookie',
        body: '{\n  "username": "yourname",\n  "password": "yourpassword"\n}',
        resp: '{\n  "success": true,\n  "user": { "id": 6, "username": "yourname", "status": "active" },\n  "hasLicense": true\n}'
      },
      {
        m: 'GET', p: '/api/auth/me', auth: 'session', d: '当前登录用户信息 + 授权码摘要',
        body: '',
        resp: '{\n  "success": true,\n  "user": { "id": 6, "username": "yourname", "status": "active" },\n  "license": { "key": "XXXX-XXXX-XXXX-XXXX", "maxUsers": 3, "status": "active", "usedCount": 1, "remaining": 2 }\n}'
      },
      { m: 'POST', p: '/api/auth/logout', auth: 'session', d: '退出登录（清除会话 Cookie）', body: '', resp: '{ "success": true }' }
    ]
  },
  {
    title: '微信扫码登录（长效代理 / 51短效地区 / 应用宝·手游助手双来源）',
    note: '异地登录 token 有效期可能很短。proxyMode 支持 direct、long、short；短效模式只传地区，51 API 由管理员统一配置。扫码和后续续期沿用账号代理，获取 Code 保持 1.1.8 规则走服务器直连。呼和浩特(150100)强制直连且不提取代理。loginSource：1=应用宝，2=手游助手。',
    items: [
      {
        m: 'POST', p: '/api/login/start', auth: 'both',
        d: '发起扫码登录，返回二维码。长效代理传 proxyUrl；短效代理传城市代码和名称。',
        body: '{\n  "proxyMode": "short",              // direct | long | short\n  "proxyRegionCode": "110100",       // short 必填\n  "proxyRegionName": "北京",\n  "loginSource": 1\n}\n\n// 长效代理：{ "proxyMode":"long", "proxyUrl":"socks5://user:pass@host:port", "loginSource":1 }',
        resp: '{\n  "success": true,\n  "sessionId": "xxxx",\n  "uuid": "xxxx",\n  "loginSource": 1,\n  "qrcodeDataUrl": "data:image/png;base64,iVBORw0KGgo…"\n}'
      },
      {
        m: 'GET', p: '/api/login/status?sessionId=xxxx', auth: 'both',
        d: '轮询扫码状态。status 变为 success 时返回登录成功的账号信息',
        body: '',
        resp: '{\n  "success": true,\n  "status": "success",   // waiting | scanned | success | 或含 error\n  "account": { "openid": "oXXXX", "nickname": "昵称", "expireAt": 1782900000000 }\n}'
      },
      { m: 'POST', p: '/api/login/stop', auth: 'both', d: '取消 / 结束扫码会话', body: '{\n  "sessionId": "xxxx"\n}', resp: '{ "success": true }' }
    ]
  },
  {
    title: '微信账号',
    items: [
      {
        m: 'GET', p: '/api/accounts', auth: 'both', d: '列出你名下（该授权码绑定）的微信账号', body: '',
        resp: '{\n  "success": true,\n  "total": 1, "active": 1, "maxUsers": 3,\n  "accounts": [ { "openid": "oXXXX", "nickname": "昵称", "loggedAt": 1782890000000, "expireAt": 1782900000000, "status": "active", "loginSource": 1 } ]   // loginSource: 1=应用宝 2=手游助手\n}'
      },
      { m: 'POST', p: '/api/accounts/refresh', auth: 'both', d: '手动续期账号 token', body: '{\n  "openid": "oXXXX"\n}', resp: '{\n  "success": true,\n  "account": { "openid": "oXXXX", "expireAt": 1782903600000, "expiresIn": 7200 }\n}' },
      { m: 'POST', p: '/api/accounts/delete', auth: 'both', d: '删除账号并解绑', body: '{\n  "openid": "oXXXX"\n}', resp: '{ "success": true, "openid": "oXXXX" }' }
    ]
  },
  {
    title: '获取 Code / 通用云操作 / 手机号 / 用户信息',
    note: '同一套纯协议传输，仅业务不同。首次调用会为该账号建立会话，之后复用（约 0.6s/次）。手机号需该微信号已绑定手机；通用云操作（operateWXData）需填该小程序的真实 api_name；用户信息取 wx.getUserInfo。',
    items: [
      {
        m: 'POST', p: '/api/yyb/get-code', auth: 'both', d: '获取小程序 wx.login code（纯协议）',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef"\n}',
        resp: '{\n  "success": true,\n  "code": "0a1b2c…",\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef"\n}'
      },
      {
        m: 'POST', p: '/api/yyb/get-codes', auth: 'both', d: '多账号并发获取 code',
        body: '{\n  "accounts": ["oAAA", "oBBB"],\n  "appid": "wx1234567890abcdef"\n}',
        resp: '{\n  "success": true,\n  "summary": "2/2 accounts succeeded",\n  "results": [ { "openid": "oAAA", "success": true, "code": "…", "totalMs": 820 } ]\n}'
      },
      {
        m: 'POST', p: '/api/yyb/invoke-cloud', auth: 'both', d: '通用云操作 operateWXData（param2 为 JSON，含 api_name / data，如 webapi_getuserencryptkey）',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "param1": "",                       // 可选\n  "param2": "{\\"api_name\\":\\"<云操作>\\",\\"data\\":{}}"   // 可选，JSON 字符串\n}',
        resp: '{\n  "success": true,\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "respJson": "{…服务端返回的业务 JSON…}"\n}'
      },
      {
        m: 'POST', p: '/api/yyb/get-phone', auth: 'both', d: '获取手机号（需该微信号已绑定手机；param2 留空即取微信绑定号码）',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "param2": ""   // 可选，留空即默认取手机号\n}',
        resp: '{\n  "success": true,\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "mobile": "15300000000",\n  "masked_phone": "153****0000",\n  "code": "5e12a0…",              // 可交服务端 getuserphonenumber 换明文\n  "encryptedData": "…", "iv": "…", "cloudId": "…",\n  "customPhoneList": [ { "mobile": "15500000000", "show_mobile": "155****0000" } ],\n  "respJson": "{…原始 JSON…}"\n}'
      },
      {
        m: 'POST', p: '/api/yyb/get-userinfo', auth: 'both', d: '获取 wx.getUserInfo 用户资料（rawData / signature / encryptedData / iv）',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef"\n}',
        resp: '{\n  "success": true,\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "rawData": "{\\"nickName\\":\\"微信用户\\",\\"avatarUrl\\":\\"https://thirdwx.qlogo.cn/...\\"}",\n  "signature": "3746641b…",\n  "encryptedData": "…", "iv": "…", "cloudId": "…",\n  "respJson": "{…原始 JSON…}"\n}'
      }
    ]
  },
  {
    title: '小程序云函数 / 云托管（wx.cloud.callFunction / callContainer）',
    note: '纯协议调用小程序云开发的云函数与云托管，走取码同一套 ilink 会话（V2 tcbapi_call_gateway）。云函数需该小程序开通云环境（未配默认环境时传 cloudEnv）。云托管为 HTTP 网关。关于 WAF：命中目标站 WAF 时可开 direct 强制直连（跳过腾讯网关，用本机/代理 IP 直接打目标）——但直连只对「按网关出口 IP 拦截」类 WAF 有效；若目标站用 deviceToken 等设备令牌/风控类 WAF（如东鹏 scan.xdp8.cn 走图灵盾 + V3 __wx__/call），换 IP 也过不了，纯 PC 协议目前无法绕过。',
    items: [
      {
        m: 'POST', p: '/api/yyb/cloud-call-function', auth: 'both', d: '调用小程序云函数 wx.cloud.callFunction',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "functionName": "login",\n  "functionData": { "foo": "bar" },   // 可选\n  "cloudEnv": ""                        // 可选，缺省用默认环境\n}',
        resp: '{\n  "success": true,\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "data": { "…": "云函数返回" },\n  "respJson": "{…原始 JSON…}"\n}'
      },
      {
        m: 'POST', p: '/api/yyb/cloud-call-container', auth: 'both', d: '调用小程序云托管容器 wx.cloud.callContainer（V2 网关；可选直连兜底）',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "cloudHost": "xxxxxxxx.sh.wxcloudrun.com",\n  "path": "https://your.api/path?code=…",   // 完整URL(REROUTE) 或 /相对路径\n  "method": "GET",\n  "headers": { "X-Request-App-Code": "xxx" },   // 可选\n  "data": "",                                    // 可选，POST body\n  "proxyUrl": "",                                // 可选，仅对「按IP拦截」类WAF有用；留空回退账号绑定代理\n  "direct": false                               // 可选，true=跳过腾讯网关、直连目标（仅绕IP类WAF）\n}',
        resp: '{\n  "success": true,\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef",\n  "data": { "…": "目标返回" },\n  "wafBlocked": false,          // true=网关出口IP命中目标站WAF（IP类可换代理；deviceToken风控类绕不了）\n  "respJson": "{…原始 JSON…}"\n}'
      }
    ]
  },
  {
    title: '公众号网页授权（OAuth2）',
    note: '用已登录的微信号为公众号完成网页授权（snsapi_base / snsapi_userinfo）。两步：先 authorize 提交授权 URL 拿 scope/授权态，再 confirm 确认授权，返回的 redirect_url 里带网页授权 code（可换 access_token）。这里的 appid 是「公众号」的 AppID，openid 是执行授权的微信账号。走取码同一套纯协议 + 三档会话缓存，支持账号级 SOCKS5 代理。',
    items: [
      {
        m: 'POST', p: '/api/yyb/oauth-authorize', auth: 'both', d: '发起公众号 OAuth2 授权，返回 scope_list / redirect_url',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx公众号appid",\n  "url": "https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx公众号appid&redirect_uri=<回调URL>&response_type=code&scope=snsapi_userinfo&state=<自定义state>#wechat_redirect"\n}',
        resp: '{\n  "success": true,\n  "ok": true,\n  "ret": 0,\n  "errmsg": null,\n  "redirect_url": "",\n  "is_recent_has_auth": 0,\n  "is_slient_auth": 0,\n  "scope_list": [ { "scope": "snsapi_userinfo", "desc": "获取用户信息", "auth_state": 0 } ],\n  "avatar_list": [],\n  "openid": "oXXXX",\n  "appid": "wx公众号appid"\n}'
      },
      {
        m: 'POST', p: '/api/yyb/oauth-authorize-confirm', auth: 'both', d: '确认授权，返回含网页授权 code 的 redirect_url',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx公众号appid",\n  "oauth_url": "https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx公众号appid&redirect_uri=<回调URL>&response_type=code&scope=snsapi_userinfo&state=<自定义state>#wechat_redirect",\n  "opt": 0\n}',
        resp: '{\n  "success": true,\n  "ok": true,\n  "ret": 0,\n  "errmsg": null,\n  "redirect_url": "https://<回调URL>?code=<网页授权code>&state=<自定义state>",\n  "scope_list": [],\n  "avatar_list": [],\n  "openid": "oXXXX",\n  "appid": "wx公众号appid"\n}'
      }
    ]
  },
  {
    title: '项目 & 面板',
    note: '内置项目分两类：① 登录换 Cookie/Token（京东 / 饿了么 / 蜜雪冰城 / 美团）——run 返回 cookie/token，再用 submit 写入青龙/呆呆面板环境变量；② 执行类（脉动扫码 / 浓五的酒馆 / 益禾堂 / 红色火箭 / 瑞幸咖啡）——服务端直接跑任务，run 结果的 cookie 字段即为带级别的运行日志文本。run 每次针对单个 openid；脉动需在 params.sn 传瓶盖码/SN。',
    items: [
      { m: 'GET', p: '/api/projects', auth: 'session', d: '已上架项目列表', body: '', resp: '{\n  "success": true,\n  "projects": [ { "id": 2, "name": "京东 Code 登录获取 Cookie", "submitPanels": ["qinglong","daidai"], "builtin": "jd-code-login" } ]\n}' },
      { m: 'GET', p: '/api/projects/:id', auth: 'session', d: '项目详情（简介 + 教程 + runConfig）', body: '', resp: '{\n  "success": true,\n  "project": { "id": 2, "name": "…", "intro": "…", "tutorial": "…", "runConfig": { "builtin": "jd-code-login", "appid": "wx73247c7819d61796", "submitPanels": ["qinglong","daidai"], "envName": "JD_COOKIE" } }\n}' },
      {
        m: 'POST', p: '/api/projects/:id/run', auth: 'session', d: '运行项目（京东：选微信账号取 code → 换京东 Cookie）',
        body: '{\n  "params": {\n    "openid": "oXXXX",\n    "proxyUrl": "socks5://user:pass@host:port"   // 可选\n  }\n}',
        resp: '{\n  "success": true,\n  "result": { "ok": true, "jdCookie": "pt_key=…;pt_pin=…;", "ptPin": "jd_xxx", "code": "…" }\n}'
      },
      {
        m: 'POST', p: '/api/projects/:id/submit', auth: 'session', d: '把结果提交到你配置好的面板环境变量（可多面板）',
        body: '{\n  "params": {\n    "panels": ["qinglong", "daidai"],\n    "envName": "JD_COOKIE",\n    "value": "pt_key=…;pt_pin=…;"\n  }\n}',
        resp: '{\n  "success": true,\n  "results": [\n    { "panel": "daidai", "ok": true, "message": "呆呆：已新增 JD_COOKIE" },\n    { "panel": "qinglong", "ok": false, "error": "尚未在「面板设置」配置该面板" }\n  ]\n}'
      },
      { m: 'GET', p: '/api/panels', auth: 'session', d: '我的面板配置（脱敏，不含密钥）', body: '', resp: '{\n  "success": true,\n  "panels": [ { "panelType": "daidai", "baseUrl": "http://…", "clientId": "…", "hasSecret": true, "lastTestOk": true } ]\n}' },
      { m: 'PUT', p: '/api/panels/:type', auth: 'session', d: '保存面板配置（:type = qinglong | daidai；密钥留空则沿用旧值）', body: '{\n  "baseUrl": "http://your-panel:5700",\n  "clientId": "<青龙 Client ID / 呆呆 App Key>",\n  "clientSecret": "<Secret，留空不改>"\n}', resp: '{ "success": true, "panel": { "panelType": "daidai", "hasSecret": true } }' },
      { m: 'POST', p: '/api/panels/:type/test', auth: 'session', d: '测试面板连接（服务端取 token）', body: '{}', resp: '{ "success": true, "ok": true, "message": "呆呆面板连接成功" }' }
    ]
  },
  {
    title: '定时任务（Cron 调度）',
    note: 'Cron 为 6 段：秒 分 时 日 月 周（0/7=周日），如「0 0 8 * * *」= 每天 8 点。一条任务可多账号批量（openids 数组）。taskType=project 定时运行内置项目（登录换 Cookie/Token 类可在 params 里配 envName + panels 自动提交面板）；taskType=code 定时获取 wx.login code。调度约每 30 秒扫描一次，秒级会有 ~30s 抖动。',
    items: [
      { m: 'GET', p: '/api/tasks', auth: 'session', d: '列出我的定时任务', body: '',
        resp: '{\n  "success": true,\n  "tasks": [ {\n    "id": 3, "name": "每早签到", "taskType": "project", "projectId": 7,\n    "project": { "name": "益禾堂 积分签到", "icon": "🧋" },\n    "openids": ["oAAA","oBBB"], "accountNames": ["号A","号B"],\n    "params": { "envName": "", "panels": [] },\n    "cron": "0 0 8 * * *", "cronText": "每天 08:00",\n    "enabled": true, "nextRunAt": 1783000000000,\n    "lastRunAt": 1782900000000, "lastStatus": "ok",\n    "lastResult": "[INFO] 任务开始…"\n  } ]\n}' },
      { m: 'POST', p: '/api/tasks', auth: 'session', d: '新建定时任务（运行项目 或 获取 Code）',
        body: '{\n  "name": "每早签到",\n  "taskType": "project",              // project | code\n  "projectId": 7,                      // taskType=project 必填\n  "appid": "wx1234567890abcdef",       // taskType=code 必填\n  "openids": ["oAAA", "oBBB"],         // 多账号批量\n  "cron": "0 0 8 * * *",               // 6 段：秒 分 时 日 月 周\n  "enabled": true,\n  "params": {\n    "proxyUrl": "",                    // 可选\n    "sn": "",                          // 脉动：瓶盖码/SN\n    "envName": "JD_COOKIE",            // 登录换Cookie类：自动提交环境变量名\n    "panels": ["qinglong", "daidai"]   // 登录换Cookie类：自动提交目标面板\n  }\n}',
        resp: '{\n  "success": true,\n  "task": { "id": 3, "name": "每早签到", "cron": "0 0 8 * * *", "cronText": "每天 08:00", "enabled": true, "nextRunAt": 1783000000000 }\n}' },
      { m: 'PUT', p: '/api/tasks/:id', auth: 'session', d: '编辑定时任务（字段同新建）',
        body: '{\n  "name": "每早签到",\n  "taskType": "project",\n  "projectId": 7,\n  "openids": ["oAAA"],\n  "cron": "0 0 7 * * *",\n  "enabled": true,\n  "params": {}\n}',
        resp: '{ "success": true, "task": { "id": 3, "cron": "0 0 7 * * *", "cronText": "每天 07:00" } }' },
      { m: 'POST', p: '/api/tasks/:id/toggle', auth: 'session', d: '启用 / 停用切换（停用后不再调度）', body: '',
        resp: '{ "success": true, "task": { "id": 3, "enabled": false, "nextRunAt": 0 } }' },
      { m: 'POST', p: '/api/tasks/:id/run', auth: 'session', d: '立即运行一次（不影响下次调度时间）', body: '',
        resp: '{\n  "success": true,\n  "result": { "status": "ok", "log": "[INFO] 任务开始…\\n[SUCCESS] …\\n[INFO] 任务结束：成功 2/2", "lastRunAt": 1782900000000 },\n  "task": { "id": 3, "lastStatus": "ok" }\n}' },
      { m: 'DELETE', p: '/api/tasks/:id', auth: 'session', d: '删除定时任务', body: '', resp: '{ "success": true }' }
    ]
  },
  {
    title: '外部脚本兼容（wx_server 风格 · 机器令牌）',
    note: '供青龙等外部脚本调用，用授权码作机器令牌：请求头 X-License-Key: <授权码>，或在 body / query 传 auth=<授权码>。这些接口作用于已登录的账号（先用上面的扫码登录添加账号）。',
    items: [
      {
        m: 'POST', p: '/wx/code', auth: 'wx', d: '获取 wx.login code（wx_server 兼容格式）',
        body: '{\n  "openid": "oXXXX",\n  "appid": "wx1234567890abcdef"\n}',
        resp: '{\n  "status": true,\n  "success": true,\n  "code": "0a1b2c…",\n  "data": { "code": "0a1b2c…", "loginCode": "0a1b2c…" }\n}'
      }
    ]
  }
]
const mColor = { GET: '#2f9e44', POST: '#2f6bf6', PUT: '#e8912d', DELETE: '#e6534d' }

const drawer = ref(false)
const cur = ref(null)
function open(it) {
  cur.value = it
  drawer.value = true
}

// 去掉示例请求体里的 // 行内注释（字符串内部的 // 如 https:// 会被保留），供 cURL 直接使用
function stripJsonComments(s) {
  let out = '', inStr = false, esc = false
  for (let i = 0; i < s.length; i++) {
    const c = s[i]
    if (inStr) {
      out += c
      if (esc) esc = false
      else if (c === '\\') esc = true
      else if (c === '"') inStr = false
      continue
    }
    if (c === '"') { inStr = true; out += c; continue }
    if (c === '/' && s[i + 1] === '/') { while (i < s.length && s[i] !== '\n') i++; continue }
    out += c
  }
  return out
}
// 压成单行合法 JSON（去注释后 parse 再 stringify）；解析失败则退回单行文本
function compactBody(body) {
  const stripped = stripJsonComments(body)
  try { return JSON.stringify(JSON.parse(stripped)) }
  catch { return stripped.replace(/\s*\n\s*/g, ' ').trim() }
}
function curlHeaders(auth) {
  const h = [`-H 'Content-Type: application/json'`]
  if (auth === 'both' || auth === 'license' || auth === 'wx') h.push(`-H 'X-License-Key: <你的授权码>'`)
  else if (auth === 'session') { h.push(`-H 'X-Requested-With: XMLHttpRequest'`); h.push(`-H 'Cookie: yyb_sid=<登录后的会话Cookie>'`) }
  return h
}
// 生成可直接复制运行的完整 cURL：域名取当前站点，body 压成单行合法 JSON
function curlFor(it) {
  if (!it) return ''
  const origin = (typeof window !== 'undefined' && window.location && window.location.origin) || 'https://你的域名'
  const parts = [`curl -X ${it.m} '${origin}${it.p}'`, ...curlHeaders(it.auth)]
  if (it.body) parts.push(`-d '${compactBody(it.body)}'`)
  return parts.join(' \\\n  ')
}

const copied = ref('')
async function copy(t, tag = '') {
  try {
    await navigator.clipboard.writeText(t)
    copied.value = tag
    setTimeout(() => { if (copied.value === tag) copied.value = '' }, 1500)
  } catch {
    /* ignore */
  }
}
</script>

<template>
  <div>
    <h2 class="ph">接口文档</h2>
    <p class="pd">点击任意接口查看请求头、请求体与响应示例。浏览器内接口凭登录会话自动鉴权；程序调用用授权码作机器令牌。</p>

    <div v-for="(g, gi) in groups" :key="g.title" class="card rise" :class="'rise-' + Math.min(gi + 1, 4)" style="margin-bottom:16px">
      <div class="ghead"><h3>{{ g.title }}</h3></div>
      <div v-if="g.note" class="gnote">{{ g.note }}</div>
      <div v-for="it in g.items" :key="it.m + it.p" class="ep" @click="open(it)">
        <span class="m" :style="{ background: mColor[it.m] }">{{ it.m }}</span>
        <code class="path">{{ it.p }}</code>
        <span class="d">{{ it.d }}</span>
        <span class="arrow">›</span>
      </div>
    </div>

    <el-drawer v-model="drawer" :title="cur ? cur.m + ' ' + cur.p : ''" size="560" direction="rtl">
      <div v-if="cur" class="detail">
        <p class="dd">{{ cur.d }}</p>
        <div class="sec curlsec">
          <div class="slabel">完整 cURL（复制后替换 &lt;你的授权码&gt; 与 openid / appid 即可直接调用）
            <el-button size="small" type="primary" @click="copy(curlFor(cur), 'curl')">{{ copied === 'curl' ? '已复制 ✓' : '复制 cURL' }}</el-button>
          </div>
          <pre class="code curl">{{ curlFor(cur) }}</pre>
        </div>
        <div class="sec">
          <div class="slabel">鉴权方式</div>
          <el-tag size="small" type="info" effect="light">{{ authLabel[cur.auth] }}</el-tag>
        </div>
        <div class="sec">
          <div class="slabel">请求头 <el-button size="small" text @click="copy(authHeaders(cur.auth), 'headers')">{{ copied === 'headers' ? '已复制 ✓' : '复制' }}</el-button></div>
          <pre class="code">{{ authHeaders(cur.auth) }}</pre>
        </div>
        <div class="sec" v-if="cur.body">
          <div class="slabel">请求体 <el-button size="small" text @click="copy(cur.body, 'body')">{{ copied === 'body' ? '已复制 ✓' : '复制' }}</el-button></div>
          <pre class="code">{{ cur.body }}</pre>
        </div>
        <div class="sec">
          <div class="slabel">响应示例 <el-button size="small" text @click="copy(cur.resp, 'resp')">{{ copied === 'resp' ? '已复制 ✓' : '复制' }}</el-button></div>
          <pre class="code">{{ cur.resp }}</pre>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.ph { margin: 0 0 4px; }
.pd { color: var(--ink-2); margin: 0 0 18px; font-size: 14px; }
.ghead { margin-bottom: 12px; }
h3 { margin: 0; }
.gnote { color: var(--ink-2); font-size: 13px; line-height: 1.75; background: var(--brand-50); padding: 12px 14px; border-radius: 10px; margin-bottom: 12px; }
.ep { display: flex; align-items: center; gap: 12px; padding: 11px 8px; border-top: 1px dashed var(--line-2); cursor: pointer; border-radius: 8px; transition: background 0.18s var(--ease); }
.ep:first-of-type { border-top: none; }
.ep:hover { background: #f5f8ff; }
.m { color: #fff; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; min-width: 56px; text-align: center; letter-spacing: 0.03em; }
.path { font-family: ui-monospace, Consolas, monospace; font-size: 13px; color: var(--ink); }
.d { color: var(--ink-2); font-size: 13px; }
.arrow { margin-left: auto; color: var(--ink-3); font-size: 20px; }
.detail { padding: 4px 4px 24px; }
.dd { color: var(--ink); font-size: 14px; margin: 0 0 18px; line-height: 1.6; }
.sec { margin-bottom: 18px; }
.slabel { font-size: 13px; font-weight: 600; color: var(--ink-2); margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between; }
.code { background: #0f1729; color: #d6e2ff; font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; line-height: 1.65; padding: 14px 16px; border-radius: 10px; overflow: auto; white-space: pre-wrap; word-break: break-word; margin: 0; }
.curlsec { padding: 12px; border-radius: 12px; background: var(--brand-50); margin-bottom: 20px; }
.curlsec .slabel { color: var(--ink); font-weight: 700; }
.code.curl { background: #0b1220; color: #e3ecff; border: 1px solid #22406b; }
@media (max-width: 560px) {
  .ep { flex-wrap: wrap; gap: 8px 10px; padding: 12px 6px; }
  .path { word-break: break-all; }
  .d { flex-basis: 100%; order: 3; font-size: 12px; }
  .arrow { order: 2; }
}
</style>
