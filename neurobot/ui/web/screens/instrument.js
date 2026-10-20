import {useMemo,useState} from 'preact/hooks';
import {html} from '../lib/ui.js';
import {VIEW_TYPES,instrumentFields,instrumentValue,structuralType,plotPoints,mediaURL} from '../lib/capabilities.js';
import {shortValue} from '../lib/equipment.js';
import {CameraView} from './camera.js';

function readProfile(id){try{const p=JSON.parse(localStorage.getItem('edge-lab-instruments')||'{}')[id];return p&&Array.isArray(p.path)?p:null;}catch{return null;}}
function saveProfile(id,p){try{const all=JSON.parse(localStorage.getItem('edge-lab-instruments')||'{}');all[id]=p;const keys=Object.keys(all);for(const k of keys.slice(0,Math.max(0,keys.length-64)))delete all[k];localStorage.setItem('edge-lab-instruments',JSON.stringify(all));return true;}catch{return false;}}
function Plot({value,type}){
  const points=plotPoints(value,type),valid=points.filter(p=>p.every(Number.isFinite));
  if(!valid.length)return html`<div class="ci-unsupported">В выбранном поле нет ${type==='pose'?'координат x, y':'совместимых отсчётов'}. Выберите другое поле или тип.</div>`;
  let xmin=Math.min(...valid.map(p=>p[0])),xmax=Math.max(...valid.map(p=>p[0])),ymin=Math.min(...valid.map(p=>p[1])),ymax=Math.max(...valid.map(p=>p[1]));
  if(xmin===xmax){xmin-=1;xmax+=1;}if(ymin===ymax){ymin-=1;ymax+=1;}
  const xy=p=>[40+520*(p[0]-xmin)/(xmax-xmin),200-170*(p[1]-ymin)/(ymax-ymin)];
  let pen=false;
  const path=points.map(p=>{if(!p.every(Number.isFinite)){pen=false;return '';}const [x,y]=xy(p),s=(pen?'L':'M')+x.toFixed(2)+','+y.toFixed(2);pen=true;return s;}).join(' ');
  return html`<div class="ci-plot"><svg viewBox="0 0 600 240" role="img" aria-label=${type==='vector'?'Вектор полученных значений':'Проекция XY полученных координат'}>
    ${[0,1,2,3,4].map(i=>html`<path class="ci-grid" d=${'M40 '+(30+42.5*i)+'H560 M'+(40+130*i)+' 30V200'} />`)}
    ${type==='vector'?html`<path class="ci-line" d=${path}/>`:valid.map(p=>{const [x,y]=xy(p);return html`<circle class="ci-point" cx=${x} cy=${y} r=${type==='pose'?7:2.5}/>`;})}
    <text x="40" y="224">${shortValue(xmin)}</text><text x="560" y="224" text-anchor="end">${shortValue(xmax)}</text><text x="8" y="34">${shortValue(ymax)}</text><text x="8" y="200">${shortValue(ymin)}</text>
    ${type==='pose'?html`<text x="300" y="18" text-anchor="middle">XY · положение без ориентации</text>`:''}
  </svg><span>${points.length} ${type==='vector'?'элементов · ось X: индекс':'точек · проекция XY'}${Array.isArray(value)&&value.length>2048?' · показаны первые 2048 из '+value.length:''}</span></div>`;
}
export function CapabilityView({device,meta}){
  const fields=useMemo(()=>instrumentFields(device),[device]);
  const [profile,setProfile]=useState(()=>readProfile(device.id));
  const [message,setMessage]=useState('');
  const initial=fields.find(f=>['image','audio','vector','points'].includes(structuralType(f.value)))||fields.find(f=>['scalar','boolean'].includes(structuralType(f.value)))||fields[0];
  const path=profile?.path||initial?.path||[],field=fields.find(f=>f.key===JSON.stringify(path));
  const value=instrumentValue(device,path),type=profile?.type&&profile.type!=='auto'?profile.type:structuralType(value);
  const declared=path[0]==='properties'?device.properties?.find(p=>p.name===path[1]):null;
  const displayUnit=profile?.unit||declared?.unit||'';
  const update=next=>{const p={path,type:'auto',unit:'',frame:'',...profile,...next};setProfile(p);setMessage(saveProfile(device.id,p)?'Привязка сохранена в этом браузере':'Привязка действует до закрытия вкладки');};
  const url=['image','audio'].includes(type)?mediaURL(value,type):null;
  return html`<div>${device.sourceInfo?.name==='cam-mcp'?html`<${CameraView} device=${device} meta=${meta}/>`:''}<div class="capability-instrument" data-testid="capability-instrument">
    <div class="ci-title"><div><span class="eq-pane-label">ТИПИЗИРОВАННЫЙ ПРИБОР</span><h3>${device.name}</h3></div><span class="ci-type">${VIEW_TYPES[type]||'JSON'}</span></div>
    <div class="ci-controls"><label>Поле<select value=${JSON.stringify(path)} onChange=${e=>update({path:JSON.parse(e.target.value)})} aria-label="Поле прибора">${fields.map(f=>html`<option value=${f.key}>${f.label}</option>`)}</select></label><label>Отображение<select value=${profile?.type||'auto'} onChange=${e=>update({type:e.target.value})} aria-label="Тип прибора">${Object.entries(VIEW_TYPES).map(([k,v])=>html`<option value=${k}>${v}</option>`)}</select></label></div>
    <div class=${'ci-display'+(['error','stale','missing'].includes(device.status)?' ci-stale':'')}>
      ${type==='scalar'?html`<div class="ci-scalar"><span>${field?.label||'Значение'}</span><strong data-testid="instrument-value">${typeof value==='number'?shortValue(value):'—'}<small>${displayUnit}</small></strong><i>${device.status==='recorded'?'Записанное значение':'Последнее наблюдение'}</i></div>`:
      type==='boolean'?html`<div class=${'ci-boolean '+(value===true?'on':'')}><span></span><strong>${value===true?'TRUE':value===false?'FALSE':'НЕТ ДАННЫХ'}</strong><small>Логическое состояние · полученное значение</small></div>`:
      ['vector','points','pose'].includes(type)?html`<${Plot} value=${value} type=${type} />`:
      type==='image'&&url?html`<figure class="ci-media"><img src=${url} alt=${'Полученный кадр · '+device.name}/><figcaption>Кадр из данных источника · ${profile?.frame||'координатная система не указана'}</figcaption></figure>`:
      type==='audio'&&url?html`<div class="ci-audio"><audio src=${url} controls preload="none"/><p>Запись источника · воспроизведение по нажатию</p></div>`:
      ['image','audio'].includes(type)?html`<div class="ci-unsupported">В этом поле нет поддержанного ${type==='image'?'растрового кадра':'аудиофрагмента'} с MIME и base64. Выберите объект ресурса.</div>`:
      html`<pre class="ci-json">${JSON.stringify(value,null,2)?.slice(0,12000)||'Нет значения'}</pre>`}
    </div>
    <details class="ci-binding"><summary>Семантика и привязка прибора</summary><p>Тип по структуре помогает прочитать данные. Единицы и систему координат задайте по документации устройства.</p><div class="ci-controls"><label>Единица<input aria-label="Единица прибора" value=${profile?.unit||''} maxLength="24" placeholder="например, °C" onInput=${e=>update({unit:e.target.value})}/></label><label>Система координат<input aria-label="Система координат прибора" value=${profile?.frame||''} maxLength="64" placeholder="из контракта источника" onInput=${e=>update({frame:e.target.value})}/></label></div><p class="ci-provenance">${profile?'Привязка пользователя':'Структурное определение'} · ${JSON.stringify(path)}<br/>${message}</p></details>
    <p class="ci-foot">${device.status==='stale'||device.status==='error'?'Данные устарели. ':''}Прибор показывает полученные данные. Калибровка и физический смысл требуют контракта источника.</p>
  </div></div>`;
}
