# agentctl, part `livepage`: the live pages' markup: the status page's and an agent's HTML, their CSS and the script streaming them
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.



# THE LIVE PANE (both pages): an EventSource on /stream renders every entry in order -- assistant text, thinking,
# each tool call with all of its input, each tool result whole (a long one collapsed, expanding in place), user,
# system and queued-message entries -- each with its time; follow (keep the newest in view) and pause (close the
# stream; resume reopens it from the last offset) toggles; the state chip from the stream's `state` events.
LIVE_CSS = r"""
.live{margin-top:6px;border:1px solid var(--line);border-radius:6px;background:var(--card)}
.live .bar{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;padding:4px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--card);z-index:1}
.live .log{max-height:600px;overflow:auto;padding:2px 8px 6px}.live.full .log{max-height:none;overflow:visible}
.live button,.agent button{font:inherit;font-size:12px;padding:1px 8px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);cursor:pointer}
.ent{padding:5px 0;border-bottom:1px dashed var(--line)}.ent:last-child{border-bottom:0}
.ent .ts{color:var(--mute);font:11px ui-monospace,Menlo,monospace;margin-right:6px}
.ent .txt{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:2px}.ent .lbl{color:var(--mute);font-size:11px;margin-top:3px}
.live pre{max-height:none;white-space:pre-wrap;overflow-wrap:anywhere}.ent.a .txt{font-size:13.5px}
.ent.th .txt{color:var(--mute);font-style:italic}
"""


