// EdgeLab: recorded evidence, local experiments and synchronized exploration. No equipment commands.
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { html } from '../lib/ui.js';
import { num, int, dur, clock, DASH } from '../lib/fmt.js';
import { LabTools } from './lab_tools.js';

const TABS = [['observe','Наблюдение'],['decisions','Решения'],['experiments','Эксперименты'],['analysis','Инструменты'],['journal','Журнал']];
const ACTIVE = new Set(['queued','pending','running','starting','cancelling']);
const JOB_STATE = {queued:'В очереди',pending:'В очереди',running:'Выполняется',starting:'Запуск',cancelling:'Остановка',cancelled:'Остановлен',done:'Завершён',completed:'Завершён',succeeded:'Завершён',failed:'Ошибка',error:'Ошибка'};
const TYPES = {input:'Вход',wake:'Пробуждение',suppress:'Подавление',llm_request:'Запрос LLM',llm_response:'Ответ LLM',tool_call:'Вызов инструмента',tool_result:'Результат инструмента',user_reply:'Ответ пользователю',notify:'Уведомление',rule_fired:'Локальное правило',context_snapshot:'Снимок контекста',run_start:'Начало',run_end:'Конец',error:'Ошибка',deferred:'Отложено',llm_unavailable:'LLM недоступна',llm_available:'LLM восстановлена'};
const LANES = [{id:'input',label:'Наблюдения',color:'cyan'},{id:'gate',label:'Гейт / правила',color:'lime'},{id:'llm',label:'Модель',color:'orange'},{id:'tool',label:'Инструменты',color:'violet'},{id:'other',label:'Система',color:'muted'}];
const laneOf = (e) => e.type==='input'?'input':['wake','suppress','rule_fired','deferred'].includes(e.type)?'gate':e.type?.startsWith('llm_')?'llm':e.type?.startsWith('tool_')?'tool':'other';
const timed = e => Number.isFinite(e?.t);
const valueText = v => v===undefined?DASH:v===null?'null':typeof v==='boolean'?String(v):typeof v==='number'?num(v,4):typeof v==='object'?(v.mimeType&&v.data?'['+v.mimeType+' · записанные данные]':JSON.stringify(v)):String(v);
const brief = (v,n=120) => {const s=valueText(v);return s.length>n?s.slice(0,n)+'…':s;};
const timeText = (t,r) => !Number.isFinite(t)?DASH:r?.clock==='virtual'?'T'+dur(t-(r.t_start||0),true):clock(t);
const titleOf = e => e ? TYPES[e.type]||e.type||'Событие' : 'Выберите событие';
const eventDetail = e => e?.reason||e?.name||e?.error||e?.text||e?.kind||e?.request||'';
const eventCount = r => r?.events?.length||0;
const sourceName = r => r?.kind==='live'?'Живая трасса агента':r?.kind==='experiment'?'Локальный эксперимент':'Импортированная трасса';
const estimated = r => r?.metrics?.tokens_basis==='mock_usage_estimate';
function capturedRun() {try {const id=sessionStorage.getItem('edge-lab-open-run')||'';sessionStorage.removeItem('edge-lab-open-run');return id;}catch{return '';}}

