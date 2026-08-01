<script setup>
// 更新日志数据：新版本在最上面。
// 日期为按项目时间线填的估计值，可直接在此数组里改成真实时间。
const releases = [
  {
    v: 'v1.1.8-p1', title: '51短效代理地区池', date: '2026-07-28',
    items: [
      '管理员统一配置 51代理按次套餐 API，普通用户只选择省市，不接触套餐凭据',
      '账号保存直连 / 长效 / 短效策略与地区；短效代理按用户和城市无限期复用，不参考标注到期时间',
      '短效代理连接失败时续期自动换 IP 重试；呼和浩特使用服务器本机出口，不调用代理 API',
      '扫码、账号续期、项目运行、定时任务和云托管支持短效地区；获取 Code 保持 1.1.8 的直连行为'
    ]
  },
  {
    v: 'v1.1.8', title: '公众号 OAuth2 协议修正：一步拿 code，不再需要 confirm', date: '2026-07-22',
    items: [
      '公众号 OAuth2 协议对齐标准微信 MicroMsg：CGI 路径从 ilink 私有路径改为 /cgi-bin/mmbiz-bin/oauth_authorize（cmdid 1254），nested 改为纯 OauthAuthorizeReq（含 BaseRequest），修复旧版服务端读不到 oauthUrl 导致 10010/10011 错误',
      '只需调用 oauth-authorize 一个接口即可拿到含 code 的 redirect_url，不再需要调用 oauth-authorize-confirm（接口保留但通常不需要）',
      'OAuth 响应解析增强：新增传输层信封拆包，正确处理 outer{BaseResponse, 业务体} 结构，scope_list / redirect_url / appname / appicon 等字段解析更健壮',
      'scene 默认值改为 4（网页授权），对齐微信标准'
    ]
  },
  {
    v: 'v1.1.7', title: '手游助手取码链路补全；云托管 WAF 说明更正为实况', date: '2026-07-22',
    items: [
      '补全手游助手（SYZS）取码链路：登录 buffer 现动态解析各自的 ilink_appid / host_appid，并贯穿登录 cmd3453、cmd2881 取码与云函数 / 云托管全链路（此前手游助手账号能扫码但取码报错，现已打通，向下兼容应用宝账号）',
      'WAF 说明更正为实话：云托管「直连兜底」只能绕开「按网关出口 IP 拦截」这一类 WAF（换干净代理即可）；若目标站用 deviceToken 等设备令牌 / 风控类 WAF（如东鹏 scan.xdp8.cn 走图灵盾 + 安卓 V3 __wx__/call 网关），换 IP 也过不了，纯 PC 协议目前无法绕过——之前「配干净代理即可绕 WAF」的表述不准确，特此更正',
      '云托管标签、代理与直连开关的提示文案同步改为按 WAF 类型区分说明'
    ]
  },
  {
    v: 'v1.1.6', title: '扫码登录支持「应用宝 / 手游助手」双来源', date: '2026-07-21',
    items: [
      '扫码登录弹窗新增登录来源选择：可选「应用宝」或「手游助手」，两种来源的账号统一在控制台管理，取码 / 手机号 / 用户信息 / 云函数 / 云托管等能力通用',
      '手游助手账号走独立的微信授权链路（snsapi_login）；自动续期按来源分别处理（应用宝走应用宝续期、手游助手走微信官方续期），互不影响',
      '账号列表对手游助手账号显示「手游」标签，便于区分来源'
    ]
  },
  {
    v: 'v1.1.5', title: '新增获取用户信息 / 云函数 / 云托管（云托管带直连兜底，WAF 适用范围见 v1.1.7 更正）', date: '2026-07-21',
    items: [
      '新增「获取用户信息」：一键获取 wx.getUserInfo 的用户资料（昵称 / 头像 rawData、signature、encryptedData / iv），走应用宝纯协议，无需扫码网关',
      '新增「云函数」：纯协议调用小程序云函数 wx.cloud.callFunction，可传函数名 functionName / 参数 functionData / 云环境 cloudEnv',
      '新增「云托管」：纯协议调用小程序云托管容器 wx.cloud.callContainer，支持自定义 cloudHost / path / method / headers / body',
      '云托管命中目标站 WAF（如出口 IP 被风控 403）时自动切换「代理直连」兜底；并提供「强制走代理直连」开关，配一个干净代理（住宅 / 移动 / 4G）可绕开被拦的网关链路',
      '控制台「获取操作」标签调整：原「调用云函数」更名为「通用云操作」（它是通用 operateWXData，避免与新「云函数」混淆）；「获取 encryptedData 和 iv」现在同时展示手机号，移除单独的「获取手机号」标签'
    ]
  },
  {
    v: 'v1.1.4', title: '定时任务时区、默认自动续期、SOCKS5 与多项安全 / 稳定性修复', date: '2026-07-17',
    items: [
      '定时任务统一按中国（上海）时区调度与显示：修复服务器为 UTC 时「每天 08:00」实际按 UTC 触发导致差 8 小时的问题',
      '默认开启账号自动续期（到期前 5 分钟）；新增账号缺失有效期时也能被自动续期覆盖',
      '修复 SOCKS5 代理拉二维码报 SSLError：socks5 统一升级为 socks5h（代理端 DNS），存量账号代理一并迁移',
      '安全修复：扫码「停止」接口补鉴权（此前可被清空全体扫码会话）；默认信任代理 IP 头改为关闭；修复无授权码可无限加号',
      '修复面板设置无法保存：面板地址允许指向内网 / 环回 / docker 地址（自托管场景），端口按实际填写不再受限',
      '修复部分开盖 / 抽奖类项目任务失败仍显示「成功」；修复农夫山泉运行时的卡顿；修复 Cron 周字段范围（如 5-7）漏跑周日',
      '前端稳健性：离开页面停止后台日志轮询；会话过期自动跳回登录页；多处加载失败给出提示而非白屏',
      '其它：扫码会话与超期调用记录自动清理、定时任务异常不再卡「运行中」、面板提交增加 SSRF 复检'
    ]
  },
  {
    v: 'v1.1.3', title: '新增益禾堂抽奖 / 沪上阿姨签到，及冰红茶 / 康师傅 / 乐虎 / 农夫山泉 / 王老吉开盖赢奖', date: '2026-07-10',
    items: [
      '新增项目「益禾堂 抽奖」：Code 登录换 qm-user-token（未绑手机号自动授权），按活动签名参与抽奖并汇总中奖结果，活动 ID 可在运行时指定',
      '新增项目「沪上阿姨 签到」：Code 登录换会员，打开小满活动授权页刷新活动态，动态生成 tokenSign / xmSign 完成每日签到',
      '新增项目「冰红茶 开盖赢奖」（康师傅冰红茶 1L 码上赢黄金）：Code 登录换活动态，输入瓶盖码链接后扫码抽奖并查询奖品',
      '新增项目「康师傅 开盖赢奖」：会员登录 + ciphertext 换活动态，扫瓶盖码抽奖，中奖自动核销并返回核销二维码信息',
      '新增项目「乐虎 开盖赢奖」：达利 Token 登录识别活动，扫瓶盖码抽奖，命中现金红包自动提现',
      '新增项目「农夫山泉 开盖赢奖」：Code 登录后按活动 AES/MD5 参数算法开盖抽奖并汇总奖品记录',
      '新增项目「王老吉 开盖赢奖」（开盖扫码赢 5 元礼金）：会员登录换活动 Token，扫瓶盖码抽奖，中奖自动领取礼金（未注册自动授权注册）',
      '以上开盖赢奖类项目均支持粘贴多条瓶盖码（换行 / 逗号分隔）、地区代理（异地防风控，留空默认用账号绑定代理），运行日志实时按级别着色输出'
    ]
  },
  {
    v: 'v1.1.2', title: '红色火箭口令红包，瑞幸活动期数可选', date: '2026-07-08',
    items: [
      '红色火箭新增「口令红包」：运行时可选填当期口令（如「中证半导」），自动完成口令兑换并领取红包；留空则和以前一样只跑签到 / ROE / 领红包',
      '瑞幸咖啡新增「活动期数」下拉：可选择运行不同期的活动（默认最新一期），抓到新一期在后端活动列表追加一条即可，无需改动运行页'
    ]
  },
  {
    v: 'v1.1.1', title: '续期修复、取 Code 加速、定时任务与面板同步修复', date: '2026-07-07',
    items: [
      '修复账号续期不生效：续期改走应用宝 pcyyb_refresh_token_auth 链路，并在续期后立即校验 login_buffer，避免“显示续期成功但取码仍失败”',
      '优化取 Code 速度：shortcloud 响应按 Content-Length 收满即返回，不再等待连接关闭；热会话场景减少无意义等待',
      '优化取码会话复用：会话缓存默认对齐 30 分钟，并新增 SQLite 持久化，服务重启后仍可复用热会话，减少重复握手和完整登录',
      '修复更新日志页面刷新 404：用户端 / 管理端改为 hash 路由，并兼容旧 /changelog 地址跳转到 /#/changelog',
      '修复定时任务多账号立即运行 524：改为后台运行 + 轮询日志，前端实时追加运行日志，不再让单个 HTTP 请求长时间挂起',
      '修复多账号同步到青龙 / 呆呆面板互相覆盖：京东继续用 pt_pin 做备注；饿了么 / 蜜雪 / 美团等项目改用账号标识或微信昵称尾号做备注',
      '安全优化：续期错误信息统一脱敏，避免 access_token / refresh_token 等敏感凭据出现在前端提示、日志或数据库错误字段中'
    ]
  },
  {
    v: 'v1.1.0', title: '运行日志实时输出，控制台账号搜索', date: '2026-07-06',
    items: [
      '运行项目改为后台运行 + 实时日志：运行过程逐行流式显示（终端风格、级别着色、自动滚动到底部），不再干等到结束才一次性出全部日志',
      '彻底解决长任务（脉动多码、红色火箭等）单请求超时报错（网关 524）：改为秒回 + 增量轮询，并对网络抖动做容错',
      '控制台「微信账号」新增搜索框，可按微信名或 openid 快速查找，支持分页联动'
    ]
  },
  {
    v: 'v1.0.9', title: '美团 / 红色火箭修复，运行日志统一，脉动防风控节奏', date: '2026-07-06',
    items: [
      '美团 Code 登录：默认直连（不再回退账号代理），避免代理触发风控「只返回身份、不下发 token」；未拿到 token 时给出明确原因提示',
      '红色火箭：修复 encryptKey 的 base64 填充问题导致 SM4 加密失败、退化明文提交被服务端 7005 拒绝；红包记录新增显示时间',
      '脉动扫码：防风控节奏由 65 秒提升到 90 秒，项目页新增注意事项提示（间隔 / 代理 / 单次码数 / 风控码保留）',
      '统一所有执行类项目运行日志为带级别（成功 / 信息 / 警告 / 错误）着色显示，去掉分隔线，更清晰一致'
    ]
  },
  {
    v: 'v1.0.8', title: '新增定时任务（Cron 调度）', date: '2026-07-06',
    items: [
      '新增「定时任务」页面：可按 Cron 定时运行项目，或定时获取 wx.login Code',
      '支持多账号批量执行；Cron 为 6 段（秒 分 时 日 月 周），内置每天 / 每 N 小时 / 每周等常用预设',
      '登录换 Cookie/Token 类项目（京东 / 饿了么 / 蜜雪冰城 / 美团）跑完可自动提交到青龙 / 呆呆面板',
      '每条任务可立即运行、启用 / 停用、查看运行日志（终端风格、按级别着色）'
    ]
  },
  {
    v: 'v1.0.7', title: '新增益禾堂 / 美团 / 红色火箭 / 瑞幸咖啡，运行日志终端化', date: '2026-07-06',
    items: [
      '新增项目「益禾堂」：积分签到，Code 登录换 qm-token（未绑手机号自动授权），换兑吧活动页登录态完成每日签到',
      '新增项目「美团」：Code 登录，本地纯算 mtgsig 签名换取 userId / token / openId，可一键提交到青龙 / 呆呆面板',
      '新增项目「红色火箭」：华泰基金指慧家，自动签到 + 按指数 ROE 生成口令兑换 + 命中红包自动 H5 领取',
      '新增项目「瑞幸咖啡」：活动抽奖并自动汇总本次与历史中奖记录，活动编号可在项目配置里按当期活动修改',
      '执行类项目支持地区代理（异地防风控），留空则默认用该账号绑定的代理',
      '优化运行日志显示：执行类项目改用终端风格代码块，按日志级别（成功 / 信息 / 警告 / 错误）着色，更清晰'
    ]
  },
  {
    v: 'v1.0.6', title: '新增蜜雪冰城 / 脉动 / 浓五的酒馆，修复账号在线检测', date: '2026-07-05',
    items: [
      '新增项目「蜜雪冰城」：Code 登录换取 accessToken，可一键提交到青龙 / 呆呆面板',
      '新增项目「脉动扫码抽奖」：支持批量瓶盖码 / SN，扫码后有剩余次数自动抽奖',
      '新增项目「浓五的酒馆」：自动完成每日签到、积分抽奖、刮刮乐',
      '脉动 / 浓五支持地区代理（异地防风控），留空则默认用该账号绑定的代理',
      '修复账号「显示在线却取码失败」：取码失败会如实翻红并提示重扫，自动续期不再把失效账号刷回在线'
    ]
  },
  {
    v: 'v1.0.5', title: '新增公众号网页授权（OAuth2）', date: '2026-07-04',
    desc: '公众号 OAuth2 授权，需要带入公众号 appid。'
  },
  {
    v: 'v1.0.4', title: '新增项目：京东、饿了么', date: '2026-07-03',
    desc: '京东、饿了么 Code 登录换取 Cookie。'
  },
  {
    v: 'v1.0.3', title: '新增云函数与手机号获取', date: '2026-07-02',
    desc: '打通云函数调用与手机号参数获取。'
  },
  {
    v: 'v1.0.2', title: '优化自动续期', date: '2026-07-01',
    desc: '采用官方接口，优化自动续期功能。'
  },
  {
    v: 'v1.0.0', title: '应用宝纯协议打通', date: '2026-06-30',
    desc: '通过授权应用宝获取各大小程序 Code。'
  }
]
</script>