LIVE_JS = r"""
const LIVE={},LIVE_OPEN=new Set(),LIVE_BIG=3000,LIVE_BIG_LINES=30;
const STATE_CLS={"working":"warn","waiting on background":"info","done":"ok","failed":"bad"};
function lvBlock(label,text){text=String(text==null?"":text);const n=text.split("\n").length;
 if(text.length>LIVE_BIG||n>LIVE_BIG_LINES)return`<details><summary>${label?E(label)+" ":""}<span class="mute">${n} lines, ${text.length} chars: click to expand</span></summary><pre>${E(text)}</pre></details>`;
 return(label?`<div class="lbl">${E(label)}</div>`:"")+`<pre>${E(text)}</pre>`}
function lvJson(v){return typeof v==="string"?v:JSON.stringify(v,null,2)}
function lvInput(i){if(!i||typeof i!=="object"||Array.isArray(i))return lvBlock("input",lvJson(i));
 const ks=Object.keys(i);if(!ks.length)return`<div class="lbl">input: {}</div>`;return ks.map(k=>lvBlock(k,lvJson(i[k]))).join("")}
function lvResult(c){if(typeof c==="string")return c;if(Array.isArray(c))return c.map(b=>b&&b.type==="text"?b.text:b&&b.type==="image"?`[image ${(b.source||{}).media_type||""}]`:lvJson(b)).join("\n");return lvJson(c)}
function lvEntry(e){const t=e.timestamp?new Date(e.timestamp):null;
 const ts=t&&!isNaN(t)?t.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}):"";
 const hd=(who,c)=>`<span class="ts" title="${E(e.timestamp||"")}">${E(ts)}</span>${chip(who,c)} `;
 const m=e.message||{};let h="";
 if(e.type==="assistant"){const cs=Array.isArray(m.content)?m.content:[{type:"text",text:lvJson(m.content)}];
  for(const b of cs){if(!b)continue;
   if(b.type==="text")h+=`<div class="ent a">${hd(e.isApiErrorMessage?"API error":"assistant",e.isApiErrorMessage?"bad":"ok")}<div class="txt">${E(b.text)}</div></div>`;
   else if(b.type==="thinking"||b.type==="redacted_thinking")h+=`<div class="ent th">${hd("thinking","info")}${b.thinking?`<div class="txt">${E(b.thinking)}</div>`:`<span class="mute">(its text is not kept in the transcript)</span>`}</div>`;
   else if(b.type==="tool_use"||b.type==="server_tool_use")h+=`<div class="ent tu">${hd("tool call","warn")}<b>${E(b.name)}</b> <span class="mute mono">${E(b.id||"")}</span>${lvInput(b.input)}</div>`;
   else h+=`<div class="ent">${hd(b.type||"block","info")}${lvBlock("",lvJson(b))}</div>`}
  return h}
 if(e.type==="user"){const who=e.isMeta?"user (meta)":"user";
  if(!Array.isArray(m.content))return`<div class="ent u">${hd(who,"info")}${lvBlock("",lvJson(m.content))}</div>`;
  for(const b of m.content){if(!b)continue;
   if(b.type==="tool_result")h+=`<div class="ent tr">${hd(b.is_error?"tool error":"tool result",b.is_error?"bad":"info")}<span class="mute mono">${E(b.tool_use_id||"")}</span>${lvBlock("",lvResult(b.content))}</div>`;
   else if(b.type==="text")h+=`<div class="ent u">${hd(who,"info")}${lvBlock("",b.text)}</div>`;
   else h+=`<div class="ent u">${hd(who+" · "+(b.type||"?"),"info")}${b.type==="image"?`<span class="mute">[image ${E((b.source||{}).media_type||"")}]</span>`:lvBlock("",lvJson(b))}</div>`}
  return h}
 if(e.type==="system")return`<div class="ent s">${hd("system"+(e.subtype?" · "+e.subtype:""),"warn")}${lvBlock("",e.content!=null?lvJson(e.content):lvJson(e))}</div>`;
 if(e.type==="queue-operation")return`<div class="ent q">${hd("message "+(e.operation||""),"info")}${e.content!=null?lvBlock("",lvJson(e.content)):""}</div>`;
 if(e.type==="attachment"){const a=e.attachment||{};return`<div class="ent at">${hd("attachment · "+(a.type||"?"),"info")}${lvBlock("",lvJson(a))}</div>`}
 if(e.type==="raw")return`<div class="ent">${hd("raw line","info")}${lvBlock("",e.text)}</div>`;
 if(e.type==="oversize")return`<div class="ent">${hd("oversize","bad")}<span class="mute">a ${E(e.bytes)}-byte line, over the stream's bound: read it in the transcript file</span></div>`;
 return`<div class="ent">${hd(e.type||"entry","info")}${lvBlock("",lvJson(e))}</div>`}
function lvScroller(p){return p.full?document.scrollingElement:p.log}
function lvFlush(p){p.raf=0;if(!p.pending)return;p.log.insertAdjacentHTML("beforeend",p.pending);p.pending="";
 p.el.querySelector(".cnt").textContent=p.n+" entries";if(p.follow){const s=lvScroller(p);s.scrollTop=s.scrollHeight}}
function lvAdd(p,html){p.pending+=html;if(!p.raf)p.raf=requestAnimationFrame(()=>lvFlush(p))}
function lvConn(p,txt,c){p.el.querySelector(".conn").innerHTML=chip(txt,c)}
function liveStart(p){if(p.es)return;
 const es=p.es=new EventSource(`/stream?agent=${encodeURIComponent(p.id)}&from=${p.offset}`);lvConn(p,"connecting","info");
 es.addEventListener("meta",ev=>{const m=JSON.parse(ev.data);p.el.querySelector(".src").textContent=tilde(m.transcript)});
 es.addEventListener("entry",ev=>{p.n++;lvAdd(p,lvEntry(JSON.parse(ev.data)));if(ev.lastEventId)p.offset=+ev.lastEventId});
 es.addEventListener("reset",ev=>{p.log.innerHTML="";p.pending="";p.n=0;p.offset=0;
  lvAdd(p,`<div class="ent">${chip("restarted","warn")} <span class="mute">the transcript was ${E(JSON.parse(ev.data).why)}: shown again from its start</span></div>`)});
 es.addEventListener("synced",ev=>{if(ev.lastEventId)p.offset=+ev.lastEventId;lvConn(p,"live","ok")});
 es.addEventListener("state",ev=>{const s=JSON.parse(ev.data);p.el.querySelector(".state").innerHTML=chip(s.state,STATE_CLS[s.state]||"info")+` <span class="mute">${E(s.why||"")}</span>`});
 es.onerror=()=>lvConn(p,es.readyState===2?"closed":"reconnecting","bad")}
function liveStop(p){if(p.es){p.es.close();p.es=null}lvConn(p,"paused","warn")}
function livePane(id,full){if(LIVE[id])return LIVE[id];const el=document.createElement("div");el.className="live"+(full?" full":"");
 el.innerHTML=`<div class="bar"><span class="conn"></span><span class="state"></span><label><input type="checkbox" class="fo" checked> follow</label><button class="pa">pause</button>${full?"":`<a href="/agent/${encodeURIComponent(id)}" target="_blank">full screen</a>`}<span class="mute cnt"></span><span class="mute mono src"></span></div><div class="log"></div>`;
 const p=LIVE[id]={id,el,full:!!full,log:el.querySelector(".log"),es:null,offset:0,n:0,pending:"",raf:0,follow:true,paused:false};
 el.querySelector(".fo").onchange=ev=>{p.follow=ev.target.checked;if(p.follow){const s=lvScroller(p);s.scrollTop=s.scrollHeight}};
 el.querySelector(".pa").onclick=ev=>{p.paused=!p.paused;ev.target.textContent=p.paused?"resume":"pause";p.paused?liveStop(p):liveStart(p)};
 liveStart(p);return p}
function liveClose(id){const p=LIVE[id];if(!p)return;liveStop(p);p.el.remove();delete LIVE[id]}
"""