async function request(path,{body,meta,signal}={}) {
  const controller=new AbortController(),abort=()=>controller.abort();
  signal?.addEventListener('abort',abort,{once:true});
  const timer=setTimeout(abort,20000);
  try {
    const r=await fetch('api/lab'+path,{method:body===undefined?'GET':'POST',cache:'no-store',signal:controller.signal,
      headers:body===undefined?{}:{'Content-Type':'application/json',...(meta?.csrf?{'X-Neurobot-CSRF':meta.csrf}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});
    const d=await r.json();if(!r.ok||d.ok===false)throw Error(d.error||d.message||'HTTP '+r.status);return d;
  } finally {clearTimeout(timer);signal?.removeEventListener('abort',abort);}
}

function useIndex() {
  const [doc,setDoc]=useState({runs:[],jobs:[],scenarios:[],limits:{}}),[error,setError]=useState(''),[revision,setRevision]=useState(0);
  useEffect(()=>{
    let alive=true,timer;const controller=new AbortController();
    const poll=async()=>{
      let wait=8000;
      try {const d=await request('',{signal:controller.signal});if(!alive)return;setDoc(d);setError('');if((d.jobs||[]).some(j=>ACTIVE.has(j.status))||(d.watchers||[]).some(w=>w.status!=='closed'))wait=1200;}
      catch(e){if(alive)setError(e.name==='AbortError'?'Лаборатория не ответила вовремя':e.message);wait=4000;}
      if(alive)timer=setTimeout(poll,wait);
    };poll();return()=>{alive=false;clearTimeout(timer);controller.abort();};
  },[revision]);
  return {doc,error,refresh:()=>setRevision(n=>n+1)};
}

function Glyph({name='orbit',size=20}) {
  const paths={orbit:'M12 2v4M12 18v4M2 12h4M18 12h4M19 12a7 7 0 1 1-14 0 7 7 0 0 1 14 0M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
    play:'M8 4l12 8-12 8Z',pause:'M8 5v14M16 5v14',prev:'M6 5v14M19 5 8 12l11 7Z',next:'M18 5v14M5 5l11 7-11 7Z',upload:'M12 16V3M7 8l5-5 5 5M4 15v6h16v-6',arrow:'M4 12h16M14 6l6 6-6 6',close:'M5 5l14 14M19 5 5 19',layers:'M3 8l9-5 9 5-9 5ZM3 12l9 5 9-5M3 16l9 5 9-5',link:'M9 15 15 9M8 17l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M16 7l1-1a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',search:'M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0M15 15l6 6',beaker:'M8 3h8M9 3v7L4 19a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2l-5-9V3M7 15h10'};
  return html`<svg class="lab-glyph" width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${paths[name]||paths.orbit} /></svg>`;
}

function Timeline({run,cursor,onSeek,selected,onSelect,zoom,setZoom,playing,setPlaying,speed,setSpeed,loop,setLoop}) {
  const events=useMemo(()=>run.events.filter(timed).sort((a,b)=>a.t-b.t),[run]);
  const start=Number.isFinite(run.t_start)?run.t_start:events[0]?.t||0,end=Math.max(start+.001,Number.isFinite(run.t_end)?run.t_end:events.at(-1)?.t||start+1);
  const full=end-start,span=zoom==='overview'?full:Math.min(full,zoom==='episode'?60:10);
  const a=Math.min(Math.max(start,cursor-span*.45),Math.max(start,end-span)),b=a+span;
  const X=t=>142+(t-a)/Math.max(.001,b-a)*822;
  const bins=new Map();
  for(const e of events){if(e.t<a||e.t>b)continue;const lane=laneOf(e),key=lane+':'+Math.floor((e.t-a)/Math.max(.001,b-a)*205);const bin=bins.get(key);if(bin)bin.events.push(e);else bins.set(key,{lane,t:e.t,events:[e]});}
  const seekStep=direction=>{const e=direction<0?[...events].reverse().find(e=>e.t<cursor-.001):events.find(e=>e.t>cursor+.001);if(e){onSelect(e);onSeek(e.t);}};
  return html`<section class="lab-timebench" data-testid="lab-timeline">
    <div class="lab-time-head"><div><span class="lab-overline">ОДНИ ЧАСЫ · ВСЕ СВИДЕТЕЛЬСТВА</span><h2>Лента эксперимента<span>${int(events.length)} событий</span></h2></div><div class="lab-zoom" role="group" aria-label="Масштаб времени">${[['overview','Обзор'],['episode','Эпизод'],['detail','Деталь']].map(([id,label])=>html`<button aria-pressed=${zoom===id} onClick=${()=>setZoom(id)}>${label}</button>`)}</div></div>
    <div class="lab-pan-hint">↔ Прокрутите ленту по горизонтали · названия дорожек закреплены</div><div class="lab-timeline-wrap"><div class="lab-timeline-scroll"><svg viewBox="0 0 1000 244" preserveAspectRatio="none" class="lab-timeline-svg" role="img" aria-label="Синхронная лента наблюдений, решений и вызовов" onClick=${e=>{if(e.target.closest('.lab-event-mark'))return;const box=e.currentTarget.getBoundingClientRect(),x=(e.clientX-box.left)/box.width*1000;if(x>=142)onSeek(Math.max(a,Math.min(b,a+(x-142)/822*(b-a))));}}>
      ${Array.from({length:7},(_,i)=>{const t=a+(b-a)*i/6;return html`<g><line x1=${X(t)} x2=${X(t)} y1="28" y2="220" class="lab-time-grid"/><text x=${X(t)} y="18" text-anchor="middle" class="lab-time-tick">${run.clock==='virtual'?dur(t-start):clock(t)}</text></g>`;})}
      ${LANES.map((lane,i)=>html`<g><text x="16" y=${53+i*35} class="lab-lane-name">${lane.label}</text><line x1="142" x2="964" y1=${49+i*35} y2=${49+i*35} class="lab-lane-line"/><rect x="127" y=${46+i*35} width="4" height="4" rx="1" class=${'lab-ink-'+lane.color} /></g>`)}
      ${[...bins.values()].map(bin=>{const y=49+LANES.findIndex(l=>l.id===bin.lane)*35,chosen=bin.events.some(e=>e.id===selected?.id),count=bin.events.length;return html`<g class=${'lab-event-mark lab-ink-'+LANES.find(l=>l.id===bin.lane).color+(chosen?' chosen':'')} onClick=${()=>{const e=bin.events[0];onSelect(e);onSeek(e.t);if(count>1&&zoom==='overview')setZoom('episode');}}><title>${count>1?count+' событий · ':''}${titleOf(bin.events[0])} · ${timeText(bin.t,run)}</title><rect x=${X(bin.t)-6} y=${y-12} width="12" height="24" class="lab-mark-hit"/><circle cx=${X(bin.t)} cy=${y} r=${Math.min(6,2.4+Math.log2(count)*.7)} />${chosen?html`<circle class="lab-mark-selected" cx=${X(bin.t)} cy=${y} r="9" />`:''}</g>`;})}
      ${cursor>=a&&cursor<=b?html`<g class="lab-playhead"><line x1=${X(cursor)} x2=${X(cursor)} y1="27" y2="218"/><path d=${`M${X(cursor)-4} 25h8l-4 5Z`} /></g>`:''}
      <rect x="142" y="211" width="822" height="23" class="lab-time-seek-zone" onClick=${e=>{const box=e.currentTarget.ownerSVGElement.getBoundingClientRect();onSeek(Math.max(a,Math.min(b,a+((e.clientX-box.left)/box.width*1000-142)/822*(b-a))));}} />
    </svg></div><div class="lab-timeline-labels" aria-hidden="true">${LANES.map((l,i)=>html`<span style=${{top:(49+i*35)/244*100+'%'}}><i class=${'lab-ink-'+l.color}></i>${l.label}</span>`)}</div></div>
    <div class="lab-transport"><div class="lab-transport-buttons"><button onClick=${()=>seekStep(-1)} aria-label="Предыдущее событие"><${Glyph} name="prev" size=${15}/></button><button class="lab-play" aria-label=${playing?'Пауза':'Проиграть запись'} aria-pressed=${playing} onClick=${()=>{if(cursor>=end)onSeek(start);setPlaying(!playing);}}><${Glyph} name=${playing?'pause':'play'} size=${15}/></button><button onClick=${()=>seekStep(1)} aria-label="Следующее событие"><${Glyph} name="next" size=${15}/></button></div><b class="lab-cursor" data-testid="lab-cursor">${timeText(cursor,run)}</b><input class="lab-scrubber" type="range" min=${start} max=${end} step=${Math.max(.001,full/10000)} value=${cursor} onInput=${e=>onSeek(Number(e.target.value))} aria-label="Момент эксперимента"/><select class="lab-speed" aria-label="Скорость просмотра" value=${speed} onChange=${e=>setSpeed(Number(e.target.value))}>${[1,4,16].map(s=>html`<option value=${s}>×${s}</option>`)}</select><button class="lab-loop" aria-pressed=${loop} title="Повтор текущего масштаба: весь прогон, 60 или 10 секунд" onClick=${()=>setLoop(!loop)}>↻ <span>Повтор</span></button><span class="lab-clock-note">${run.clock==='virtual'?'виртуальное время':'часы трассы'}</span></div>
  </section>`;
}

function readingRows(observations,cursor,count) {
  const map=new Map();const source=count==null?observations:observations.slice(0,count);
  for(const o of source){if(!Number.isFinite(o.t)||o.t>cursor)continue;map.set(JSON.stringify([o.device,o.property]),o);}
  return [...map.values()];
}

function scenarioReadings(run,cursor) {
  const truth=run.scenario_truth;if(!truth)return null;
  const rows=[];const header=truth.header||{};
  for(const [device,props] of Object.entries(header.initial_state||{}))for(const [property,value] of Object.entries(props))rows.push({device,property,value,t:run.t_start,origin:truth.source,unit:(header.devices||[]).find(d=>d.id===device)?.properties?.[property]?.unit});
  for(const e of truth.events||[])if(e.kind==='sensor'&&Number.isFinite(e.t)&&e.t<=cursor)rows.push({...e,unit:e.unit||(header.devices||[]).find(d=>d.id===e.device)?.properties?.[e.property]?.unit});
  return readingRows(rows,cursor);
}

function ContextSnapshot({snapshot}) {
  return html`<div class="lab-context-authentic"><span>${snapshot?.complete===false?'ЗАПИСАННЫЙ СНИМОК · НЕПОЛНЫЙ':'ЗАПИСАННЫЙ СНИМОК'}</span>${Array.isArray(snapshot?.messages)?html`<div class="lab-context-messages">${snapshot.messages.map((m,i)=>html`<section><header><b>${m.role||'message'}</b><small>0${i+1}</small></header><pre>${typeof m.content==='string'?m.content:JSON.stringify(m.content,null,2)}</pre></section>`)}</div><details><summary>Схемы инструментов · ${snapshot.tools?.length||0}</summary><pre>${JSON.stringify(snapshot.tools||[],null,2)}</pre></details><div class="lab-context-hash">${snapshot.sha256?'SHA256 '+snapshot.sha256.slice(0,20)+'…':''}</div>`:html`<pre>${typeof snapshot==='string'?snapshot:JSON.stringify(snapshot,null,2)}</pre>`}</div>`;
}

function Readings({rows,run,onEvent,empty='В этой части трассы измерений нет'}) {
  return rows.length?html`<div class="lab-readings">${rows.slice(0,60).map(o=>html`<button class=${'lab-reading'+(o.value===null?' unknown':'')} onClick=${()=>onEvent(o.event_id)} title=${JSON.stringify(o)}><span class="lab-reading-key"><b>${o.device||'Источник'}</b><small>${o.property||'значение'}</small></span><span class="lab-reading-value">${brief(o.value,60)}<small>${o.unit||''}</small></span><span class="lab-reading-time">${timeText(o.t,run)}</span></button>`)}${rows.length>60?html`<div class="lab-inline-note">Показаны 60 из ${rows.length} полей; полная трасса доступна в журнале.</div>`:''}</div>`:html`<div class="lab-empty-inline"><${Glyph} name="layers" size=${26}/><span>${empty}</span></div>`;
}

function PromptSegments({event}) {
  const estimates=event?.segments_tokens_est,raw=estimates||event?.segments;
  const entries=Object.entries(raw||{}).filter(([,v])=>typeof v==='number'&&Number.isFinite(v)&&v>=0),total=entries.reduce((n,[,v])=>n+v,0);
  const labels={prefix:'Префикс',profile:'Профиль',history:'История',state:'Состояние',turn:'Ход'};
  return html`<section class="lab-prompt"><div class="lab-subhead"><h3>Из чего собран запрос</h3><span>${estimates?'оценка токенов':'символы из трассы'}</span></div>${entries.length?html`<div class="lab-segment-strip" role="img" aria-label="Доли записанных сегментов промпта">${entries.map(([k,v])=>html`<span class=${'segment-'+k} style=${{width:total?100*v/total+'%':'0%'}} title=${(labels[k]||k)+': '+int(v)}></span>`)}</div><div class="lab-segment-labels">${entries.map(([k,v])=>html`<span><i class=${'segment-'+k}></i>${labels[k]||k}<b>${int(v)}</b></span>`)}</div>`:html`<p class="lab-inline-note">Размеры сегментов для этого события не записаны.</p>`}</section>`;
}

function Observation({run,cursor,event,onEvent}) {
  const observations=run.observations||[],world=scenarioReadings(run,cursor),rows=world||readingRows(observations,cursor);
  const ctx=(run.contexts||[]).filter(c=>timed(c)&&c.t<=cursor).at(-1);
  const byId=new Map(run.events.map(e=>[e.id,e]));
  const req=ctx?byId.get(ctx.prompt?.snapshot_event_id||ctx.event_id):null;
  const snapshot=req?.context_snapshot;
  const recorded=ctx?.prompt?.available&&snapshot!==undefined;
  const known=ctx?readingRows(observations,ctx.t,ctx.observed_through):[];
  return html`<div class="lab-observation" data-testid="lab-observation"><div class="lab-question"><span class="lab-kicker">01 / ВИДИМОСТЬ</span><h2>Что произошло.<br/><em>Что было известно.</em></h2><p>Один момент времени связывает наблюдения с зафиксированным ходом агента.</p></div><div class="lab-dual">
    <section class="lab-world"><header><span class="lab-pane-index">A</span><div><h3>${world?'Мир сценария':'Входные наблюдения'}</h3><p>Последние значения к ${timeText(cursor,run)}</p></div><span class="lab-pane-dot cyan"></span></header>${world?html`<div class="lab-truth-label">ИСТИНА СИМУЛЯТОРА · ${run.scenario_truth.source}</div>`:''}<${Readings} rows=${rows} run=${run} onEvent=${onEvent}/><footer>${world?'Из seeded-сценария · синтетические входы, не физические измерения':'Из входов трассы · не независимая физическая истина'}</footer></section>
    <section class="lab-knowledge"><header><span class="lab-pane-index">B</span><div><h3>Контекст агента</h3><p>${ctx?'Запрос '+(ctx.req_id||'')+' · '+timeText(ctx.t,run):'Снимок выбранного запроса'}</p></div><span class="lab-pane-dot lime"></span></header>
      ${recorded?html`<${ContextSnapshot} snapshot=${snapshot}/>`:html`<div class="lab-context-missing"><${Glyph} name="layers" size=${29}/><h4>Контекст промпта не записан</h4><p>${ctx?.prompt?.reason||'Размеры сегментов и входные события не доказывают, какие значения попали в запрос.'}</p></div>`}
      ${ctx&&!recorded?html`<details class="lab-reconstruction"><summary>Реконструкция входов к этому запросу <span>${known.length}</span></summary><p>Восстановлено из принятых наблюдений. Содержимое запроса этим не подтверждается.</p><${Readings} rows=${known} run=${run} onEvent=${onEvent}/></details>`:''}<footer>${recorded?'Содержимое из context_snapshot исходной трассы':'Граница наблюдения и знания показана явно'}</footer>
    </section></div><${PromptSegments} event=${req||event}/><${Resources} run=${run}/><div class="lab-observation-note"><${Glyph} name="link" size=${16}/><span>Выберите отметку на ленте, чтобы связать показание с первичным событием и решением.</span></div></div>`;
}

function connectedEvents(run,event) {
  if(!event)return [];
  const byId=new Map(run.events.map(e=>[e.id,e])),adj=new Map();
  for(const e of run.events)for(const link of e.links||[]){if(!byId.has(link.id))continue;(adj.get(e.id)||adj.set(e.id,[]).get(e.id)).push(link.id);(adj.get(link.id)||adj.set(link.id,[]).get(link.id)).push(e.id);}
  const ids=new Set([event.id]),queue=[event.id];
  while(queue.length&&ids.size<100){const id=queue.shift();for(const n of adj.get(id)||[])if(!ids.has(n)){ids.add(n);queue.push(n);if(ids.size>=100)break;}}
  return [...ids].map(id=>byId.get(id)).filter(Boolean).sort((a,b)=>(a.t??Infinity)-(b.t??Infinity));
}

function Decisions({run,cursor,event,onSelect}) {
  const related=useMemo(()=>connectedEvents(run,event),[run,event]),requests=related.filter(e=>e.type==='llm_request');
  const selectedRequest=event?.type==='llm_request'?event:requests.at(-1);
  const stages=[{id:'input',label:'Наблюдение',types:['input']},{id:'gate',label:'Допуск / подавление',types:['wake','suppress','rule_fired','deferred']},{id:'model',label:'Обращение к модели',types:['llm_request','llm_response']},{id:'result',label:'Действие / результат',types:['tool_call','tool_result','user_reply','notify','error']}];
  const ids=new Set(related.map(e=>e.id)),links=related.flatMap(e=>(e.links||[]).filter(l=>ids.has(l.id)).map(l=>({from:l.id,to:e.id,relation:l.relation})));
  const candidates=run.events.filter(e=>['wake','suppress','llm_request','tool_result'].includes(e.type)&&timed(e)&&e.t<=cursor).slice(-18).reverse();
  return html`<div class="lab-decisions" data-testid="lab-decisions"><div class="lab-question horizontal"><div><span class="lab-kicker">02 / ПРИЧИННОСТЬ</span><h2>Решение под микроскопом<span>.</span></h2></div><p>Связи строятся по идентификаторам исходной трассы. Близость событий во времени связью не считается.</p></div>
    <div class="lab-decision-focus"><span class=${'lab-type-tag '+laneOf(event||{})}>${titleOf(event)}</span><b>${brief(eventDetail(event),110)}</b><code>${event?.id||DASH}</code><span>${timeText(event?.t,run)}</span></div>
    <div class="lab-chain">${stages.map((s,i)=>{const es=related.filter(e=>s.types.includes(e.type));return html`<section class=${'lab-chain-stage stage-'+s.id}><header><span>0${i+1}</span><h3>${s.label}</h3><b>${es.length}</b></header>${es.length?es.slice(0,12).map(e=>html`<button class=${'lab-causal-event'+(e.id===event?.id?' selected':'')} onClick=${()=>onSelect(e)}><span>${titleOf(e)}</span><b>${brief(eventDetail(e),75)||e.req_id||e.call_id||e.input_id||e.record_id||e.id}</b><small>${timeText(e.t,run)} · ${e.id}</small></button>`):html`<div class="lab-stage-empty">В связанной трассе<br/>нет события этого этапа</div>`}</section>`;})}</div>
    ${links.length?html`<details class="lab-provenance"><summary><${Glyph} name="link" size=${15}/> ${links.length} записанных связей</summary><div>${links.slice(0,100).map(l=>html`<span><code>${l.from}</code><i>→ ${l.relation} →</i><code>${l.to}</code></span>`)}</div></details>`:html`<div class="lab-inline-note lab-no-links">Для выбранного события причинные ссылки не записаны. Показано только первичное событие.</div>`}
    <${PromptSegments} event=${selectedRequest}/><div class="lab-decision-bottom"><section><div class="lab-subhead"><h3>Другие решения в этом эпизоде</h3><span>до курсора</span></div><div class="lab-decision-list">${candidates.length?candidates.map(e=>html`<button class=${e.id===event?.id?'selected':''} onClick=${()=>onSelect(e)}><span class=${'lab-mini-dot '+laneOf(e)}></span><time>${timeText(e.t,run)}</time><b>${titleOf(e)}</b><span>${brief(eventDetail(e),75)}</span><${Glyph} name="arrow" size=${13}/></button>`):html`<p class="lab-inline-note">Событий решений до этого момента нет.</p>`}</div></section><section class="lab-event-detail"><div class="lab-subhead"><h3>Первичное событие</h3><code>${event?.id||DASH}</code></div><pre data-testid="lab-raw-event">${event?JSON.stringify(event,null,2):'Выберите событие на ленте'}</pre></section></div></div>`;
}

function MetricCompare({a,b}) {
  const metrics=[['sensor_inputs','Входы датчиков',''],['wakes','Пробуждения',''],['suppressed','Подавлено',''],['llm_calls','Вызовы модели',''],['total_tokens',estimated(a)||estimated(b)?'Токены · оценка mock':'Токены по reported usage',''],['prompt_tokens_est','Оценка входных токенов',''],['latency_ms_mean','Ответ mock / диагностическая задержка','мс'],['errors','Ошибки',''],['cost_rub','Стоимость по провайдеру','₽']];
  const strict=r=>{const s=r?.evaluation?.situations;return s?`${int(s.reacted_in_time)} / ${int(s.significant)}`:DASH;};
  return html`<div class="lab-compare-table"><div class="lab-compare-row header"><span>Измерение</span><b>A / ${a?.label||'Прогон'}</b><b>B / ${b?.label||'Выберите прогон'}</b></div><div class="lab-compare-row critical"><span>Ситуации: строгая реакция в дедлайн</span><b>${strict(a)}</b><b>${strict(b)}</b></div><div class="lab-compare-row"><span>Пропущено / опоздание</span><b>${a?.evaluation?.situations?int(a.evaluation.situations.missed?.length||0)+' / '+int(a.evaluation.situations.late?.length||0):DASH}</b><b>${b?.evaluation?.situations?int(b.evaluation.situations.missed?.length||0)+' / '+int(b.evaluation.situations.late?.length||0):DASH}</b></div>${metrics.map(([key,label,unit])=>html`<div class="lab-compare-row"><span>${label}</span><b>${num(a?.metrics?.[key],key==='latency_ms_mean'||key==='cost_rub'?2:0)}<small>${a?.metrics?.[key]!=null?unit:''}</small></b><b>${num(b?.metrics?.[key],key==='latency_ms_mean'||key==='cost_rub'?2:0)}<small>${b?.metrics?.[key]!=null?unit:''}</small></b></div>`)}<div class="lab-compare-row resource-heading"><span>Ресурсы · только PID агента на хосте</span><b>${resourceHost(a)}</b><b>${resourceHost(b)}</b></div>${[['rss','RSS · максимум снятых отсчётов','МиБ',2],['cpu','CPU между отсчётами','с',3],['span','Наблюдавшийся интервал хоста','с',2]].map(([key,label,unit,nd])=>html`<div class="lab-compare-row"><span>${label}</span><b>${num(resourceValue(a,key),nd)}<small>${resourceValue(a,key)!=null?unit:''}</small></b><b>${num(resourceValue(b,key),nd)}<small>${resourceValue(b,key)!=null?unit:''}</small></b></div>`)}</div>`;
}

function resourceHost(run) {const host=run?.resources?.host;return host?[host.system,host.machine].filter(Boolean).join(' / ')||DASH:DASH;}
function resourceValue(run,key) {
  const r=run?.resources,m=run?.metrics;const value=key==='rss'?(m?.agent_rss_sampled_max_bytes??r?.rss_sampled_max_bytes):key==='cpu'?(m?.agent_cpu_observed_s??r?.cpu_observed_s):r?.observed_wall_s;
  return Number.isFinite(value)?key==='rss'?value/(1024*1024):value:null;
}

function Resources({run}) {
  const resources=run?.resources;
  if(!resources)return run?.kind==='experiment'?html`<p class="lab-inline-note lab-resource-unavailable">Ресурсы процесса в этой трассе не записаны.</p>`:'';
  const samples=(resources.samples||[]).map(s=>({t:Number.isFinite(s.wall_s)?s.wall_s:s.ts_wall,rss:s.rss_bytes})).filter(s=>Number.isFinite(s.t)&&Number.isFinite(s.rss));
  const start=samples[0]?.t??0,end=samples.at(-1)?.t??start,span=Math.max(.001,end-start),max=Math.max(1,...samples.map(s=>s.rss));
  const X=t=>8+(t-start)/span*584,Y=v=>74-v/max*57;
  const path=samples.map((s,i)=>(i?'L':'M')+X(s.t)+','+Y(s.rss)).join(' ');
  return html`<section class="lab-resources" data-testid="lab-resources" data-basis="diagnostic_host_agent_pid"><div class="lab-subhead"><div><span class="lab-kicker">ФАКТИЧЕСКОЕ ИСПОЛНЕНИЕ / ЧАСЫ ХОСТА</span><h3>Ресурсы процесса агента</h3></div><span>${resourceHost(run)}${resources.pid?' · PID '+resources.pid:''}</span></div><div class="lab-resource-body"><div class="lab-resource-plot"><svg viewBox="0 0 600 90" preserveAspectRatio="none" role="img" aria-label="Измеренные отсчёты RSS процесса агента в реальном времени хоста"><line x1="8" x2="592" y1="74" y2="74" class="lab-lane-line"/>${samples.length?html`<path class="lab-resource-line" d=${path}/>${samples.map(s=>html`<circle class="lab-resource-point" cx=${X(s.t)} cy=${Y(s.rss)} r="2"><title>${num(s.rss/(1024*1024),2)} МиБ · ${num(s.t-start,3)} с от первого отсчёта</title></circle>`)}`:''}</svg><div><span>${int(samples.length)} отсчётов RSS</span><span>${Number.isFinite(resources.interval_s)?'период '+num(resources.interval_s,2)+' с':'период не записан'}</span></div></div><div class="lab-resource-number" data-testid="resource-rss"><span>RSS · максимум отсчётов</span><b>${num(resourceValue(run,'rss'),2)}<small>${resourceValue(run,'rss')!=null?'МиБ':''}</small></b></div><div class="lab-resource-number" data-testid="resource-cpu"><span>Наблюдавшийся CPU</span><b>${num(resourceValue(run,'cpu'),3)}<small>${resourceValue(run,'cpu')!=null?'с':''}</small></b><small>${resourceValue(run,'span')!=null?'за '+num(resourceValue(run,'span'),2)+' с хоста':'Интервал не записан'}</small></div></div><p>${resources.note||'Диагностические отсчёты одного PID на текущем хосте; дочерние процессы не включены.'}</p><p>${resourceValue(run,'span')!=null?'Короткое наблюдение '+num(resourceValue(run,'span'),2)+' с':'Длительность наблюдения не записана'}; это не замер на Jetson, не энергия и не оценка за 8 часов. Максимум отсчётов не равен пику за всю жизнь процесса.</p></section>`;
}

function Evaluation({run}) {
  const situations=run?.evaluation?.situations;
  if(!situations)return html`<p class="lab-inline-note">Эталон и дедлайны в этой трассе не записаны; полнота реакции не вычисляется.</p>`;
  return html`<section class="lab-evaluation"><div class="lab-subhead"><h3>Значимые ситуации и дедлайны</h3><span>строгая проверка / исходный сценарий</span></div><div class="lab-evaluation-list">${(situations.details||[]).map(s=>html`<div class=${s.in_time?'reacted':'missed'}><span class="lab-eval-symbol">${s.in_time?'✓':'!'}</span><div><b>${s.name||s.id}</b><small>${s.id} · начало ${timeText(s.t,run)} · дедлайн ${num(s.deadline_s,1)} с</small></div><strong>${s.in_time?'В дедлайн':Number.isFinite(s.reaction_t)?'Опоздание':'Нет строгой реакции'}<small>${Number.isFinite(s.latency_s)?num(s.latency_s,2)+' с · ':''}${s.via||'подтверждение не записано'}</small></strong></div>`)}</div><p class="lab-inline-note">${situations.rule||'Зачёт только по записанной связи с ситуацией.'}</p><p class="lab-inline-note">${run.evaluation?.metric_validity?.note||'Оценка относится к этому сценарию.'}</p></section>`;
}

function Experiments({index,run,compare,setCompare,comparison,meta,onCreated,onRefresh,cursor,onSeek}) {
  const scenarios=index.scenarios||[],[form,setForm]=useState({scenario:'noise-burst',policy:'gate',threshold:30,debounce_s:2,heartbeat_s:30,duration_s:180,history:'mask',window_turns:8,label:''}),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{if(scenarios.length&&!scenarios.some(s=>s.id===form.scenario))setForm(f=>({...f,scenario:scenarios[0].id}));},[scenarios]);
  const field=(k,v)=>setForm(f=>({...f,[k]:v}));
  const launch=async e=>{e.preventDefault();setBusy(true);setError('');try{const body={...form};if(!body.label.trim())delete body.label;const d=await request('/experiments',{body,meta});onCreated(d.job);onRefresh();}catch(e){setError(e.name==='AbortError'?'Запуск не ответил вовремя. Проверьте список заданий перед повтором.':e.message);onRefresh();}finally{setBusy(false);}};
  const cancel=async id=>{try{await request('/cancel',{body:{id},meta});onRefresh();}catch(e){setError(e.message);}};
  const recent=(index.jobs||[]).slice().reverse().slice(0,6);
  return html`<div class="lab-experiments" data-testid="lab-experiments"><div class="lab-question horizontal"><div><span class="lab-kicker">03 / ПРОВЕРЯЕМЫЕ ГИПОТЕЗЫ</span><h2>Изменить условие.<br/><em>Сравнить следствие.</em></h2></div><p>Запустите ограниченный локальный сценарий. Результат получает собственную трассу, конфигурацию и идентификатор.</p></div>
    <div class="lab-experiment-grid"><form class="lab-experiment-form" onSubmit=${launch}><div class="lab-subhead"><h3><${Glyph} name="beaker" size=${18}/> Новый эксперимент</h3><span>локальный сценарий</span></div><label class="lab-field">Сценарий<select value=${form.scenario} onChange=${e=>field('scenario',e.target.value)}>${scenarios.map(s=>html`<option value=${s.id}>${s.label||s.id}</option>`)}</select></label>
      <div class="lab-policy-picker" role="group" aria-label="Политика пробуждений">${[['gate','По событиям','Гейт'],['heartbeat','По времени','Heartbeat'],['hybrid','Вместе','Hybrid']].map(([id,head,label])=>html`<button type="button" aria-pressed=${form.policy===id} onClick=${()=>field('policy',id)}><span>${head}</span><b>${label}</b></button>`)}</div>
      <div class="lab-number-fields">${[['threshold','Порог в комнате',20,100,.5,'°C'],['debounce_s','Debounce',0,10,.1,'с'],['heartbeat_s','Heartbeat',5,300,1,'с'],['duration_s','Длительность',60,600,10,'вирт. с']].map(([key,label,min,max,step,unit])=>html`<label class="lab-field">${label}<div><input type="number" required min=${min} max=${max} step=${step} value=${form[key]} onInput=${e=>field(key,Number(e.target.value))}/><span>${unit}</span></div></label>`)}</div><div class="lab-number-fields"><label class="lab-field">История контекста<select value=${form.history} onChange=${e=>field('history',e.target.value)}><option value="mask">Маскирование</option><option value="window">Окно последних ходов</option><option value="full">Полная история</option><option value="summary_system">Сводка истории</option></select></label><label class="lab-field">Окно истории<div><input type="number" min="1" max="32" step="1" required value=${form.window_turns} onInput=${e=>field('window_turns',Number(e.target.value))}/><span>ходов</span></div></label></div><label class="lab-field">Название <small>необязательно</small><input value=${form.label} maxlength="120" placeholder="Например, гейт / порог 30" onInput=${e=>field('label',e.target.value)}/></label>
      ${error?html`<div class="lab-error" role="alert">${error}</div>`:''}<button class="lab-primary lab-launch" type="submit" disabled=${busy||!scenarios.length} data-testid="lab-launch">${busy?'Запускается…':'Запустить эксперимент'}<${Glyph} name="arrow" size=${17}/></button><p class="lab-form-note">Порог настраивает температуру комнаты; для котла задано 85 °C, протечка определяется по смене состояния. История меняет контекст модели. Сценарий локальный, без физического оборудования.</p>
    </form><div class="lab-jobs"><div class="lab-subhead"><h3>Ход работы</h3><span>${recent.length} последних заданий</span></div>${recent.length?recent.map(j=>html`<div class=${'lab-job '+j.status}><span class="lab-job-symbol">${ACTIVE.has(j.status)?'↗':['error','failed'].includes(j.status)?'!':'↳'}</span><div><b>${j.spec?.label||j.spec?.scenario||j.label||j.id}</b><span>${JOB_STATE[j.status]||j.status}${j.phase?' · '+j.phase:''}</span>${j.error?html`<small>${j.error}</small>`:''}<code>${j.id}</code></div>${ACTIVE.has(j.status)?html`<button type="button" class="lab-text-button" onClick=${()=>cancel(j.id)}>Остановить</button>`:''}</div>`):html`<div class="lab-jobs-empty"><${Glyph} name="beaker" size=${48}/><h3>Здесь начинается исследование</h3><p>Выберите сценарий и политику. Лента и сравнение появятся из фактически выполненного задания.</p></div>`}<div class="lab-experiment-contract"><b>ЧТО ФИКСИРУЕТСЯ</b><span>Входы → решения → результаты</span><span>Конфигурация и виртуальное время</span><span>Измерения и происхождение данных</span></div></div></div>
    ${run?html`<section class="lab-comparison"><div class="lab-subhead"><div><span class="lab-kicker">A / B · ОБЩИЙ НОЛЬ ВРЕМЕНИ</span><h3>Два прогона, один вопрос</h3></div><select aria-label="Прогон для сравнения" value=${compare||''} onChange=${e=>setCompare(e.target.value)}><option value="">Выбрать прогон B…</option>${(index.runs||[]).filter(r=>r.id!==run.id).map(r=>html`<option value=${r.id}>${r.label||r.id}</option>`)}</select></div><div class="lab-pair-validity">${comparison?(run.provenance?.day_sha256&&run.provenance.day_sha256===comparison.provenance?.day_sha256?'Один исходный день · SHA256 совпадает':'Совпадение исходного дня не подтверждено'):'Выберите второй прогон для проверки сопоставимости'}${comparison&&run.provenance?.agent_source_sha256?html`<small>${run.provenance.agent_source_sha256===comparison.provenance?.agent_source_sha256?'Одна версия агента':'Версии исходников агента различаются'}</small>`:''}</div><${AlignedRuns} a=${run} b=${comparison} cursor=${cursor} onSeek=${onSeek}/><${MetricCompare} a=${run} b=${comparison}/><${Evaluation} run=${run}/>${comparison?html`<details class="lab-second-evaluation"><summary>Строгая проверка прогона B</summary><${Evaluation} run=${comparison}/></details>`:''}<p class="lab-inline-note">Прогоны совмещены по началу собственных часов. Экономия вызовов оценивается вместе с полнотой реакции. Mock-токены — оценка; стоимость неизвестна, если счёт провайдера не записан.</p><details class="lab-config-pair"><summary>Конфигурации A и B</summary><pre>${JSON.stringify(run.config||{},null,2)}</pre><pre>${comparison?JSON.stringify(comparison.config||{},null,2):'Прогон B не выбран'}</pre></details></section>`:''}
  </div>`;
}

function AlignedRuns({a,b,cursor,onSeek}) {
  const span=Math.max(1,(a.t_end||0)-(a.t_start||0),b?(b.t_end||0)-(b.t_start||0):0);
  return html`<div class="lab-aligned" role="img" aria-label="События двух прогонов, совмещённых по началу"><svg viewBox="0 0 1000 106" preserveAspectRatio="none" onClick=${e=>{const box=e.currentTarget.getBoundingClientRect(),q=((e.clientX-box.left)/box.width*1000-48)/930;if(onSeek)onSeek(a.t_start+Math.max(0,Math.min(1,q))*span);}}>${[a,b].map((r,i)=>html`<g><text x="14" y=${35+i*46}>${i?'B':'A'}</text><line class="lab-lane-line" x1="48" x2="978" y1=${31+i*46} y2=${31+i*46}/>${r?r.events.filter(e=>timed(e)&&['input','wake','suppress','llm_request','tool_result'].includes(e.type)).filter((_,j,arr)=>j%Math.max(1,Math.ceil(arr.length/600))===0).map(e=>html`<line class=${'lab-compare-mark lab-ink-'+LANES.find(l=>l.id===laneOf(e)).color} x1=${48+(e.t-r.t_start)/span*930} x2=${48+(e.t-r.t_start)/span*930} y1=${24+i*46} y2=${38+i*46}/>`):html`<text class="lab-align-empty" x="65" y="81">Выберите второй прогон</text>`}</g>`)}<line class="lab-compare-cursor" x1=${48+Math.max(0,cursor-a.t_start)/span*930} x2=${48+Math.max(0,cursor-a.t_start)/span*930} y1="9" y2="96"/></svg><span>0:00</span><span>${dur(span)}</span></div>`;
}

function Journal({run,cursor,event,onSeek,onSelect}) {
  const [query,setQuery]=useState(''),[limit,setLimit]=useState(80);
  const events=run.events.filter(e=>(e.type+' '+e.id+' '+JSON.stringify(e)).toLowerCase().includes(query.toLowerCase()));
  return html`<div class="lab-journal"><${LabTools} run=${run} cursor=${cursor} event=${event} onSeek=${onSeek} mode="notebook"/><details class="lab-primary-journal"><summary>Первичный журнал <span>${run.events.length} событий</span></summary><label class="lab-event-search"><${Glyph} name="search" size=${17}/><input value=${query} placeholder="Тип, идентификатор или содержимое события" onInput=${e=>{setQuery(e.target.value);setLimit(80);}} aria-label="Поиск событий трассы"/></label><div class="lab-log-table">${events.slice(0,limit).map(e=>html`<button class=${event?.id===e.id?'selected':''} onClick=${()=>onSelect(e)}><time>${timeText(e.t,run)}</time><span class=${'lab-mini-dot '+laneOf(e)}></span><b>${titleOf(e)}</b><span>${brief(eventDetail(e),110)}</span><code>${e.id}</code></button>`)}</div>${events.length>limit?html`<button class="lab-text-button" onClick=${()=>setLimit(n=>n+100)}>Показать ещё ${Math.min(100,events.length-limit)} событий</button>`:''}</details></div>`;
}

function ImportDialog({meta,onClose,onImported,limit}) {
  const ref=useRef(null),[name,setName]=useState('Новая трасса'),[content,setContent]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{ref.current?.showModal();},[]);
  const file=async e=>{const f=e.target.files?.[0];if(!f)return;if(f.size>limit){setError('Файл больше допустимого объёма '+int(limit/1024)+' КиБ');return;}setName(f.name);setContent(await f.text());setError('');};
  const submit=async e=>{e.preventDefault();setBusy(true);setError('');try{const d=await request('/import',{body:{name,content},meta});onImported(d.run?.id||d.run_id||d.id);}catch(e){setError(e.name==='AbortError'?'Импорт не ответил вовремя. Проверьте каталог прогонов.':e.message);}finally{setBusy(false);}};
  return html`<dialog class="lab-import-dialog" ref=${ref} onCancel=${onClose} aria-labelledby="lab-import-title"><div class="lab-dialog-top"><span class="lab-overline">ИСТОЧНИК / JSONL</span><button onClick=${onClose} aria-label="Закрыть импорт"><${Glyph} name="close"/></button></div><h2 id="lab-import-title">Принести свои данные<span>.</span></h2><p>Трасса сохраняет исходные поля, время и идентификаторы. Причинные связи появятся только там, где они записаны.</p><form onSubmit=${submit}><label class="lab-file-picker"><${Glyph} name="upload" size=${24}/><span>Выбрать JSONL-файл<small>или вставить строки ниже</small></span><input type="file" accept=".jsonl,.ndjson,.txt,.json" onChange=${file}/></label><label class="lab-field">Название<input value=${name} maxlength="120" required onInput=${e=>setName(e.target.value)}/></label><label class="lab-field">События<textarea value=${content} onInput=${e=>setContent(e.target.value)} placeholder='{"type":"input","ts_clock":0,"id":"i1",…}' required spellcheck="false"/></label>${error?html`<div class="lab-error" role="alert">${error}</div>`:''}<button type="submit" class="lab-primary" disabled=${busy||!content.trim()}>${busy?'Импортируется…':'Открыть трассу'}<${Glyph} name="arrow" size=${17}/></button></form></dialog>`;
}

