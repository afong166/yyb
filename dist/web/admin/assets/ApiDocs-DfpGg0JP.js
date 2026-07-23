import{_ as R,c,a as t,F as X,x as k,b as m,d as v,r as _,e as f,o as p,t as i,j as T,A as U,n as $,f as u}from"./index-DiFPqeht.js";const q={class:"ghead"},z={key:0,class:"gnote"},D=["onClick"],G={class:"path"},N={class:"d"},H={key:0,class:"detail"},V={class:"dd"},A={class:"sec curlsec"},M={class:"slabel"},B={class:"code curl"},I={class:"sec"},J={class:"sec"},F={class:"slabel"},W={class:"code"},K={key:0,class:"sec"},Q={class:"slabel"},Y={class:"code"},Z={class:"sec"},ee={class:"slabel"},se={class:"code"},te={__name:"ApiDocs",setup(ne){function g(s){return s==="admin"?`Content-Type: application/json
X-Requested-With: XMLHttpRequest
Cookie: yyb_admin_sid=…   # 管理员登录后自动携带`:"Content-Type: application/json"}const w={admin:"管理员会话（Cookie）",none:"无"},E=[{title:"管理员认证",note:"首个管理员由后端启动时打印的「管理员令牌」引导：用户名 admin、密码=该令牌，登录后请立即改密。登录成功后下发独立的管理员会话 Cookie。",items:[{m:"POST",p:"/api/admin/login",auth:"none",d:"管理员登录",body:`{
  "username": "admin",
  "password": "<管理员令牌或已改的密码>"
}`,resp:`{
  "success": true,
  "admin": { "id": 1, "username": "admin" }
}`},{m:"POST",p:"/api/admin/logout",auth:"admin",d:"退出",body:"",resp:'{ "success": true }'},{m:"GET",p:"/api/admin/me",auth:"admin",d:"当前管理员",body:"",resp:'{ "success": true, "admin": { "id": 1, "username": "admin" } }'},{m:"POST",p:"/api/admin/change-password",auth:"admin",d:"修改管理员密码（至少 8 位）",body:`{
  "password": "newStrongPassword"
}`,resp:'{ "success": true }'}]},{title:"用户与授权码",items:[{m:"GET",p:"/api/admin/users?status=pending",auth:"admin",d:"用户列表（status 可选：pending/active/disabled）",body:"",resp:`{
  "success": true,
  "users": [ { "id": 3, "username": "newbie01", "status": "active", "license": { "key": "XXXX-…", "maxUsers": 2, "usedCount": 0 } } ]
}`},{m:"GET",p:"/api/admin/users/:id",auth:"admin",d:"用户详情：账号 + 授权码 + 绑定微信账号 + 调用记录",body:"",resp:`{
  "success": true,
  "user": { "id": 3, "username": "newbie01", "status": "active" },
  "license": { "key": "XXXX-…", "maxUsers": 2 },
  "wechatAccounts": [ … ],
  "callRecords": [ … ], "callCount": 12
}`},{m:"POST",p:"/api/admin/users/:id/approve",auth:"admin",d:"通过注册审核（置为 active）",body:"",resp:'{ "success": true, "user": { "id": 3, "status": "active" } }'},{m:"POST",p:"/api/admin/users/:id/disable",auth:"admin",d:"禁用用户",body:"",resp:'{ "success": true, "user": { "id": 3, "status": "disabled" } }'},{m:"POST",p:"/api/admin/users/:id/enable",auth:"admin",d:"启用用户",body:"",resp:'{ "success": true, "user": { "id": 3, "status": "active" } }'},{m:"POST",p:"/api/admin/users/:id/reset-password",auth:"admin",d:"重置用户密码",body:`{
  "password": "newpass123"
}`,resp:'{ "success": true }'},{m:"DELETE",p:"/api/admin/users/:id",auth:"admin",d:"删除用户（连带授权码/微信账号/调用记录）",body:"",resp:'{ "success": true }'},{m:"POST",p:"/api/admin/users/:id/authcode",auth:"admin",d:"发放 / 更新该用户授权码（配额=可绑定微信账号数）",body:`{
  "maxUsers": 3,
  "note": "vip",
  "expiresAt": 0   // 0=永久，或毫秒时间戳
}`,resp:`{
  "success": true,
  "license": { "key": "XXXX-XXXX-XXXX-XXXX", "maxUsers": 3, "status": "active" }
}`},{m:"POST",p:"/api/admin/users/:id/authcode/status",auth:"admin",d:"启用 / 禁用授权码",body:`{
  "status": "disabled"   // active | disabled
}`,resp:'{ "success": true, "license": { "status": "disabled" } }'},{m:"DELETE",p:"/api/admin/users/:id/authcode",auth:"admin",d:"删除该用户授权码",body:"",resp:'{ "success": true }'}]},{title:"项目管理",items:[{m:"GET",p:"/api/admin/projects",auth:"admin",d:"全部项目",body:"",resp:`{
  "success": true,
  "projects": [ { "id": 1, "name": "京东签到", "status": "on", "panelType": "qinglong" } ]
}`},{m:"POST",p:"/api/admin/projects",auth:"admin",d:"新增项目（runConfig 里 submitPanels=可提交的面板，envName=默认变量名）",body:`{
  "name": "京东 Code 登录获取 Cookie",
  "summary": "一键换取京东 Cookie",
  "intro": "## 简介",
  "tutorial": "## 使用教程",
  "status": "on",
  "runConfig": {
    "builtin": "jd-code-login",
    "appid": "wx73247c7819d61796",
    "submitPanels": ["qinglong", "daidai"],
    "envName": "JD_COOKIE"
  }
}`,resp:'{ "success": true, "project": { "id": 2, "name": "…", "submitPanels": ["qinglong","daidai"] } }'},{m:"PUT",p:"/api/admin/projects/:id",auth:"admin",d:"编辑项目",body:`{
  "summary": "更新后的简介",
  "tutorial": "更新后的教程"
}`,resp:'{ "success": true, "project": { … } }'},{m:"POST",p:"/api/admin/projects/:id/shelf",auth:"admin",d:"上架 / 下架",body:`{
  "on": true
}`,resp:'{ "success": true, "project": { "status": "on" } }'},{m:"DELETE",p:"/api/admin/projects/:id",auth:"admin",d:"删除项目",body:"",resp:'{ "success": true }'}]},{title:"监控与审计",items:[{m:"GET",p:"/api/admin/call-records?userId=&limit=200",auth:"admin",d:"调用记录（userId 可选筛选某用户）",body:"",resp:`{
  "success": true,
  "records": [ { "user_id": 2, "action": "get-code", "appid": "wx…", "result": "ok", "ms": 812, "created_at": 1782900000000 } ]
}`},{m:"GET",p:"/api/admin/audit?limit=200",auth:"admin",d:"管理员操作审计日志",body:"",resp:`{
  "success": true,
  "audit": [ { "admin_id": 1, "action": "license-issue", "target_type": "user", "target_id": "3" } ]
}`},{m:"GET",p:"/api/admin/stats",auth:"admin",d:"概览统计（含 MMTLS 会话池监控）",body:"",resp:`{
  "success": true,
  "users": { "total": 4, "pending": 0, "active": 4 },
  "licenses": 4,
  "recentCalls": 500,
  "sessionPool": {
    "cached": 3, "live": 2, "expired": 1, "ttlSeconds": 600,
    "totalRequests": 128, "hit0rtt": 96, "hitRelogin": 12, "rebuild": 20,
    "reuseRate": 0.844
  }
}`}]}],S={GET:"#2f9e44",POST:"#e6534d",PUT:"#e8912d",DELETE:"#c0392b"},h=_(!1),a=_(null);function x(s){a.value=s,h.value=!0}function P(s){let e="",d=!1,y=!1;for(let o=0;o<s.length;o++){const n=s[o];if(d){e+=n,y?y=!1:n==="\\"?y=!0:n==='"'&&(d=!1);continue}if(n==='"'){d=!0,e+=n;continue}if(n==="/"&&s[o+1]==="/"){for(;o<s.length&&s[o]!==`
`;)o++;continue}e+=n}return e}function O(s){const e=P(s);try{return JSON.stringify(JSON.parse(e))}catch{return e.replace(/\s*\n\s*/g," ").trim()}}function j(s){const e=["-H 'Content-Type: application/json'"];return s==="admin"&&(e.push("-H 'X-Requested-With: XMLHttpRequest'"),e.push("-H 'Cookie: yyb_admin_sid=<管理员会话Cookie>'")),e}function C(s){if(!s)return"";const e=typeof window<"u"&&window.location&&window.location.origin||"https://你的域名",d=[`curl -X ${s.m} '${e}${s.p}'`,...j(s.auth)];return s.body&&d.push(`-d '${O(s.body)}'`),d.join(` \\
  `)}const r=_("");async function b(s,e=""){try{await navigator.clipboard.writeText(s),r.value=e,setTimeout(()=>{r.value===e&&(r.value="")},1500)}catch{}}return(s,e)=>{const d=f("el-button"),y=f("el-tag"),o=f("el-drawer");return p(),c("div",null,[e[11]||(e[11]=t("h2",{class:"ph"},"接口文档",-1)),e[12]||(e[12]=t("p",{class:"pd"},"点击任意接口查看鉴权方式、请求头、请求体与响应示例。管理接口均需管理员会话 Cookie。",-1)),(p(),c(X,null,k(E,(n,L)=>t("div",{key:n.title,class:$(["card rise","rise-"+Math.min(L+1,3)]),style:{"margin-bottom":"16px"}},[t("div",q,[t("h3",null,i(n.title),1)]),n.note?(p(),c("div",z,i(n.note),1)):T("",!0),(p(!0),c(X,null,k(n.items,l=>(p(),c("div",{key:l.m+l.p,class:"ep",onClick:ae=>x(l)},[t("span",{class:"m",style:U({background:S[l.m]})},i(l.m),5),t("code",G,i(l.p),1),t("span",N,i(l.d),1),e[5]||(e[5]=t("span",{class:"arrow"},"›",-1))],8,D))),128))],2)),64)),m(o,{modelValue:h.value,"onUpdate:modelValue":e[4]||(e[4]=n=>h.value=n),title:a.value?a.value.m+" "+a.value.p:"",size:"560",direction:"rtl"},{default:v(()=>[a.value?(p(),c("div",H,[t("p",V,i(a.value.d),1),t("div",A,[t("div",M,[e[6]||(e[6]=u("完整 cURL（复制后替换 <管理员会话Cookie> 与 :id 等占位即可调用） ",-1)),m(d,{size:"small",type:"primary",onClick:e[0]||(e[0]=n=>b(C(a.value),"curl"))},{default:v(()=>[u(i(r.value==="curl"?"已复制 ✓":"复制 cURL"),1)]),_:1})]),t("pre",B,i(C(a.value)),1)]),t("div",I,[e[7]||(e[7]=t("div",{class:"slabel"},"鉴权方式",-1)),m(y,{size:"small",type:"info",effect:"light"},{default:v(()=>[u(i(w[a.value.auth]),1)]),_:1})]),t("div",J,[t("div",F,[e[8]||(e[8]=u("请求头 ",-1)),m(d,{size:"small",text:"",onClick:e[1]||(e[1]=n=>b(g(a.value.auth),"headers"))},{default:v(()=>[u(i(r.value==="headers"?"已复制 ✓":"复制"),1)]),_:1})]),t("pre",W,i(g(a.value.auth)),1)]),a.value.body?(p(),c("div",K,[t("div",Q,[e[9]||(e[9]=u("请求体 ",-1)),m(d,{size:"small",text:"",onClick:e[2]||(e[2]=n=>b(a.value.body,"body"))},{default:v(()=>[u(i(r.value==="body"?"已复制 ✓":"复制"),1)]),_:1})]),t("pre",Y,i(a.value.body),1)])):T("",!0),t("div",Z,[t("div",ee,[e[10]||(e[10]=u("响应示例 ",-1)),m(d,{size:"small",text:"",onClick:e[3]||(e[3]=n=>b(a.value.resp,"resp"))},{default:v(()=>[u(i(r.value==="resp"?"已复制 ✓":"复制"),1)]),_:1})]),t("pre",se,i(a.value.resp),1)])])):T("",!0)]),_:1},8,["modelValue","title"])])}}},de=R(te,[["__scopeId","data-v-6fc44d2c"]]);export{de as default};