<template>
  <div>
    <h2 class="ph">更新日志</h2>
    <p class="pd">记录每次版本迭代的更新内容，最新版本在最上方。</p>

    <div class="card rise rise-1">
      <ul class="tl">
        <li v-for="(r, i) in releases" :key="r.v" class="item">
          <span class="node"><span class="dot" /></span>
          <div class="body">
            <div class="head">
              <span class="ver">{{ r.v }}</span>
              <span class="title">{{ r.title }}</span>
              <span v-if="i === 0" class="new">最新</span>
            </div>
            <div class="date">{{ r.date }}</div>
            <ul v-if="r.items" class="desc items">
              <li v-for="(it, i) in r.items" :key="i">{{ it }}</li>
            </ul>
            <div v-else class="desc">{{ r.desc }}</div>
          </div>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.ph { margin: 0 0 4px; }
.pd { color: var(--ink-2); margin: 0 0 18px; font-size: 14px; }
.tl { list-style: none; margin: 0; padding: 6px 2px; position: relative; }
.item { position: relative; display: flex; gap: 16px; padding-bottom: 26px; }
.item:last-child { padding-bottom: 4px; }
/* 时间线竖线：从当前节点圆点连到下一个 */
.node { position: relative; flex: none; width: 14px; display: flex; justify-content: center; padding-top: 5px; }
.node::before {
  content: ''; position: absolute; top: 16px; bottom: -26px; left: 50%;
  width: 2px; transform: translateX(-50%); background: var(--line-2);
}
.item:last-child .node::before { display: none; }
.dot {
  width: 11px; height: 11px; border-radius: 50%; background: var(--brand);
  box-shadow: 0 0 0 4px var(--brand-50); position: relative; z-index: 1;
}
.body { flex: 1; min-width: 0; }
.head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ver {
  background: var(--brand); color: #fff; font-size: 12px; font-weight: 700;
  padding: 2px 9px; border-radius: 6px; letter-spacing: 0.02em;
  font-family: ui-monospace, Consolas, monospace; flex: none;
}
.title { font-size: 15px; font-weight: 600; color: var(--ink); }
.new {
  font-size: 11px; color: var(--brand); border: 1px solid var(--brand);
  border-radius: 20px; padding: 1px 8px; line-height: 1.5;
}
.date { color: var(--ink-3); font-size: 12.5px; margin: 8px 0 6px; font-variant-numeric: tabular-nums; }
.desc { color: var(--ink-2); font-size: 13.5px; line-height: 1.7; }
.desc.items { margin: 0; padding-left: 18px; }
.desc.items li { margin: 3px 0; }

@media (max-width: 600px) {
  .title { font-size: 14px; }
  .desc { font-size: 13px; }
}
</style>