export function LabScreen({meta}) {
  const index=useIndex(),[runId,setRunId]=useState(capturedRun),[data,setData]=useState(null),[loading,setLoading]=useState(false),[error,setError]=useState(''),[tab,setTab]=useState('observe'),[cursor,setCursor]=useState(0),[selectedId,setSelectedId]=useState(''),[zoom,setZoom]=useState('overview'),[playing,setPlaying]=useState(false),[speed,setSpeed]=useState(1),[loop,setLoop]=useState(false),[importing,setImporting]=useState(false),[compare,setCompare]=useState(''),[comparison,setComparison]=useState(null),[pending,setPending]=useState(''),[follow,setFollow]=useState(true);
  const initial=useRef(false),liveFollow=useRef(true);liveFollow.current=follow;
  useEffect(()=>{if(!runId&&index.doc.runs?.length&&!initial.current){initial.current=true;const latest=[...index.doc.runs].reverse().find(r=>!ACTIVE.has(r.status)&&!['error','failed'].includes(r.status));if(latest)setRunId(latest.id);}},[index.doc]);
  useEffect(()=>{
    if(!runId)return;let alive=true,timer,first=true,lastVersion='';const controller=new AbortController();setLoading(true);setData(null);setError('');setPlaying(false);setFollow(true);
    const poll=async()=>{
      let again=false;
      try{
        const d=await request('/runs/'+encodeURIComponent(runId),{signal:controller.signal});if(!alive)return;
        const r=d.run||d;if(!Array.isArray(r.events))throw Error('В ответе отсутствует трасса событий');again=r.kind==='live';
        const version=[r.revision,r.provenance?.sha256,r.status,r.stale,r.provenance?.watch_error].join(':');
        if(first||version!==lastVersion){setData(r);lastVersion=version;
          if(first){const focus=r.events.find(e=>e.type==='llm_request'&&timed(e))||r.events.find(e=>timed(e));setCursor(again?r.t_end??0:focus?.t??r.t_start??0);setSelectedId(again?'':focus?.id||'');setZoom('overview');}
          else if(again&&liveFollow.current){setCursor(r.t_end??0);setSelectedId('');}
        }
        first=false;setError('');
      }catch(e){if(alive){setError(e.name==='AbortError'?'Трасса не ответила вовремя':e.message);again=true;}}
      finally{if(alive){setLoading(false);if(again)timer=setTimeout(poll,1000);}}
    };poll();return()=>{alive=false;clearTimeout(timer);controller.abort();};
  },[runId]);
  useEffect(()=>{setComparison(null);if(!compare)return;let alive=true;const c=new AbortController();request('/runs/'+encodeURIComponent(compare),{signal:c.signal}).then(d=>{if(alive)setComparison(d.run||d);}).catch(e=>{if(alive)setError('Сравнение: '+e.message);});return()=>{alive=false;c.abort();};},[compare]);
  useEffect(()=>{if(data?.kind==='experiment'&&!compare){const other=[...(index.doc.runs||[])].reverse().find(r=>r.id!==data.id&&r.kind==='experiment'&&!ACTIVE.has(r.status));if(other)setCompare(other.id);}if(compare===data?.id){setCompare('');setComparison(null);}},[data,index.doc]);
  useEffect(()=>{if(!pending)return;const j=(index.doc.jobs||[]).find(j=>j.id===pending);if(j?.run_id&&!ACTIVE.has(j.status)){setRunId(j.run_id);setTab('observe');setPending('');}else if(j&&['error','failed','cancelled'].includes(j.status))setPending('');},[index.doc,pending]);
  useEffect(()=>{if(!playing||!data)return;let last=performance.now();const span=zoom==='overview'?data.t_end-data.t_start:zoom==='episode'?60:10,a=zoom==='overview'?data.t_start:Math.min(Math.max(data.t_start,cursor-span*.45),Math.max(data.t_start,data.t_end-span)),b=Math.min(data.t_end,a+span);const timer=setInterval(()=>{const now=performance.now(),dt=(now-last)/1000;last=now;setCursor(t=>{const next=t+dt*speed;if(next>=b){if(loop)return a;setPlaying(false);return b;}return next;});},100);return()=>clearInterval(timer);},[playing,data,speed,loop,zoom]);
  const explicit=data?.events.find(e=>e.id===selectedId);
  const event=explicit&&(!timed(explicit)||Math.abs(explicit.t-cursor)<.0001)?explicit:data?.events.filter(e=>timed(e)&&e.t<=cursor).at(-1)||null;
  const onSeek=t=>{if(!data||!Number.isFinite(t))return;if(data.kind==='live')setFollow(false);setCursor(Math.max(data.t_start,Math.min(data.t_end,t)));setSelectedId('');};
  const onSelect=e=>{if(!e)return;if(data?.kind==='live')setFollow(false);if(timed(e))setCursor(Math.max(data.t_start,Math.min(data.t_end,e.t)));setSelectedId(e.id);};
  const onEvent=id=>{const e=data?.events.find(e=>e.id===id);if(e)onSelect(e);};
  const hasRun=!!data;
  const primaryMetrics=data?.metrics||{};
  const watcher=(index.doc.watchers||[]).find(w=>w.run_id===runId);
  return html`<div class="lab-screen" data-testid="lab-screen"><header class="lab-masthead"><div class="lab-title-lockup"><span class="lab-mark"><${Glyph} name="orbit" size=${31}/></span><div><span class="lab-overline">EDGE AI / ИССЛЕДОВАТЕЛЬСКАЯ СРЕДА</span><h1>Edge<span>Lab</span><i>β</i></h1></div></div><div class="lab-masthead-copy">Наблюдать систему.<br/><b>Проверять объяснения.</b></div><button class="lab-secondary" onClick=${()=>setImporting(true)} data-testid="lab-import"><${Glyph} name="upload" size=${16}/> Импорт трассы</button><button class="lab-primary" onClick=${()=>setTab('experiments')}><${Glyph} name="beaker" size=${16}/> Эксперимент</button></header>
    <div class="lab-runbar"><div class="lab-run-selector"><span class="lab-overline">АКТИВНЫЙ ПРОГОН</span><select value=${runId} aria-label="Активный прогон" onChange=${e=>setRunId(e.target.value)} data-testid="lab-run-select"><option value="" disabled>Выберите или создайте прогон</option>${(index.doc.runs||[]).map(r=>html`<option value=${r.id}>${r.label||r.id}</option>`)}</select></div><div class="lab-run-provenance"><span class=${'lab-run-kind '+(data?.kind||'')}>${data?sourceName(data):'Локальная лаборатория'}</span><small>${data?.origin||'Трассы · сценарии · проверяемые связи'}</small>${data?html`<a class="lab-export" href=${'api/lab/export/'+encodeURIComponent(data.id)} download=${data.id+'.jsonl'}>Экспорт JSONL ↗</a>`:''}</div>${data?html`<div class="lab-run-counts"><span><b>${int(eventCount(data))}</b> событий</span><span><b>${num(primaryMetrics.llm_calls,0)}</b> вызовов модели</span><span><b>${num(primaryMetrics.total_tokens,0)}</b> ${estimated(data)?'токенов · оценка mock':'токенов reported usage'}</span></div>`:html`<span class="lab-run-empty-note">Здесь появятся результаты выполненного эксперимента</span>`}</div>
    ${index.error||error?html`<div class="lab-error" role="alert">${error||index.error}<button class="lab-text-button" onClick=${index.refresh}>Обновить</button></div>`:''}
    ${(index.doc.watchers||[]).length?html`<div class="lab-watchers">${index.doc.watchers.filter(w=>w.error||!w.run_id).map(w=>html`<p class="lab-inline-note" role="status">Живая трасса · ${w.label||w.id}: ${w.error||'ожидание первых полных JSONL-записей'}</p>`)}</div>`:''}
    ${data?.kind==='live'?html`<div class=${'lab-live-source'+(watcher?.error?' has-error':'')} data-testid="lab-live-source"><div><span class="badge">${watcher?.error?'ОШИБКА ЧТЕНИЯ':data.status==='live'?'ЖИВАЯ ТРАССА':data.status==='completed'?'ТРАССА ЗАВЕРШЕНА':'ФРАГМЕНТ ТРАССЫ'}</span><span>${watcher?.error||'Только полные полученные записи · обновление раз в секунду'}</span></div><button class="lab-secondary" data-testid="lab-follow" aria-pressed=${follow} onClick=${()=>{setFollow(v=>!v);if(!follow){setPlaying(false);setCursor(data.t_end??0);setSelectedId('');}}}>${follow?'● Следовать за потоком':'Следовать за потоком'}</button></div>`:''}
    <nav class="lab-tabs" aria-label="Представления лаборатории">${TABS.map(([id,label],i)=>html`<button aria-current=${tab===id?'page':undefined} aria-pressed=${tab===id} onClick=${()=>setTab(id)}><small>0${i+1}</small>${label}${id==='experiments'&&(index.doc.jobs||[]).some(j=>ACTIVE.has(j.status))?html`<i class="lab-tab-dot"></i>`:''}</button>`)}</nav>
    ${loading?html`<div class="lab-loading" role="status">Чтение первичной трассы…</div>`:''}
    ${hasRun&&!loading?html`<${Timeline} run=${data} cursor=${cursor} onSeek=${onSeek} selected=${event} onSelect=${onSelect} zoom=${zoom} setZoom=${setZoom} playing=${playing} setPlaying=${v=>{setPlaying(v);if(v)setFollow(false);}} speed=${speed} setSpeed=${setSpeed} loop=${loop} setLoop=${setLoop}/>`:''}
    <div class="lab-content" data-view=${tab}>${tab==='experiments'?html`<${Experiments} index=${index.doc} run=${data} compare=${compare} setCompare=${setCompare} comparison=${comparison} meta=${meta} cursor=${cursor} onSeek=${onSeek} onCreated=${j=>{if(j?.id)setPending(j.id);}} onRefresh=${index.refresh}/>`:hasRun&&!loading?tab==='observe'?html`<${Observation} run=${data} cursor=${cursor} event=${event} onEvent=${onEvent}/>`:tab==='decisions'?html`<${Decisions} run=${data} cursor=${cursor} event=${event} onSelect=${onSelect}/>`:tab==='analysis'?html`<${LabTools} run=${data} cursor=${cursor} event=${event} onSeek=${onSeek} mode="analysis"/>`:html`<${Journal} run=${data} cursor=${cursor} event=${event} onSeek=${onSeek} onSelect=${onSelect}/>`:!loading?html`<div class="lab-start"><div class="lab-start-art" aria-hidden="true"><span class="lab-start-orbit"></span><span class="lab-start-core"><${Glyph} name="orbit" size=${70}/></span><span class="lab-start-input">наблюдение</span><span class="lab-start-gate">решение</span><span class="lab-start-output">результат</span></div><span class="lab-kicker">ОТ СОБЫТИЯ К ОБЪЯСНЕНИЮ</span><h2>У системы есть история.<br/><em>Давайте её исследуем.</em></h2><p>Импортируйте JSONL-трассу или выполните локальный сценарий. Лаборатория свяжет наблюдения, решения и результаты по записанным свидетельствам.</p><div><button class="lab-primary" onClick=${()=>setTab('experiments')}>Первый эксперимент<${Glyph} name="arrow" size=${17}/></button><button class="lab-secondary" onClick=${()=>setImporting(true)}>Открыть свою трассу</button></div><small>Без подключения к физическим приводам · данные появляются из выполненного прогона</small></div>`:''}</div>
    <footer class="lab-integrity"><span>Наблюдение ≠ контекст ≠ решение</span><span>${data?'Источник: '+(data.origin||data.id):'EdgeLab / локальное исследование'}</span></footer>
    ${importing?html`<${ImportDialog} meta=${meta} limit=${index.doc.limits?.max_import_bytes||index.doc.limits?.import_bytes||8*1024*1024} onClose=${()=>setImporting(false)} onImported=${id=>{setImporting(false);if(id)setRunId(id);initial.current=true;index.refresh();}}/>`:''}</div>`;
}
