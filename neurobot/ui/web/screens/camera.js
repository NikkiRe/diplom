import {useEffect,useRef,useState} from 'preact/hooks';
import {html} from '../lib/ui.js';
import {clock,num} from '../lib/fmt.js';
import {mediaURL} from '../lib/capabilities.js';

// One reviewed camera adapter. Tool names on an unknown device do not grant
// authority to execute them. The backend validates the cam-mcp contract again.
export function CameraView({device,meta}){
  const [frame,setFrame]=useState(null),[busy,setBusy]=useState(false),[watch,setWatch]=useState(false),[error,setError]=useState(''),[saving,setSaving]=useState(false);
  const active=useRef(false),alive=useRef(true),controller=useRef(null);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;controller.current?.abort();};},[]);
  const refresh=async()=>{
    if(active.current)return;active.current=true;setBusy(true);controller.current=new AbortController();
    const timeout=setTimeout(()=>controller.current?.abort(),5000);
    try{
      const r=await fetch('api/catalog/frame',{method:'POST',headers:{'Content-Type':'application/json',...(meta?.csrf?{'X-Neurobot-CSRF':meta.csrf}:{})},body:JSON.stringify({source_id:device.sourceId}),signal:controller.current.signal});
      const doc=await r.json();if(!r.ok||doc.ok===false)throw Error(doc.error||'Камера: HTTP '+r.status);
      if(!mediaURL(doc,'image'))throw Error('Камера вернула неподдержанное изображение');
      if(alive.current){setFrame(doc);setError('');}
    }catch(e){if(alive.current)setError(e.name==='AbortError'?'Камера не ответила за 5 с':e.message);}
    finally{clearTimeout(timeout);active.current=false;if(alive.current)setBusy(false);}
  };
  useEffect(()=>{if(!watch)return;refresh();const timer=setInterval(refresh,2000);return()=>clearInterval(timer);},[watch,device.sourceId]);
  const save=async()=>{
    if(!frame||saving)return;setSaving(true);setError('');
    const t=frame.observed_at,metadata=frame.metadata||{};
    const events=[{type:'run_start',clock:'real',ts_wall:t,ts_clock:t,origin:frame.origin||'cam-mcp snapshot',source_kind:'camera_snapshot'},
      {type:'input',kind:'image',device:device.id,property:'frame',value:{mimeType:frame.mimeType,data:frame.data},metadata,
        source:device.source,ts_wall:t,ts_clock:t,clock:'wall',provenance:'cam-mcp snapshot received by EdgeLab; age_s is source metadata'},
      {type:'run_end',ts_wall:t,ts_clock:t}];
    try{
      const r=await fetch('api/lab/import',{method:'POST',headers:{'Content-Type':'application/json',...(meta?.csrf?{'X-Neurobot-CSRF':meta.csrf}:{})},body:JSON.stringify({name:'Кадр · '+device.name+' · '+clock(t),events})});
      const doc=await r.json();if(!r.ok)throw Error(doc.error||'HTTP '+r.status);
      if(doc.run?.id)sessionStorage.setItem('edge-lab-open-run',doc.run.id);
      location.hash='#/lab';
    }catch(e){setError(e.message);}finally{setSaving(false);}
  };
  return html`<section class="camera-instrument" data-testid="camera-instrument"><div class="ci-title"><div><span class="eq-pane-label">КАМЕРА / CAM-MCP</span><h3>Увидеть источник</h3></div><span class="ci-type">${frame?.source==='synthetic'?'СИНТЕТИЧЕСКАЯ СЦЕНА':'СНИМОК'}</span></div>
    <div class="camera-controls"><button class="eq-secondary" onClick=${refresh} disabled=${busy} data-testid="camera-snapshot">${busy?'Получение кадра…':'Получить кадр'}</button><button class=${watch?'eq-primary':'eq-secondary'} aria-pressed=${watch} onClick=${()=>setWatch(!watch)}>Обновлять каждые 2 с · ${watch?'вкл':'выкл'}</button><button class="eq-secondary" disabled=${!frame||saving} onClick=${save} data-testid="camera-save-episode">${saving?'Сохранение…':'Кадр в лабораторию ↗'}</button></div>
    ${error?html`<p class="eq-source-error" role="status">${error}</p>`:''}
    <div class=${'camera-viewport'+(error?' is-stale':'')}>${frame?html`<img src=${mediaURL(frame,'image')} alt=${'Кадр cam-mcp · '+device.name} data-testid="camera-frame"/><div class="camera-frame-meta"><span>${frame.metadata?.w||'—'} × ${frame.metadata?.h||'—'} · ${frame.mimeType}</span><span>Получен ${clock(frame.observed_at)} · возраст кадра при чтении ${num(frame.metadata?.age_s,2)} с</span></div>`:html`<div class="camera-empty"><svg viewBox="0 0 60 45" width="70" aria-hidden="true"><path d="M5 8h35v30H5zM40 17l15-8v28l-15-8M15 22h15M22 15v15"/></svg><h3>Кадр по запросу</h3><p>Источник предоставляет изображение отдельно от сводки детектора движения.</p></div>`}</div>
    <p class="ci-foot">${frame?.source==='synthetic'?'Синтетическая сцена cam-mcp · без физической камеры. ':''}Читается снимок камеры. ${watch?'Частота обновления — 0,5 Гц. ':'Автообновление выключено. '}Кадры не отправляются LLM.</p></section>`;
}
