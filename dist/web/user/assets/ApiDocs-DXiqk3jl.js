import{_ as U,c,a as n,F as g,m as T,b as m,d as y,r as _,f as k,o as l,t as a,q as x,D as q,A as E,e as r}from"./index-D8xTdsAg.js";const I={class:"ghead"},L={key:0,class:"gnote"},R=["onClick"],B={class:"path"},D={class:"d"},z={key:0,class:"detail"},J={class:"dd"},K={class:"sec curlsec"},G={class:"slabel"},H={class:"code curl"},$={class:"sec"},V={class:"sec"},F={class:"slabel"},M={class:"code"},W={key:0,class:"sec"},Q={class:"slabel"},Y={class:"code"},Z={class:"sec"},ee={class:"slabel"},se={class:"code"},ne={__name:"ApiDocs",setup(te){function f(s){return s==="session"?`Content-Type: application/json
X-Requested-With: XMLHttpRequest
Cookie: yyb_sid=…   # 登录后浏览器自动携带`:s==="both"?`Content-Type: application/json
# 方式一（浏览器登录后）：自动带 Cookie: yyb_sid=… + X-Requested-With: XMLHttpRequest
# 方式二（程序 / 脚本调用）：X-License-Key: <你的授权码>`:s==="license"?`Content-Type: application/json
X-License-Key: <你的授权码>`:s==="wx"?`Content-Type: application/json
X-License-Key: <你的授权码>   # 或在 body / query 传 auth=<你的授权码>`:"Content-Type: application/json"}const w={session:"登录会话（Cookie）",both:"登录会话 或 授权码",license:"授权码（机器令牌）",wx:"授权码（机器令牌）",none:"无"},C=[{title:"认证",items:[{m:"POST",p:"/api/auth/register",auth:"none",d:"注册账号（提交后进入待审核，管理员通过后方可登录）",body:`{
  "username": "yourname",
  "password": "yourpassword"
}`,resp:`{
  "success": true,
  "user": { "id": 6, "username": "yourname", "status": "pending" },
  "message": "注册成功，请等待管理员审核后登录"
}`},{m:"POST",p:"/api/auth/login",auth:"none",d:"登录，成功后服务端下发 httpOnly 会话 Cookie",body:`{
  "username": "yourname",
  "password": "yourpassword"
}`,resp:`{
  "success": true,
  "user": { "id": 6, "username": "yourname", "status": "active" },
  "hasLicense": true
}`},{m:"GET",p:"/api/auth/me",auth:"session",d:"当前登录用户信息 + 授权码摘要",body:"",resp:`{
  "success": true,
  "user": { "id": 6, "username": "yourname", "status": "active" },
  "license": { "key": "XXXX-XXXX-XXXX-XXXX", "maxUsers": 3, "status": "active", "usedCount": 1, "remaining": 2 }
}`},{m:"POST",p:"/api/auth/logout",auth:"session",d:"退出登录（清除会话 Cookie）",body:"",resp:'{ "success": true }'}]},{title:"微信扫码登录（支持 SOCKS5 代理）",note:"异地登录 token 有效期可能很短。程序调用时可在 /api/login/start 的请求体加 proxyUrl（socks5://user:pass@host:port），登录及后续该账号的续期/取号都会走这个代理，从而延长有效期。流程：start → 拿二维码 → 用户扫码 → 轮询 status 直到 success。",items:[{m:"POST",p:"/api/login/start",auth:"both",d:"发起扫码登录，返回二维码（Base64）。proxyUrl 可选，支持 socks5 / http 代理。",body:`{
  "proxyUrl": "socks5://user:pass@1.2.3.4:1080"   // 可选，留空则直连
}`,resp:`{
  "success": true,
  "sessionId": "xxxx",
  "uuid": "xxxx",
  "qrcodeDataUrl": "data:image/png;base64,iVBORw0KGgo…"
}`},{m:"GET",p:"/api/login/status?sessionId=xxxx",auth:"both",d:"轮询扫码状态。status 变为 success 时返回登录成功的账号信息",body:"",resp:`{
  "success": true,
  "status": "success",   // waiting | scanned | success | 或含 error
  "account": { "openid": "oXXXX", "nickname": "昵称", "expireAt": 1782900000000 }
}`},{m:"POST",p:"/api/login/stop",auth:"both",d:"取消 / 结束扫码会话",body:`{
  "sessionId": "xxxx"
}`,resp:'{ "success": true }'}]},{title:"微信账号",items:[{m:"GET",p:"/api/accounts",auth:"both",d:"列出你名下（该授权码绑定）的微信账号",body:"",resp:`{
  "success": true,
  "total": 1, "active": 1, "maxUsers": 3,
  "accounts": [ { "openid": "oXXXX", "nickname": "昵称", "loggedAt": 1782890000000, "expireAt": 1782900000000, "status": "active" } ]
}`},{m:"POST",p:"/api/accounts/refresh",auth:"both",d:"手动续期账号 token",body:`{
  "openid": "oXXXX"
}`,resp:`{
  "success": true,
  "account": { "openid": "oXXXX", "expireAt": 1782903600000, "expiresIn": 7200 }
}`},{m:"POST",p:"/api/accounts/delete",auth:"both",d:"删除账号并解绑",body:`{
  "openid": "oXXXX"
}`,resp:'{ "success": true, "openid": "oXXXX" }'}]},{title:"获取 Code / 云函数 / 手机号",note:"三者同一套纯协议传输，仅业务不同。首次调用会为该账号建立会话，之后复用（约 0.6s/次）。手机号需该微信号已绑定手机；云函数需填该小程序的真实 api_name。",items:[{m:"POST",p:"/api/yyb/get-code",auth:"both",d:"获取小程序 wx.login code（纯协议）",body:`{
  "openid": "oXXXX",
  "appid": "wx1234567890abcdef"
}`,resp:`{
  "success": true,
  "code": "0a1b2c…",
  "openid": "oXXXX",
  "appid": "wx1234567890abcdef"
}`},{m:"POST",p:"/api/yyb/get-codes",auth:"both",d:"多账号并发获取 code",body:`{
  "accounts": ["oAAA", "oBBB"],
  "appid": "wx1234567890abcdef"
}`,resp:`{
  "success": true,
  "summary": "2/2 accounts succeeded",
  "results": [ { "openid": "oAAA", "success": true, "code": "…", "totalMs": 820 } ]
}`},{m:"POST",p:"/api/yyb/invoke-cloud",auth:"both",d:"调用云函数 operateWXData（param2 为 JSON，含 api_name / data）",body:`{
  "openid": "oXXXX",
  "appid": "wx1234567890abcdef",
  "param1": "",                       // 可选
  "param2": "{\\"api_name\\":\\"<云操作>\\",\\"data\\":{}}"   // 可选，JSON 字符串
}`,resp:`{
  "success": true,
  "openid": "oXXXX",
  "appid": "wx1234567890abcdef",
  "respJson": "{…服务端返回的业务 JSON…}"
}`},{m:"POST",p:"/api/yyb/get-phone",auth:"both",d:"获取手机号（需该微信号已绑定手机；param2 留空即取微信绑定号码）",body:`{
  "openid": "oXXXX",
  "appid": "wx1234567890abcdef",
  "param2": ""   // 可选，留空即默认取手机号
}`,resp:`{
  "success": true,
  "openid": "oXXXX",
  "appid": "wx1234567890abcdef",
  "mobile": "15300000000",
  "masked_phone": "153****0000",
  "code": "5e12a0…",              // 可交服务端 getuserphonenumber 换明文
  "encryptedData": "…", "iv": "…", "cloudId": "…",
  "customPhoneList": [ { "mobile": "15500000000", "show_mobile": "155****0000" } ],
  "respJson": "{…原始 JSON…}"
}`}]},{title:"公众号网页授权（OAuth2）",note:"用已登录的微信号为公众号完成网页授权（snsapi_base / snsapi_userinfo）。两步：先 authorize 提交授权 URL 拿 scope/授权态，再 confirm 确认授权，返回的 redirect_url 里带网页授权 code（可换 access_token）。这里的 appid 是「公众号」的 AppID，openid 是执行授权的微信账号。走取码同一套纯协议 + 三档会话缓存，支持账号级 SOCKS5 代理。",items:[{m:"POST",p:"/api/yyb/oauth-authorize",auth:"both",d:"发起公众号 OAuth2 授权，返回 scope_list / redirect_url",body:`{
  "openid": "oXXXX",
  "appid": "wx公众号appid",
  "url": "https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx公众号appid&redirect_uri=<回调URL>&response_type=code&scope=snsapi_userinfo&state=<自定义state>#wechat_redirect"
}`,resp:`{
  "success": true,
  "ok": true,
  "ret": 0,
  "errmsg": null,
  "redirect_url": "",
  "is_recent_has_auth": 0,
  "is_slient_auth": 0,
  "scope_list": [ { "scope": "snsapi_userinfo", "desc": "获取用户信息", "auth_state": 0 } ],
  "avatar_list": [],
  "openid": "oXXXX",
  "appid": "wx公众号appid"
}`},{m:"POST",p:"/api/yyb/oauth-authorize-confirm",auth:"both",d:"确认授权，返回含网页授权 code 的 redirect_url",body:`{
  "openid": "oXXXX",
  "appid": "wx公众号appid",
  "oauth_url": "https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx公众号appid&redirect_uri=<回调URL>&response_type=code&scope=snsapi_userinfo&state=<自定义state>#wechat_redirect",
  "opt": 0
}`,resp:`{
  "success": true,
  "ok": true,
  "ret": 0,
  "errmsg": null,
  "redirect_url": "https://<回调URL>?code=<网页授权code>&state=<自定义state>",
  "scope_list": [],
  "avatar_list": [],
  "openid": "oXXXX",
  "appid": "wx公众号appid"
}`}]},{title:"项目 & 面板",note:"内置项目分两类：① 登录换 Cookie/Token（京东 / 饿了么 / 蜜雪冰城 / 美团）——run 返回 cookie/token，再用 submit 写入青龙/呆呆面板环境变量；② 执行类（脉动扫码 / 浓五的酒馆 / 益禾堂 / 红色火箭 / 瑞幸咖啡）——服务端直接跑任务，run 结果的 cookie 字段即为带级别的运行日志文本。run 每次针对单个 openid；脉动需在 params.sn 传瓶盖码/SN。",items:[{m:"GET",p:"/api/projects",auth:"session",d:"已上架项目列表",body:"",resp:`{
  "success": true,
  "projects": [ { "id": 2, "name": "京东 Code 登录获取 Cookie", "submitPanels": ["qinglong","daidai"], "builtin": "jd-code-login" } ]
}`},{m:"GET",p:"/api/projects/:id",auth:"session",d:"项目详情（简介 + 教程 + runConfig）",body:"",resp:`{
  "success": true,
  "project": { "id": 2, "name": "…", "intro": "…", "tutorial": "…", "runConfig": { "builtin": "jd-code-login", "appid": "wx73247c7819d61796", "submitPanels": ["qinglong","daidai"], "envName": "JD_COOKIE" } }
}`},{m:"POST",p:"/api/projects/:id/run",auth:"session",d:"运行项目（京东：选微信账号取 code → 换京东 Cookie）",body:`{
  "params": {
    "openid": "oXXXX",
    "proxyUrl": "socks5://user:pass@host:port"   // 可选
  }
}`,resp:`{
  "success": true,
  "result": { "ok": true, "jdCookie": "pt_key=…;pt_pin=…;", "ptPin": "jd_xxx", "code": "…" }
}`},{m:"POST",p:"/api/projects/:id/submit",auth:"session",d:"把结果提交到你配置好的面板环境变量（可多面板）",body:`{
  "params": {
    "panels": ["qinglong", "daidai"],
    "envName": "JD_COOKIE",
    "value": "pt_key=…;pt_pin=…;"
  }
}`,resp:`{
  "success": true,
  "results": [
    { "panel": "daidai", "ok": true, "message": "呆呆：已新增 JD_COOKIE" },
    { "panel": "qinglong", "ok": false, "error": "尚未在「面板设置」配置该面板" }
  ]
}`},{m:"GET",p:"/api/panels",auth:"session",d:"我的面板配置（脱敏，不含密钥）",body:"",resp:`{
  "success": true,
  "panels": [ { "panelType": "daidai", "baseUrl": "http://…", "clientId": "…", "hasSecret": true, "lastTestOk": true } ]
}`},{m:"PUT",p:"/api/panels/:type",auth:"session",d:"保存面板配置（:type = qinglong | daidai；密钥留空则沿用旧值）",body:`{
  "baseUrl": "http://your-panel:5700",
  "clientId": "<青龙 Client ID / 呆呆 App Key>",
  "clientSecret": "<Secret，留空不改>"
}`,resp:'{ "success": true, "panel": { "panelType": "daidai", "hasSecret": true } }'},{m:"POST",p:"/api/panels/:type/test",auth:"session",d:"测试面板连接（服务端取 token）",body:"{}",resp:'{ "success": true, "ok": true, "message": "呆呆面板连接成功" }'}]},{title:"定时任务（Cron 调度）",note:"Cron 为 6 段：秒 分 时 日 月 周（0/7=周日），如「0 0 8 * * *」= 每天 8 点。一条任务可多账号批量（openids 数组）。taskType=project 定时运行内置项目（登录换 Cookie/Token 类可在 params 里配 envName + panels 自动提交面板）；taskType=code 定时获取 wx.login code。调度约每 30 秒扫描一次，秒级会有 ~30s 抖动。",items:[{m:"GET",p:"/api/tasks",auth:"session",d:"列出我的定时任务",body:"",resp:`{
  "success": true,
  "tasks": [ {
    "id": 3, "name": "每早签到", "taskType": "project", "projectId": 7,
    "project": { "name": "益禾堂 积分签到", "icon": "🧋" },
    "openids": ["oAAA","oBBB"], "accountNames": ["号A","号B"],
    "params": { "envName": "", "panels": [] },
    "cron": "0 0 8 * * *", "cronText": "每天 08:00",
    "enabled": true, "nextRunAt": 1783000000000,
    "lastRunAt": 1782900000000, "lastStatus": "ok",
    "lastResult": "[INFO] 任务开始…"
  } ]
}`},{m:"POST",p:"/api/tasks",auth:"session",d:"新建定时任务（运行项目 或 获取 Code）",body:`{
  "name": "每早签到",
  "taskType": "project",              // project | code
  "projectId": 7,                      // taskType=project 必填
  "appid": "wx1234567890abcdef",       // taskType=code 必填
  "openids": ["oAAA", "oBBB"],         // 多账号批量
  "cron": "0 0 8 * * *",               // 6 段：秒 分 时 日 月 周
  "enabled": true,
  "params": {
    "proxyUrl": "",                    // 可选
    "sn": "",                          // 脉动：瓶盖码/SN
    "envName": "JD_COOKIE",            // 登录换Cookie类：自动提交环境变量名
    "panels": ["qinglong", "daidai"]   // 登录换Cookie类：自动提交目标面板
  }
}`,resp:`{
  "success": true,
  "task": { "id": 3, "name": "每早签到", "cron": "0 0 8 * * *", "cronText": "每天 08:00", "enabled": true, "nextRunAt": 1783000000000 }
}`},{m:"PUT",p:"/api/tasks/:id",auth:"session",d:"编辑定时任务（字段同新建）",body:`{
  "name": "每早签到",
  "taskType": "project",
  "projectId": 7,
  "openids": ["oAAA"],
  "cron": "0 0 7 * * *",
  "enabled": true,
  "params": {}
}`,resp:'{ "success": true, "task": { "id": 3, "cron": "0 0 7 * * *", "cronText": "每天 07:00" } }'},{m:"POST",p:"/api/tasks/:id/toggle",auth:"session",d:"启用 / 停用切换（停用后不再调度）",body:"",resp:'{ "success": true, "task": { "id": 3, "enabled": false, "nextRunAt": 0 } }'},{m:"POST",p:"/api/tasks/:id/run",auth:"session",d:"立即运行一次（不影响下次调度时间）",body:"",resp:`{
  "success": true,
  "result": { "status": "ok", "log": "[INFO] 任务开始…\\n[SUCCESS] …\\n[INFO] 任务结束：成功 2/2", "lastRunAt": 1782900000000 },
  "task": { "id": 3, "lastStatus": "ok" }
}`},{m:"DELETE",p:"/api/tasks/:id",auth:"session",d:"删除定时任务",body:"",resp:'{ "success": true }'}]},{title:"外部脚本兼容（wx_server 风格 · 机器令牌）",note:"供青龙等外部脚本调用，用授权码作机器令牌：请求头 X-License-Key: <授权码>，或在 body / query 传 auth=<授权码>。这些接口作用于已登录的账号（先用上面的扫码登录添加账号）。",items:[{m:"POST",p:"/wx/code",auth:"wx",d:"获取 wx.login code（wx_server 兼容格式）",body:`{
  "openid": "oXXXX",
  "appid": "wx1234567890abcdef"
}`,resp:`{
  "status": true,
  "success": true,
  "code": "0a1b2c…",
  "data": { "code": "0a1b2c…", "loginCode": "0a1b2c…" }
}`}]}],O={GET:"#2f9e44",POST:"#2f6bf6",PUT:"#e8912d",DELETE:"#e6534d"},X=_(!1),o=_(null);function S(s){o.value=s,X.value=!0}function A(s){let e="",i=!1,b=!1;for(let p=0;p<s.length;p++){const t=s[p];if(i){e+=t,b?b=!1:t==="\\"?b=!0:t==='"'&&(i=!1);continue}if(t==='"'){i=!0,e+=t;continue}if(t==="/"&&s[p+1]==="/"){for(;p<s.length&&s[p]!==`
`;)p++;continue}e+=t}return e}function P(s){const e=A(s);try{return JSON.stringify(JSON.parse(e))}catch{return e.replace(/\s*\n\s*/g," ").trim()}}function j(s){const e=["-H 'Content-Type: application/json'"];return s==="both"||s==="license"||s==="wx"?e.push("-H 'X-License-Key: <你的授权码>'"):s==="session"&&(e.push("-H 'X-Requested-With: XMLHttpRequest'"),e.push("-H 'Cookie: yyb_sid=<登录后的会话Cookie>'")),e}function v(s){if(!s)return"";const e=typeof window<"u"&&window.location&&window.location.origin||"https://你的域名",i=[`curl -X ${s.m} '${e}${s.p}'`,...j(s.auth)];return s.body&&i.push(`-d '${P(s.body)}'`),i.join(` \\
  `)}const d=_("");async function h(s,e=""){try{await navigator.clipboard.writeText(s),d.value=e,setTimeout(()=>{d.value===e&&(d.value="")},1500)}catch{}}return(s,e)=>{const i=k("el-button"),b=k("el-tag"),p=k("el-drawer");return l(),c("div",null,[e[11]||(e[11]=n("h2",{class:"ph"},"接口文档",-1)),e[12]||(e[12]=n("p",{class:"pd"},"点击任意接口查看请求头、请求体与响应示例。浏览器内接口凭登录会话自动鉴权；程序调用用授权码作机器令牌。",-1)),(l(),c(g,null,T(C,(t,N)=>n("div",{key:t.title,class:E(["card rise","rise-"+Math.min(N+1,4)]),style:{"margin-bottom":"16px"}},[n("div",I,[n("h3",null,a(t.title),1)]),t.note?(l(),c("div",L,a(t.note),1)):x("",!0),(l(!0),c(g,null,T(t.items,u=>(l(),c("div",{key:u.m+u.p,class:"ep",onClick:oe=>S(u)},[n("span",{class:"m",style:q({background:O[u.m]})},a(u.m),5),n("code",B,a(u.p),1),n("span",D,a(u.d),1),e[5]||(e[5]=n("span",{class:"arrow"},"›",-1))],8,R))),128))],2)),64)),m(p,{modelValue:X.value,"onUpdate:modelValue":e[4]||(e[4]=t=>X.value=t),title:o.value?o.value.m+" "+o.value.p:"",size:"560",direction:"rtl"},{default:y(()=>[o.value?(l(),c("div",z,[n("p",J,a(o.value.d),1),n("div",K,[n("div",G,[e[6]||(e[6]=r("完整 cURL（复制后替换 <你的授权码> 与 openid / appid 即可直接调用） ",-1)),m(i,{size:"small",type:"primary",onClick:e[0]||(e[0]=t=>h(v(o.value),"curl"))},{default:y(()=>[r(a(d.value==="curl"?"已复制 ✓":"复制 cURL"),1)]),_:1})]),n("pre",H,a(v(o.value)),1)]),n("div",$,[e[7]||(e[7]=n("div",{class:"slabel"},"鉴权方式",-1)),m(b,{size:"small",type:"info",effect:"light"},{default:y(()=>[r(a(w[o.value.auth]),1)]),_:1})]),n("div",V,[n("div",F,[e[8]||(e[8]=r("请求头 ",-1)),m(i,{size:"small",text:"",onClick:e[1]||(e[1]=t=>h(f(o.value.auth),"headers"))},{default:y(()=>[r(a(d.value==="headers"?"已复制 ✓":"复制"),1)]),_:1})]),n("pre",M,a(f(o.value.auth)),1)]),o.value.body?(l(),c("div",W,[n("div",Q,[e[9]||(e[9]=r("请求体 ",-1)),m(i,{size:"small",text:"",onClick:e[2]||(e[2]=t=>h(o.value.body,"body"))},{default:y(()=>[r(a(d.value==="body"?"已复制 ✓":"复制"),1)]),_:1})]),n("pre",Y,a(o.value.body),1)])):x("",!0),n("div",Z,[n("div",ee,[e[10]||(e[10]=r("响应示例 ",-1)),m(i,{size:"small",text:"",onClick:e[3]||(e[3]=t=>h(o.value.resp,"resp"))},{default:y(()=>[r(a(d.value==="resp"?"已复制 ✓":"复制"),1)]),_:1})]),n("pre",se,a(o.value.resp),1)])])):x("",!0)]),_:1},8,["modelValue","title"])])}}},ie=U(ne,[["__scopeId","data-v-0586aed3"]]);export{ie as default};