AGENT_PAGE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent output</title>
<style>/*CSS*/</style></head><body><main>
<h1><a href="/">agentctl</a> · <span id="who" class="mono"></span></h1>
<div id="root"></div>
</main>
<script>
"use strict";
const E=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const chip=(s,c)=>`<span class="chip ${c||"info"}">${E(s)}</span>`;
let HOME="";const tilde=p=>{p=String(p||"");const m=p.match(/^\/Users\/[^\/]+/);return m?"~"+p.slice(m[0].length):p};
/*LIVE_JS*/
const ID=decodeURIComponent(location.pathname.replace(/^\/agent\//,""));
document.getElementById("who").textContent=ID;document.title=ID+" · agentctl";
document.getElementById("root").appendChild(livePane(ID,true).el);
</script></body></html>
"""


STATUS_PAGE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>agentctl status</title>
<style>
:root{--bg:#fbfbfa;--fg:#1d1f21;--mute:#6a6f75;--line:#e2e2df;--card:#fff;--link:#0b62c4;
--ok:#1e7a3b;--okb:#e3f3e7;--bad:#b42318;--badb:#fde8e6;--warn:#8a5a00;--warnb:#fdf1d8;--info:#1f4f8f;--infob:#e5eefa;--nb:#eeeeec}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--fg:#e4e4e2;--mute:#9a9ea4;--line:#2c2e31;--card:#1b1c1f;--link:#7fb2ff;
--ok:#7ad495;--okb:#18321f;--bad:#ff8f84;--badb:#3d1a17;--warn:#f2c46b;--warnb:#3a2d10;--info:#9cc2ff;--infob:#1a2840;--nb:#26282b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1500px;margin:0 auto;padding:12px 16px 40px}
h1{font-size:17px;margin:0 0 8px}h2{font-size:14px;margin:22px 0 6px;display:flex;gap:8px;align-items:baseline}
h2 small{color:var(--mute);font-weight:400}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.card .k{color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.04em}.card .v{font-size:14px;margin-top:2px}
.line{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin:8px 0;font-family:ui-monospace,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:6px}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
tr:last-child td{border-bottom:0}td.n{white-space:nowrap}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.mute{color:var(--mute)}.chip{display:inline-block;padding:0 7px;border-radius:9px;font-size:11px;font-weight:600;white-space:nowrap;background:var(--nb)}
.ok{background:var(--okb);color:var(--ok)}.bad{background:var(--badb);color:var(--bad)}.warn{background:var(--warnb);color:var(--warn)}.info{background:var(--infob);color:var(--info)}
.err{background:var(--badb);color:var(--bad);border-radius:6px;padding:6px 10px}
.agent{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:8px}
.agent .top{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline}.agent .said{margin:6px 0;white-space:pre-wrap;overflow-wrap:anywhere}
details{margin-top:4px}summary{cursor:pointer;color:var(--mute)}pre{margin:4px 0;padding:6px 8px;background:var(--bg);border:1px solid var(--line);border-radius:4px;overflow-x:auto;font-size:11.5px;max-height:420px}
ul.c{margin:2px 0;padding-left:16px}#stamp{color:var(--mute);font-weight:400;font-size:12px}
/*LIVE_CSS*/
</style></head><body><main>
<h1>agentctl <span id="stamp">loading…</span></h1>
<div id="root"></div>
</main>
<script>
"use strict";
const E=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let NOW=Date.now()/1000;
function ago(t){if(t==null||t==="")return"–";if(typeof t==="string"){const p=Date.parse(t);if(isNaN(p))return E(t);t=p/1000}
 const s=Math.round(NOW-t),a=Math.abs(s),f=a<90?a+"s":a<5400?Math.round(a/60)+"m":a<172800?(a/3600).toFixed(1)+"h":Math.round(a/86400)+"d";return s>=0?f+" ago":"in "+f}
function hm(t){if(!t)return"–";const d=new Date(t*1000);return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}
function chipClass(s){s=String(s||"").toLowerCase();
 if(/^(success|green|ok|on|live|alive|landed|verified|cleared|checked|yes)/.test(s))return"ok";
 if(/(failure|timed_out|red|offered|escalat|dead|off|unreachable|error|rejected|blocked|stalled|no$)/.test(s))return"bad";
 if(/(leased|fix-pending|checking|pushing|queued|waiting|held|assembling|bisecting|verif|running|pending|warn)/.test(s))return"warn";return"info"}
const chip=(s,c)=>`<span class="chip ${c||chipClass(s)}">${E(s)}</span>`;
const link=(u,t)=>u?`<a href="${E(u)}" target="_blank" rel="noopener">${E(t)}</a>`:E(t);
const tilde=p=>{p=String(p||"");return HOME&&p.startsWith(HOME)?"~"+p.slice(HOME.length):p};
const logLink=p=>`<a href="/log?path=${encodeURIComponent(p)}" target="_blank">full log</a>`;
let HOME="";
let STALE={};
/*LIVE_JS*/
function sec(title,sub,data,fn,key){const st=key&&STALE[key];let h=`<h2>${E(title)} <small>${sub||""}</small>${st?` <span class="chip warn">${E(st)}</span>`:""}</h2>`;
 if(data&&data.error)return h+`<div class="err">${E(data.error)}</div>`;try{return h+fn(data)}catch(e){return h+`<div class="err">render: ${E(e)}</div>`}}
function table(cols,rows){if(!rows.length)return`<div class="mute">none</div>`;
 return`<div class="tw"><table><thead><tr>${cols.map(c=>`<th>${E(c)}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`}
function header(s){const h=s.header||{},st=s.store||{},dp=s.dispatcher||{};let o="";
 if(h.error)o+=`<div class="err">header: ${E(h.error)}</div>`;
 const tickAge=st.tick_age!=null?st.tick_age:h.tick_age, tickSrc=st.tick_age!=null?"remote store":"state.json";
 const gate=h.gate||[];const gateTxt=h.error?"?":(h.gate_red?"RED":(gate.length?"GREEN":"?"));
 const net=h.network&&h.network.at&&(NOW-h.network.at)<600?`lossy (${Math.round((h.network.loss||0)*100)}% loss)`:"ok";
 const tun=h.tunnel||{};const tunTxt=h.store_port===false?"DOWN (:3014 refused)":tun.escalated?"escalated":(tun.fails?`${tun.fails} fails`:"ok");
 const disp=dp.error?"?":(dp.blocker?"OFF":"ON");
 o+=`<div class="cards">
 <div class="card"><div class="k">gate</div><div class="v">${chip(gateTxt)} ${gate.map(g=>`${E(g.name)} ${link(g.url,"#"+(g.number??"?"))} ${chip(g.conclusion||"?")}${g.newer?` <span class="mute">→ ${link(g.newer_url,"#"+g.newer)} ${E(g.newer_status)}</span>`:""}`).join("<br>")}</div></div>
 <div class="card"><div class="k">reconciler tick</div><div class="v">${tickAge==null?chip("never","bad"):chip(Math.round(tickAge)+" s ago",tickAge>120?"bad":"ok")} <span class="mute">${tickSrc}</span></div></div>
 <div class="card"><div class="k">store</div><div class="v">${st.reachable?chip("reachable","ok"):chip("unreachable","bad")} <span class="mute mono">${E(st.remote||"local")}${st.ms!=null?" · "+st.ms+" ms":""}</span>${st.error?`<div class="mute">${E(st.error)}</div>`:""}</div></div>
 <div class="card"><div class="k">network / tunnel</div><div class="v">${chip("net "+net,net==="ok"?"ok":"bad")} ${chip("tunnel "+tunTxt,tunTxt==="ok"?"ok":"bad")}</div></div>
 <div class="card"><div class="k">dispatcher · fixers</div><div class="v">${chip(disp)} fixers ${E(dp.alive??"?")}/${E(dp.cap??"?")}${dp.blocker?`<div class="mute">${E(dp.blocker)}</div>`:""}${dp.error?`<div class="mute">${E(dp.error)}</div>`:""}</div></div>
 <div class="card"><div class="k">waiting · checking</div><div class="v">${(h.waiting||[]).length} · ${(h.queued||[]).length} <span class="mute">code ${E(h.code||"?")}</span></div></div>
 </div>`;
 if(h.line)o+=`<div class="line">${E(h.line)}</div>`;
 if(dp.escalated&&dp.escalated.length)o+=`<div class="err">ESCALATED: ${dp.escalated.map(x=>E(x.type+" "+x.id+": "+x.words)).join("<br>")}</div>`;
 return o}
function reds(rs){return table(["state","red","run","failing jobs","age","holder","deadline / lapse","last note"],rs.map(r=>{
 const l=r.lease;const holder=l&&l.live?`${E(l.holder)}${l.worktree?`<div class="mute mono">${E(tilde(l.worktree))}</div>`:""}`:`<span class="mute">–</span>`;
 const lapse=l&&l.live?`lapses ${hm(l.expires_at)} (${ago(l.expires_at)})<div class="mute">max ${hm(l.max_until)}</div>`:"";
 return[chip(r.state)+(r.level?" "+chip("escalated","bad"):"")+`<div class="mute">${E(r.short)}</div>`,
  `${E(r.workflow)} ${r.gate?chip("gate","bad"):chip(r.red_kind||"other","info")}${r.family?" "+chip("family "+r.family,"info"):""}<div class="mute mono">${E(r.key)} · ${E(r.tier||"")} ${E(r.kind||"")}</div>`,
  `<span class="n">${link(r.run_url,"#"+(r.run??"?"))}</span>`,
  E((r.jobs||[]).join(", "))+(r.line?`<div class="mute mono">${E(String(r.line).slice(0,200))}</div>`:""),
  `<span class="n">${ago(r.opened_at)}</span>`,holder,
  `<span class="n">${hm(r.deadline)} (${ago(r.deadline)})</span>${lapse}`,
  `<div>${E(r.note||r.text||"")}</div>`]}))}
function agents(as){if(!as.length)return`<div class="mute">none</div>`;return as.map(a=>{
 const ls=(a.leases||[]);const lease=ls.map(l=>`${E(l.name)} ${chip(l.kind||"lease")} renewed ${ago(l.renewed_at)}, lapses ${hm(l.expires_at)}${l.note?` — ${E(l.note)}`:""}`).join("<br>");
 const commits=(a.commits||[]).map(c=>`<li><span class="mono">${E(c.sha)}</span> <span class="mute">${E(c.when)}</span> ${E(c.subject)}</li>`).join("");
 const files=[...(a.dirty||[]).map(x=>"(uncommitted) "+x),...(a.recent_files||[])];
 const logs=(a.logs||[]).map(g=>`<details data-k="${E(g.path)}"><summary>${E(g.name)} <span class="mute">${g.size} B, ${ago(g.mtime)}</span> · ${logLink(g.path)}</summary><pre>${E(g.tail)}</pre></details>`).join("");
 return`<div class="agent"><div class="top"><b>${E(a.label||a.holder||("pid "+a.pid))}</b>
 ${a.alive===true?chip("alive","ok"):a.alive===false?chip("dead"+(a.rc!=null?" rc "+a.rc:""),"bad"):chip("?")}
 <span>pid <span class="mono">${E(a.pid??"–")}</span></span><span>${E(a.kind||"")}${a.tier?" · tier "+E(a.tier):""}</span>
 <span>${E(a.workflow||"")} ${a.run!=null?"#"+E(a.run):""}</span><span class="mono mute">${E(tilde(a.worktree))}</span>
 <span>started ${ago(a.started)}</span>${a.ended?`<span>ended ${ago(a.ended)}</span>`:""}<span>last progress ${ago(a.last_progress)}</span>
 ${a.live?`<span>${chip(a.live_state||"?",STATE_CLS[a.live_state]||"info")} <button data-live-btn="${E(a.live)}">${LIVE_OPEN.has(a.live)?"hide live output":"live output"}</button> <a href="/agent/${encodeURIComponent(a.live)}" target="_blank">full screen</a></span>`:""}</div>
 ${lease?`<div class="mute">${lease}</div>`:""}
 ${a.verdict?`<div class="said"><b>verdict:</b> ${E(a.verdict)}</div>`:""}
 ${a.said?`<div class="said"><span class="mute">last said ${ago(a.said_at)}:</span> ${E(a.said)}</div>`:""}
 ${commits?`<ul class="c">${commits}</ul>`:""}
 ${files.length?`<details data-k="files:${E(a.pid)}:${E(a.worktree)}"><summary>${files.length} files changed recently</summary><pre>${E(files.join("\n"))}</pre></details>`:""}
 ${a.git_error?`<div class="err">git: ${E(a.git_error)}</div>`:""}${a.git_stale?`<div class="mute">${E(a.git_stale)}</div>`:""}
 ${a.detail_error?`<div class="err">${E(a.detail_error)}</div>`:""}${logs}${a.live?`<div class="livehost" data-live="${E(a.live)}"></div>`:""}</div>`}).join("")}
function landing(l){const row=c=>[`<span class="mono">${E(c.id)}</span>`,chip(c.state)+(c.level?" "+chip("escalated","bad"):""),
 E(tilde(c.worktree||""))+(c.branch?` <span class="mute">${E(c.branch)}</span>`:""),`<span class="mono">${E(c.head||"")}</span>`,
 `<span class="n">${ago(c.entered_at)}</span>`,`<span class="n">${c.past_deadline?chip("past "+hm(c.deadline),"bad"):hm(c.deadline)+" ("+ago(c.deadline)+")"}</span>`,E(c.why||"")];
 const cols=["id","state","worktree","head","in state","deadline","why"];
 const hold=l.hold?`<div class="err">land HELD until ${hm(l.hold.until)} by ${E(l.hold.by||"?")}${l.hold.why?": "+E(l.hold.why):""} (<span class="mono">agentctl land release</span>; it expires by itself then)</div>`:"";
 return`${hold}<div class="mute">batch</div>${table(cols,(l.batch||[]).map(row))}<div class="mute" style="margin-top:6px">changes</div>${table(cols,(l.changes||[]).map(row))}`}
function conds(cs){return table(["condition","since","probe","last result","next check","text"],cs.map(c=>[
 `${chip(c.kind,"bad")}${c.level?" "+chip("escalated","bad"):""}<div class="mute mono">${E(c.key)}</div>`,`<span class="n">${E(c.opened)}</span>`,
 `${E(c.probe)}<div class="mute">clears when ${E(c.until)}</div>`,`${c.ok==null?"cannot tell":"fails"} (${E(c.detail)}) · fails ${E(c.fails||0)}`,
 `<span class="n">${ago(c.next_check)}</span>`,E(c.text)]))}
function issues(is){return table(["issue","filed","by","text"],is.map(i=>[`<span class="mono">${E(i.id)}</span>${i.level?" "+chip("escalated","bad"):""}`,
 `<span class="n">${ago(i.opened)}</span>`,E(i.by||"?"),`<div>${E(i.text)}</div>${i.cwd?`<div class="mute">${E(tilde(i.cwd))}</div>`:""}`]))}
function events(es){return table(["#","when","kind","item","→","text"],es.map(e=>[`<span class="mono">${E(e.seq)}</span>`,`<span class="n">${hm(e.at)} <span class="mute">${ago(e.at)}</span></span>`,
 chip(e.kind,e.severity==="info"?"info":e.severity==="escalate"?"bad":"warn"),`<span class="mono">${E(e.item||"")}</span>`,e.to?`${E(e.from||"")} → ${chip(e.to)}`:"",E(e.text)]))}
function render(s){NOW=s.at;HOME=s.home||"";STALE=s.stale||{};const n=x=>Array.isArray(x)?x.length:"?";
 const tops={};for(const id in LIVE)tops[id]=LIVE[id].log.scrollTop;  // the open panes move to the new rows as they are
 document.getElementById("root").innerHTML=header(s)
 +sec("Reds",n(s.reds)+" open",s.reds,reds,"reds")
 +sec("Agents",n(s.agents)+" (lease holders and fixers alive or ended in the last 3 h)",s.agents,agents,"agents")
 +sec("Waiting / landing","",s.landing,landing,"landing")
 +sec("Open conditions",n(s.conditions),s.conditions,conds,"conditions")
 +sec("agentctl issues",n(s.issues)+" open (`agentctl issue`)",s.issues,issues,"issues")
 +sec("Events","last "+n(s.events),s.events,events,"events");
 document.querySelectorAll("details[data-k]").forEach(d=>{if(OPEN.has(d.dataset.k))d.open=true});
 const hosts=new Set();document.querySelectorAll(".livehost[data-live]").forEach(h=>{const id=h.dataset.live;hosts.add(id);
  if(LIVE_OPEN.has(id)){h.appendChild(livePane(id).el);if(tops[id]!=null)LIVE[id].log.scrollTop=tops[id]}});
 for(const id in LIVE)if(!hosts.has(id)){liveClose(id);LIVE_OPEN.delete(id)}  // its agent left the page
 document.getElementById("stamp").textContent="· "+new Date(s.at*1000).toLocaleTimeString()+" · refreshes every 15 s"}
async function load(){try{const r=await fetch("/api/status",{cache:"no-store"});if(!r.ok)throw new Error("HTTP "+r.status);render(await r.json())}
 catch(e){document.getElementById("stamp").textContent="· refresh failed: "+e+" (retrying)"}}
const OPEN=new Set();  // the <details> the reader opened stay open across refreshes
document.addEventListener("toggle",e=>{const k=e.target.dataset&&e.target.dataset.k;if(!k)return;e.target.open?OPEN.add(k):OPEN.delete(k)},true);
document.addEventListener("click",e=>{const b=e.target.closest&&e.target.closest("button[data-live-btn]");if(!b)return;
 const id=b.dataset.liveBtn,h=document.querySelector(`.livehost[data-live="${CSS.escape(id)}"]`);
 if(LIVE_OPEN.has(id)){LIVE_OPEN.delete(id);liveClose(id);b.textContent="live output"}
 else{LIVE_OPEN.add(id);if(h)h.appendChild(livePane(id).el);b.textContent="hide live output"}});
load();setInterval(load,15000);
</script></body></html>
"""


STATUS_PAGE_HTML = STATUS_PAGE_HTML.replace("/*LIVE_CSS*/", LIVE_CSS).replace("/*LIVE_JS*/", LIVE_JS)


AGENT_PAGE_HTML = AGENT_PAGE_HTML.replace("/*CSS*/", STATUS_PAGE_HTML.split("<style>", 1)[1].split("</style>", 1)[0]) \
    .replace("/*LIVE_JS*/", LIVE_JS)
