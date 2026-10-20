import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { html } from '../lib/ui.js';
import { Icon } from '../lib/icons.js';
import { SignalMap } from '../lib/signalmap.js';
import { GROUPS, STATUS, streamDevices, catalogDevices, numericPoints, shortValue } from '../lib/equipment.js';
import { num, clock, age, DASH } from '../lib/fmt.js';

const EMPTY={sources:[],devices:[],updated_at:0};
function saved(key,fallback) { try { const s=localStorage.getItem(key);return s?JSON.parse(s):fallback; } catch { return fallback; } }
function persist(key,value) { try { localStorage.setItem(key,JSON.stringify(value)); } catch { /* session only */ } }
function useCatalog(meta) {
  const [doc,setDoc]=useState(EMPTY),[error,setError]=useState('');
  useEffect(()=>{
    let alive=true,controller=null;
    const refresh=async()=>{
      controller=new AbortController();
      try { const r=await fetch('api/catalog',{cache:'no-store',signal:controller.signal});if(!r.ok)throw Error('Каталог MCP: HTTP '+r.status);
        const d=await r.json();if(alive){setDoc(d);setError('');} }
      catch(e){if(alive&&e.name!=='AbortError')setError(e.message);}
    };
    refresh();const timer=setInterval(refresh,2000);
    return()=>{alive=false;clearInterval(timer);if(controller)controller.abort();};
  },[]);
  const post=async(path,body)=>{
    const r=await fetch('api/catalog/'+path,{method:'POST',headers:{'Content-Type':'application/json',...(meta&&meta.csrf?{'X-Neurobot-CSRF':meta.csrf}:{})},body:JSON.stringify(body)});
    const d=await r.json();if(!r.ok||d.ok===false)throw Error(d.error||'HTTP '+r.status);setDoc(d);return d;
  };
  return {doc,error,post};
}
function numeric(signal) { return typeof signal.value==='number'||typeof signal.value==='boolean'; }
function chooseSignal(d) {
  const priority={scan:'r[',health:'fps',safety:'min_clearance_m',mem:'total_mib',cmd:'v'}[d.kind];
  return (priority&&d.signals.find(s=>(priority==='r['?s.name.startsWith(priority):s.name===priority)&&numeric(s)))||d.signals.find(numeric)||d.signals[0];
}
function Trace({points=[],signal,compact=false}) {
  const values=points.map(p=>p[1]).filter(Number.isFinite),lo=values.length?Math.min(...values):0,hi=values.length?Math.max(...values):1;
  const min=lo===hi?lo-Math.max(Math.abs(lo)*.03,.5):lo,max=lo===hi?hi+Math.max(Math.abs(hi)*.03,.5):hi;
  const W=650,H=compact?70:160,L=compact?0:12,R=compact?0:12,B=compact?3:20,top=compact?4:14;
  const start=points[0]?.[0]||0,end=points[points.length-1]?.[0]||start+1;
  let pen=false;
  const path=points.map(([t,v])=>{if(!Number.isFinite(v)){pen=false;return '';}
    const x=L+(t-start)/Math.max(.001,end-start)*(W-L-R),y=H-B-(v-min)/Math.max(.001,max-min)*(H-top-B);
    const command=pen?(signal?.type==='boolean'?`H${x} V${y}`:`L${x},${y}`):`M${x},${y}`;pen=true;return command;
  }).join(' ');
  const last=points[points.length-1];
  return html`<div class=${'eq-trace'+(compact?' compact':'')} data-testid=${compact?undefined:'signal-trace'}>
    <svg viewBox=${`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label=${'История сигнала '+(signal?.name||'')}>
      ${[.2,.5,.8].map(q=>html`<line x1="0" x2=${W} y1=${H*q} y2=${H*q} class="eq-gridline" />`)}
      ${values.length>1?html`${values.length===points.length?html`<path class="eq-trace-fill" d=${path+` L${W-R},${H-B} L${L},${H-B}Z`} />`:''}<path class="eq-trace-line" d=${path} />`:''}
      ${last&&Number.isFinite(last[1])?html`<circle class="eq-trace-head" cx=${points.length>1?W-R:L} cy=${H-B-(last[1]-min)/Math.max(.001,max-min)*(H-top-B)} r=${compact?2:3} />`:''}
    </svg>
    ${!values.length?html`<div class="eq-trace-empty">${numeric(signal||{})?'Ожидание измерений':'Для этого типа доступен просмотр значения и JSON'}</div>`:''}
    ${!compact&&values.length?html`<div class="eq-trace-axis"><span>${clock(start)}</span><span>${clock(end)}</span></div>`:''}
  </div>`;
}
function Connection({catalog,close}) {
  const ref=useRef(null),[url,setUrl]=useState(''),[label,setLabel]=useState(''),[busy,setBusy]=useState(false),[err,setErr]=useState('');
  useEffect(()=>{ref.current.showModal();},[]);
  const connect=async(e)=>{e.preventDefault();setBusy(true);setErr('');try{await catalog.post('sources',{url,label});close();}catch(e){setErr(e.message);}finally{setBusy(false);}};
  return html`<dialog ref=${ref} class="eq-connect-dialog" onCancel=${close} aria-labelledby="connect-title" data-testid="source-dialog">
    <div class="eq-dialog-top"><span class="eq-index">ПОДКЛЮЧЕНИЕ / MCP</span><button class="icon-button" onClick=${close} aria-label="Закрыть подключение"><${Icon} name="close" /></button></div>
    <h2 id="connect-title">Добавить оборудование<span>.</span></h2><p>Подключите MCP-сервер — каталог устройств, свойства и доступные сигналы появятся автоматически.</p>
    <form onSubmit=${connect}><label>Адрес сервера<input data-testid="source-url" type="url" required placeholder="http://127.0.0.1:18090/mcp" value=${url} onInput=${e=>setUrl(e.target.value)} autofocus /></label>
      <label>Название <span>необязательно</span><input placeholder="Например, датчики лаборатории" value=${label} onInput=${e=>setLabel(e.target.value)} maxlength="100" /></label>
      <div class="eq-connection-scheme"><span>Оборудование</span><span>→</span><b>MCP</b><span>→</span><span>Атлас сигналов</span></div>
      <div class="eq-connect-note">Сервер должен быть доступен локально. Для удалённой платы используйте SSH-туннель. Каталог читает описания и состояния; настройки приводов не меняются.</div>
      ${err?html`<div class="eq-source-error" role="alert" data-testid="source-error">${err}</div>`:''}
      <button class="eq-primary" type="submit" disabled=${busy} data-testid="source-submit">${busy?'Подключение…':'Подключить источник'}<${Icon} name="chevron" size=${16} /></button>
    </form>
  </dialog>`;
}
function SourceManager({catalog,close}) {
  const ref=useRef(null),[error,setError]=useState(''),[expanded,setExpanded]=useState(null);
  useEffect(()=>{ref.current.showModal();},[]);
  const remove=async id=>{try{await catalog.post('disconnect',{id});}catch(e){setError(e.message);}};
  return html`<dialog class="eq-connect-dialog" ref=${ref} onCancel=${close} aria-labelledby="sources-title">
    <div class="eq-dialog-top"><span class="eq-index">ВНЕШНИЕ ИСТОЧНИКИ</span><button class="icon-button" onClick=${close} aria-label="Закрыть"><${Icon} name="close" /></button></div><h2 id="sources-title">Подключения<span>.</span></h2>
    ${catalog.doc.sources.length?catalog.doc.sources.map(s=>html`<section class="eq-source-entry"><div class="eq-source-row" data-testid="catalog-source"><div><b>${s.label||s.url}</b><small>${s.url}</small><span class=${s.status==='error'?'eq-source-error':''} data-testid=${s.status==='error'?'source-error':undefined}>${STATUS[s.status]||s.status}${s.error?' · '+s.error:''}</span></div><div class="eq-source-actions"><button class="eq-text-button" onClick=${()=>setExpanded(expanded===s.id?null:s.id)} aria-expanded=${expanded===s.id}>Описание</button><button class="eq-text-button" onClick=${()=>remove(s.id)}>Отключить</button></div></div>${expanded===s.id?html`<pre class="eq-raw eq-source-metadata" data-testid="source-metadata">${JSON.stringify(s,null,2)}</pre>`:''}</section>`):html`<div class="eq-source-row"><span>Внешних источников пока нет. Поток робота работает независимо.</span></div>`}
    ${error?html`<p class="eq-source-error">${error}</p>`:''}
  </dialog>`;
}
function exportDevice(device,T) {
  const body=JSON.stringify({format:'edge-atlas-snapshot/1',displayed_at:T,device:{id:device.id,name:device.name,source:device.source,status:device.status,signals:device.signals,raw:device.raw}},null,2);
  const url=URL.createObjectURL(new Blob([body],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=device.name.replace(/[^\p{L}\p{N}_-]/gu,'_')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function Inspector({device,selectedSignal,setSignal,pins,togglePin,now}) {
  const [query,setQuery]=useState(''),[raw,setRaw]=useState(false),[limit,setLimit]=useState(60);
  const rawText=useMemo(()=>raw&&device?JSON.stringify(device.raw,null,2):'',[device?.raw,raw]);
  useEffect(()=>{setQuery('');setLimit(60);setRaw(false);},[device?.id]);
  if(!device)return html`<aside class="eq-inspector"><div class="eq-empty-state">Выберите устройство на карте или в каталоге</div></aside>`;
  const fields=device.signals.filter(s=>(s.name+' '+shortValue(s.value)).toLowerCase().includes(query.toLowerCase()));
  const isPin=s=>pins.some(p=>p.device===device.id&&p.signal===s.key);
  return html`<aside class="eq-inspector" data-testid="device-detail"><div class="eq-pane-label"><span>ИНСПЕКТОР</span><span class="eq-index">03</span></div>
    <div class="eq-device-title"><span class=${'eq-device-glyph '+device.group}><${Icon} name=${device.group==='sensor'?'pulse':device.group==='motion'?'safety':'chip'} size=${25} /></span><div><h2>${device.name}</h2><p>${device.label}</p></div></div>
    <div class="eq-device-meta"><span class=${'eq-status '+device.status}>${STATUS[device.status]||device.status}</span><span>${device.clock==='wall'?'MCP · живые данные':device.source}</span></div>
    <dl class="eq-metadata"><dt>Последние данные</dt><dd>${device.lastTs?clock(device.lastTs):DASH}</dd><dt>Наблюдаемая частота</dt><dd>${device.rate==null?DASH:num(device.rate,1)+' Гц'}</dd><dt>Сигналы</dt><dd>${device.signals.length}</dd></dl>
    ${device.note?html`<p class="eq-device-note">${device.note}</p>`:''}
    <div class="eq-inspector-tabs"><button aria-pressed=${!raw} onClick=${()=>setRaw(false)}>Поля <span>${device.signals.length}</span></button><button aria-pressed=${raw} data-testid="raw-tab" onClick=${()=>setRaw(true)}>Исходный JSON</button></div>
    ${raw?html`<pre class="eq-raw" data-testid="device-raw">${rawText}</pre>`:html`<label class="eq-field-search"><${Icon} name="search" size=${14} /><input placeholder="Найти поле или значение" value=${query} onInput=${e=>{setQuery(e.target.value);setLimit(60);}} data-testid="signal-search" aria-label="Найти сигнал" /></label>
      <div class="eq-fields" data-testid="signal-list">${fields.length?fields.slice(0,limit).map(s=>html`<div class=${'eq-field'+(selectedSignal===s.key?' selected':'')} data-testid="signal-row" data-path=${s.name}>
        <button class="eq-field-select" onClick=${()=>setSignal(s.key)} title=${s.name}><span>${s.name}</span><b class=${s.value===null?'is-null':''}>${shortValue(s.value)}<small>${s.unit||''}</small></b><i>${s.type}${s.value===null?' · null':''}${s.status&&s.status!=='ok'?' · '+s.status:''}</i></button>
        <button class=${'eq-pin'+(isPin(s)?' active':'')} aria-label=${(isPin(s)?'Открепить ':'Закрепить ')+s.name} aria-pressed=${isPin(s)} onClick=${()=>togglePin(device,s)} title="Закрепить сигнал">${isPin(s)?'●':'+'}</button></div>`):html`<div class="eq-empty-state">${query?'Сигналы не найдены':'Устройство не передаёт значения'}</div>`}</div>
      ${fields.length>limit?html`<button class="eq-load-more" onClick=${()=>setLimit(n=>n+120)}>Показать ещё ${Math.min(120,fields.length-limit)} из ${fields.length-limit}</button>`:''}`}
    <div class="eq-capabilities" data-testid="device-capabilities"><span class="eq-pane-label">ВОЗМОЖНОСТИ ИСТОЧНИКА</span><p>${device.clock==='wall'?'Свойства из каталога MCP.':'Поля получены из потока наблюдения.'} ${device.signals.filter(s=>s.writable).length?device.signals.filter(s=>s.writable).length+' свойств допускают запись через MCP.':''}</p>
      ${device.actions?.length?html`<div class="eq-action-tags">${device.actions.map(a=>html`<span title=${a.description||''}>${a.name||String(a)}</span>`)}</div>`:''}
      <button class="eq-text-button" onClick=${()=>exportDevice(device,now)} data-testid="device-export">Сохранить снимок JSON <span>↗</span></button></div>
  </aside>`;
}
export function Equipment({tl,T,run,live,meta}) {
  const catalog=useCatalog(meta),[selection,setSelection]=useState(null),[signalKey,setSignal]=useState(null),[query,setQuery]=useState(''),[filter,setFilter]=useState('all');
  const [connect,setConnect]=useState(false),[sources,setSources]=useState(false),[mode,setMode]=useState('map'),[pinError,setPinError]=useState('');
  const [pins,setPins]=useState(()=>saved('edge-atlas-pins',[])),[motion,setMotion]=useState(()=>saved('edge-atlas-motion',true));
  const history=useRef(new Map());
  const timeline=useRef(tl);
  const searchRef=useRef(null);
  useEffect(()=>{
    const onKey=e=>{if(e.key==='/'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!e.target.closest('input,textarea,select,dialog')){e.preventDefault();searchRef.current?.focus();}};
    window.addEventListener('keydown',onKey);return()=>window.removeEventListener('keydown',onKey);
  },[]);
  const external=useMemo(()=>catalogDevices(catalog.doc),[catalog.doc]);
  const devices=[...streamDevices(tl,T,!!live,run),...external];
  const device=devices.find(d=>d.id===selection)||devices.find(d=>d.group==='sensor'&&d.status!=='missing')||devices[0];
  const signal=device?.signals.find(s=>s.key===signalKey)|| (device?chooseSignal(device):null);
  const now=device?.clock==='wall'?Date.now()/1000:T;
  useEffect(()=>{
    if(timeline.current!==tl){for(const k of history.current.keys())if(k.startsWith('stream:'))history.current.delete(k);timeline.current=tl;}
    const activeIds=new Set(external.map(d=>d.id));
    for(const id of history.current.keys())if(!id.startsWith('stream:')&&!activeIds.has(id))history.current.delete(id);
    for(const d of external) {
      const arr=history.current.get(d.id)||[];const stamp=d.sampleTs;
      if(['live','degraded'].includes(d.status)&&stamp&&(!arr.length||arr[arr.length-1].t!==stamp))arr.push({t:stamp,values:Object.fromEntries(d.signals.map(s=>[s.key,['error','stale','unknown','missing'].includes(s.status)?null:s.value]))});
      else if(['error','stale','missing'].includes(d.status)&&arr.length&&!arr[arr.length-1].gap)arr.push({t:d.sourceStatusTs||Date.now()/1000,values:{},gap:true});
      history.current.set(d.id,arr.slice(-180));
    }
    // Laser scans are transient in the ROS stream. Keep bounded received samples for field traces,
    // not copies of full scans in the shared SSE buffer. No generated/interpolated measurements.
    if(live)for(const d of devices.filter(d=>d.kind==='scan'&&d.lastTs)){
      const arr=history.current.get(d.id)||[];
      if(!arr.length||arr[arr.length-1].t!==d.lastTs)arr.push({t:d.lastTs,values:Object.fromEntries(d.signals.map(s=>[s.key,s.value]))});
      history.current.set(d.id,arr.filter(m=>m.t>=T-300).slice(-600));
    }
  },[external,T,tl]);
  const select=id=>{setSelection(id);setSignal(null);};
  const togglePin=(d,s)=>{
    setPins(old=>{
      const exists=old.some(p=>p.device===d.id&&p.signal===s.key);
      if(!exists&&old.length>=4){setPinError('Можно закрепить до четырёх сигналов. Сначала открепите один.');return old;}
      const next=exists?old.filter(p=>p.device!==d.id||p.signal!==s.key):[...old,{device:d.id,signal:s.key,name:s.name,deviceName:d.name}];persist('edge-atlas-pins',next);setPinError('');return next;
    });
  };
  const matched=devices.filter(d=>(filter==='all'||d.group===filter)&&(d.name+' '+d.label+' '+d.source+' '+d.signals.map(s=>s.name).join(' ')).toLowerCase().includes(query.toLowerCase()));
  const signalCount=devices.reduce((n,d)=>n+d.signals.length,0),available=devices.filter(d=>['live','recorded','degraded'].includes(d.status)).length;
  const points=device&&signal?numericPoints(device,signal,now,history.current.get(device.id)||[]):[];
  const samples=points.filter(p=>Number.isFinite(p[1]));
  return html`<div class="equipment" data-testid="equipment" data-motion=${motion?'on':'off'}>
    <div class="eq-toolbar"><div class="eq-summary"><span><b>${devices.length.toString().padStart(2,'0')}</b> источников</span><span><b>${signalCount}</b> сигналов</span><span><i class="signal-dot"></i>${available} с данными</span></div>
      <div class="eq-toolbar-actions"><button class="eq-text-button eq-motion" aria-pressed=${motion} onClick=${()=>{setMotion(!motion);persist('edge-atlas-motion',!motion);}}>Анимация <b>${motion?'вкл':'выкл'}</b></button><button class="eq-secondary" data-testid="sources-manage" onClick=${()=>setSources(true)}>Подключения <span>${catalog.doc.sources.length}</span></button><button class="eq-primary" onClick=${()=>setConnect(true)} data-testid="add-source"><b>+</b> Источник</button></div></div>
    ${catalog.error?html`<div class="eq-source-error" data-testid="catalog-error">${catalog.error}</div>`:''}
    ${catalog.doc.sources.filter(s=>['error','degraded'].includes(s.status)).map(s=>html`<div class="eq-source-error" data-testid="source-error">${s.label||s.url} · ${s.error||STATUS[s.status]}<button onClick=${()=>setSources(true)}>Подключения ↗</button></div>`)}
    <div class="eq-workspace"><aside class="eq-catalog"><div class="eq-pane-label"><span>КАТАЛОГ</span><span class="eq-index">01</span></div>
      <label class="eq-search"><${Icon} name="search" size=${16} /><input ref=${searchRef} data-testid="device-search" value=${query} onInput=${e=>setQuery(e.target.value)} placeholder="Устройство или сигнал" aria-label="Поиск оборудования" /><kbd>/</kbd></label>
      <div class="eq-filters" role="group" aria-label="Тип оборудования">${[['all','Все'],['sensor','Датчики'],['system','Система'],['mcp','MCP']].map(([id,label])=>html`<button aria-pressed=${filter===id} onClick=${()=>setFilter(id)}>${label}</button>`)}</div>
      <div class="eq-device-list" data-testid="device-list">${Object.entries(GROUPS).map(([group,label])=>{
        const groupDevices=matched.filter(d=>d.group===group);return groupDevices.length?html`<div class="eq-device-group"><h3>${label}<span>${groupDevices.length}</span></h3>${groupDevices.map(d=>html`<button class=${'eq-device'+(d.id===device?.id?' selected':'')} onClick=${()=>select(d.id)} data-testid="equipment-device" data-device-id=${d.id} aria-pressed=${d.id===device?.id}>
          <span class=${'eq-device-port '+d.status}></span><span><b>${d.name}</b><small>${d.label}</small></span><i>${d.signals.length}</i></button>`)}</div>`:'';
      })}${!matched.length?html`<div class="eq-empty-state">Ничего не найдено.<br/>Попробуйте название поля.</div>`:''}</div>
      <div class="eq-catalog-foot"><span class="eq-index">DISCOVERY / ${catalog.doc.sources.length?'MCP + STREAM':'STREAM'}</span><p>Поля определяются из данных.<br/>Неизвестные типы тоже видны.</p></div>
    </aside>
    <section class="eq-workplane"><div class="eq-workplane-head"><div><span class="eq-pane-label">РАБОЧАЯ ПЛОСКОСТЬ <span class="eq-index">02</span></span><h2>${mode==='map'?'Карта сигналов':'Все источники'}</h2></div><div class="eq-view-switch"><button aria-pressed=${mode==='map'} onClick=${()=>setMode('map')} aria-label="Карта сигналов"><${Icon} name="network" size=${17} /></button><button aria-pressed=${mode==='grid'} onClick=${()=>setMode('grid')} aria-label="Сетка устройств"><${Icon} name="overview" size=${17} /></button></div></div>
      ${mode==='map'?html`<${SignalMap} devices=${devices} selected=${device?.id} onSelect=${select} T=${T} live=${!!live||run.catalog||catalog.doc.sources.length>0} />`:html`<div class="eq-device-grid">${devices.map(d=>html`<button class=${d.id===device?.id?'selected':''} onClick=${()=>select(d.id)}><${Icon} name=${d.group==='sensor'?'pulse':'chip'} size=${24} /><b>${d.name}</b><small>${d.signals.length} полей · ${STATUS[d.status]}</small></button>`)}</div>`}
      <div class="eq-scope" data-testid="signal-scope"><div class="eq-scope-head"><div><span class="eq-pane-label">ОСЦИЛЛОГРАММА / ПОСЛЕДНИЕ 5 МИН</span><h3 data-testid="selected-signal">${signal?signal.name:'Выберите сигнал'}</h3><span class="eq-signal-origin">${device?.name||''} ${signal?' / '+signal.type:''}</span></div>
        <div class="eq-scope-value" data-testid="selected-value">${signal?shortValue(signal.value):DASH}<small>${signal?.unit||''}</small><button class=${'eq-pin '+(pins.some(p=>p.device===device?.id&&p.signal===signal?.key)?'active':'')} disabled=${!signal} onClick=${()=>signal&&togglePin(device,signal)} data-testid="pin-selected" aria-label="Закрепить выбранный сигнал">+</button></div></div>
        <${Trace} points=${points} signal=${signal} /><div class="eq-scope-footer"><span>${samples.length} отсчётов${samples.length?' · min '+shortValue(Math.min(...samples.map(p=>p[1])))+' / max '+shortValue(Math.max(...samples.map(p=>p[1]))):''}</span><span>${device?.clock==='wall'?'История с подключения в этой вкладке':live?'Часы потока':'Часы записи'} · полученные отсчёты</span></div></div>
    </section>
    <${Inspector} key=${device?.id} device=${device} selectedSignal=${signal?.key} setSignal=${setSignal} pins=${pins} togglePin=${togglePin} now=${now} />
    </div>
    <section class="eq-pinned" data-testid="signal-pins"><div class="eq-pinhead"><div><span class="eq-index">WATCHLIST</span><h2>Сигналы под наблюдением<span>${pins.length} / 4</span></h2></div>${pins.length?html`<button class="eq-text-button" onClick=${()=>{setPins([]);persist('edge-atlas-pins',[]);}}>Очистить</button>`:html`<span>Выберите поле и нажмите +</span>`}</div>
      ${pinError?html`<p class="eq-source-error" role="status">${pinError}</p>`:''}
      ${pins.length?html`<div class="eq-pin-grid">${pins.map(p=>{
        const d=devices.find(d=>d.id===p.device),s=d?.signals.find(s=>s.key===p.signal),ps=d&&s?numericPoints(d,s,d.clock==='wall'?Date.now()/1000:T,history.current.get(d.id)||[]):[];
        return html`<article class="eq-pin-card" data-testid="pinned-signal"><button class="eq-pin-close" onClick=${()=>{const next=pins.filter(x=>x!==p);setPins(next);persist('edge-atlas-pins',next);}} aria-label=${'Открепить '+p.name}>×</button><button class="eq-pin-open" onClick=${()=>{if(d){select(d.id);setSignal(p.signal);}}}><small>${p.deviceName}</small><h3>${p.name}</h3><b>${s?shortValue(s.value):DASH}<span>${s?.unit||''}</span></b></button><${Trace} points=${ps} signal=${s} compact=${true} />${!d?html`<span class="eq-pin-unavailable">Источник сейчас недоступен</span>`:''}</article>`;
      })}</div>`:html`<div class="eq-pin-empty"><span>+</span><p>Соберите свою приборную панель<small>До четырёх сигналов из любых подключённых источников. Выбор сохранится в браузере.</small></p><div class="eq-ghost-trace"><svg viewBox="0 0 200 50" aria-hidden="true"><path d="M0 30h25l9-12 10 20 12-25 12 17h30l10-9 10 19 12-25 13 15h57" /></svg></div></div>`}
    </section>
    <div class="eq-integrity"><span>Карта показывает наблюдаемые каналы и их принадлежность потоку.</span><span>MCP-источники живые и независимы от часов записи.</span></div>
    ${connect?html`<${Connection} catalog=${catalog} close=${()=>setConnect(false)} />`:''}${sources?html`<${SourceManager} catalog=${catalog} close=${()=>setSources(false)} />`:''}
  </div>`;
}
