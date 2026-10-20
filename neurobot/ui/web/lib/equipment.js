// Schema-driven equipment model. Unknown fields, null, false and zero remain inspectable.
// Paths retain their segments: a property literally named "driver.x" is not nested data.
const CHANNELS = {
  scan: ['Лидар', 'sensor', '/scan', 1], health: ['Камера · perception', 'sensor', '/neurobot/perception_health', 3],
  safety: ['Контур безопасности', 'motion', '/neurobot/safety_state', 2.5],
  gesture: ['Распознавание жестов', 'sensor', '/neurobot/gesture', 30],
  mem: ['Вычислительный модуль', 'system', 'Память и CPU', 25], units: ['Компоненты стека', 'system', 'systemd / процессы', 25],
  llm: ['Языковая модель', 'system', 'Вызовы LLM', 3600], agent: ['ИИ-агент', 'system', 'Реплики и результаты', 3600],
  k1: ['Задержка реакции', 'system', 'Расчёт по жестам', 30], stats: ['Метрики прогона', 'system', 'Агрегаты стенда', 25],
  alarm: ['Тревоги', 'event', 'События защиты', 3600], operator: ['Действия оператора', 'event', 'Журнал действий', 3600],
  fault: ['События стенда', 'event', 'Журнал отказов', 3600], touch: ['Контакты', 'event', 'Истина симулятора', 25],
};
const UNITS = { min_clearance_m:'м',front_m:'м',d_stop_m:'м',tilt_deg:'°',v:'м/с',v_max:'м/с',v_rev:'м/с',w:'рад/с',w_max:'рад/с',
  fps:'кадр/с',frame_age_s:'с',total_mib:'МиБ',budget_mib:'МиБ',cpu_pct:'%',conf:'',latency_ms:'мс',latency_s:'с',
  d_cmd_s:'с',d_rec_s:'с',distance_m:'м',moving_s:'с',range_max:'м',rub_fact:'₽',rub_est:'₽',ttr_s:'с' };
export const GROUPS = { sensor:'Датчики', motion:'Движение', system:'Система', event:'События', mcp:'MCP' };
const DISPLAY_UNITS = { Cel:'°C',Pa:'Па',V:'В',m:'м',s:'с',Hz:'Гц','m/s':'м/с' };
export const STATUS = { live:'В потоке',recorded:'Запись',stale:'Устарело',missing:'Нет данных',error:'Ошибка',ready:'Подключён',degraded:'Частично',connecting:'Подключение',described:'Есть описание' };
export function pathName(path) {
  return path.reduce((s,k) => typeof k === 'number' ? s+'['+k+']' : s ? s+'.'+k : String(k), '');
}
export function atPath(obj,path) { return path.reduce((o,k) => o == null ? undefined : o[k],obj); }
export function flatten(obj,path=[],out=[]) {
  if (obj !== null && typeof obj === 'object') {
    const entries = Object.entries(obj);
    if (!entries.length) out.push({path,key:JSON.stringify(path),name:pathName(path),value:obj,type:Array.isArray(obj)?'array':'object'});
    else for (const [k,v] of entries) flatten(v,[...path,Array.isArray(obj)?Number(k):k],out);
  } else out.push({path,key:JSON.stringify(path),name:pathName(path),value:obj,type:obj===null?'null':typeof obj});
  return out;
}
export function signalFields(doc) {
  return flatten(doc).filter(p=>p.path.length && !['k','t'].includes(p.path[0])).map(p=>({...p,unit:UNITS[p.path[p.path.length-1]]||'',readable:true,writable:false}));
}
export function shortValue(value) {
  if (value === undefined || value === null) return '—';
  if (typeof value === 'number') return Number.isFinite(value) ? new Intl.NumberFormat('ru-RU',{maximumFractionDigits:4}).format(value) : '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'object') return Array.isArray(value)?'[]':'{}';
  return String(value);
}
function observedRate(rows,T) {
  const ts=[...new Set(rows.filter(m=>m.t<=T && m.t>T-15).map(m=>m.t))];
  return ts.length>1 ? (ts.length-1)/(ts[ts.length-1]-ts[0]) : null;
}
export function streamDevices(tl,T,live,run) {
  if (run.catalog) return [];
  const result=[];
  for (const kind of Object.keys(tl.by)) {
    if (['hello','hb','end'].includes(kind)) continue;
    const list=tl.list(kind), index=tl.idx(kind,T);
    if (index<0) continue;
    const parts=kind==='cmd'?[...new Set(list.slice(0,index+1).map(m=>m.topic))]:[null];
    for (const part of parts) {
      const rows=part===null?list:list.filter(m=>m.topic===part);
      let raw=part===null?list[index]:[...rows].reverse().find(m=>m.t<=T);
      if (kind==='gesture') {
        const gs=tl.gestures(T);
        raw=gs.length ? gs.reduce((a,b)=>a.last>b.last?a:b) : raw;
      }
      const [name,group,label,ttl]=CHANNELS[kind]||[kind,'event',kind,30];
      const lastTs=raw.t;
      const status=live && !['event','gesture','llm','agent'].includes(kind) && T-lastTs>ttl ? 'stale' : live?'live':'recorded';
      result.push({id:'stream:'+kind+(part?':'+part:''),name:part?({'req':'Запрос движения','out':'Выход на привод','base':'Обратная связь базы'}[part]||part):name,
        group:kind==='cmd'?'motion':group,kind,topic:part,label:kind==='cmd'?({'req':'/neurobot/cmd_vel_req','out':'/cmd_vel','base':'Вход базы'}[part]||part):label,
        source:live?'Живой шлюз':'Запись · '+run.system.toUpperCase(),raw,lastTs,status,rate:observedRate(rows,T),
        signals:signalFields(raw),rows,ttl:ttl||2,clock:'stream',actions:[]});
    }
  }
  // Absence in the source is explicit. A recording does not silently acquire a laser scan.
  if (!result.some(d=>d.kind==='scan')) result.unshift({id:'stream:scan',name:'Лидар',kind:'scan',group:'sensor',label:'/scan',source:live?'Живой шлюз':'Запись',
    raw:null,lastTs:null,status:'missing',rate:null,signals:[],rows:[],clock:'stream',actions:[],note:live?'Поток /scan ещё не получен':'Скан не входит в эту запись'});
  return result;
}
export function catalogDevices(catalog) {
  return (catalog.devices||[]).map(d=>{
    const src=(catalog.sources||[]).find(s=>s.id===d.source);
    const props=d.properties||[],bad=p=>['stale','error','unknown','missing'].includes(p.status);
    const state=src?.status==='error'?'error':src?.status==='connecting'?'missing':src?.status==='degraded'?'degraded':
      !props.length?'described':props.every(p=>p.status==='error')?'error':props.every(bad)?'stale':props.some(bad)?'degraded':'live';
    return {...d,group:'mcp',kind:d.kind||'device',label:d.location||d.id,source:src?src.label||src.url:d.source,
      sourceId:d.source,sourceInfo:src?.server_info||{},clock:'wall',raw:d.raw||d,sampleTs:src?.last_success||0,sourceStatusTs:src?.updated_at||0,
      lastTs:state==='described'?null:Math.max(0,...props.map(p=>Number.isFinite(p.ts)?p.ts:0))||src?.last_success||null,status:state,
      signals:(d.properties||[]).flatMap(p=>{
        const vals=flatten(p.value,[p.name]);
        return vals.map(v=>({...p,...v,unit:DISPLAY_UNITS[p.unit]||p.unit||'',readable:p.readable!==false,writable:!!p.writable,property:p.name}));
      }),rows:[],rate:null,actions:d.actions||[]};
  });
}
export function numericPoints(device,signal,T,history=[]) {
  const val=v=>typeof v==='boolean'?Number(v):typeof v==='number'&&Number.isFinite(v)?v:null;
  if (!signal) return [];
  if (device.clock==='wall' || (device.kind==='scan' && history.length)) return history.filter(m=>m.t<=T && m.t>=T-300).map(m=>[m.t,val(m.values[signal.key])]);
  return device.rows.filter(m=>m.t<=T && m.t>=T-300).slice(-600).map(m=>[m.t,val(atPath(m,signal.path))]);
}
