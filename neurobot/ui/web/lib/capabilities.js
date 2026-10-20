// Structural rendering is separate from physical semantics. Names never confer
// units, coordinate frames, calibration or permission to run an actuator.
export const VIEW_TYPES = { auto:'По структуре', scalar:'Число', boolean:'Состояние', vector:'Вектор / волна', points:'Облако XY', pose:'Позиция XY', image:'Изображение', audio:'Аудио', raw:'JSON' };
export function atPath(value,path=[]) { return path.reduce((v,k)=>v!=null&&Object.hasOwn(v,k)?v[k]:undefined,value); }
export function instrumentFields(device) {
  const fields=[];
  const walk=(value,path,depth)=>{
    if(fields.length>=256||depth>6)return;
    fields.push({path,key:JSON.stringify(path),label:path.length?path.join(' / '):'Весь объект',value});
    if(value&&typeof value==='object'&&!Array.isArray(value)) for(const [k,v] of Object.entries(value))walk(v,[...path,k],depth+1);
  };
  if(device?.properties?.length)for(const p of device.properties)walk(p.value,['properties',p.name],0);
  else walk(device?.raw,[],0);
  return fields;
}
export function instrumentValue(device,path) {
  if(path?.[0]==='properties')return atPath(device?.properties?.find(p=>p.name===path[1])?.value,path.slice(2));
  return atPath(device?.raw,path||[]);
}
export function mediaURL(value,kind) {
  const types=kind==='image'?['image/png','image/jpeg','image/webp','image/gif']:['audio/wav','audio/x-wav','audio/mpeg','audio/ogg'];
  const mime=value?.mimeType||value?.mime_type||value?.mime;
  let data=typeof value==='string'?value:value?.data;
  if(typeof data!=='string'||data.length>2800000)return null;
  if(data.startsWith('data:')) {
    const match=/^data:([^;,]+);base64,([A-Za-z0-9+/=\r\n]+)$/.exec(data);
    return match&&types.includes(match[1])?data:null;
  }
  return types.includes(mime)&&/^[A-Za-z0-9+/=\r\n]+$/.test(data)?'data:'+mime+';base64,'+data:null;
}
export function structuralType(value) {
  if(typeof value==='boolean')return 'boolean';
  if(typeof value==='number'&&Number.isFinite(value))return 'scalar';
  if(mediaURL(value,'image'))return 'image';
  if(mediaURL(value,'audio'))return 'audio';
  if(Array.isArray(value)&&value.length&&value.every(v=>v==null||typeof v==='number'))return 'vector';
  // Array-of-tuples is a structural XY projection, not a calibrated point cloud.
  if(Array.isArray(value)&&value.length&&value.every(v=>Array.isArray(v)&&v.length>=2&&typeof v[0]==='number'&&typeof v[1]==='number'))return 'points';
  return 'raw';
}
export function plotPoints(value,type) {
  if(type==='vector'&&Array.isArray(value))return value.slice(0,2048).map((v,i)=>[i,typeof v==='number'&&Number.isFinite(v)?v:null]);
  if(type==='points'&&Array.isArray(value))return value.slice(0,2048).map(v=>Array.isArray(v)?v.slice(0,2):null).filter(v=>v?.length===2&&v.every(Number.isFinite));
  if(type==='pose') {
    const v=value?.position||value;
    if(v&&Number.isFinite(v.x)&&Number.isFinite(v.y))return [[v.x,v.y]];
    if(Array.isArray(v)&&v.length>=2&&v.slice(0,2).every(Number.isFinite))return [v.slice(0,2)];
  }
  return [];
}
export function captureObservations(devices,history,now) {
  const events=[];
  for(const d of devices.filter(d=>d.clock==='wall')) {
    const samples=history.get(d.id)||[];
    for(const row of samples)for(const s of d.signals) {
      const value=row.values?.[s.key];
      if(value===undefined&&!row.gap)continue;
      events.push({type:'input',kind:'sensor',ts_wall:row.t,ts_clock:row.t,device:d.id,property:s.name,
        value:row.gap?null:value,unit:s.unit||undefined,status:row.gap?'missing':s.status||'observed',
        source:d.source,source_id:d.sourceId,clock:'wall',provenance:'MCP observations received by this browser'});
      if(events.length>=10000)throw Error('Эпизод больше 10 000 значений. Выберите меньше источников.');
    }
    // The history is scalar and bounded. Preserve the latest structured media
    // object once at its actual observation time, never copy it into old times.
    for(const p of d.properties||[]) {
      if(p.value===null||typeof p.value==='number'||typeof p.value==='boolean')continue;
      const t=Number.isFinite(p.ts)?p.ts:d.lastTs;
      if(!Number.isFinite(t))continue;
      const type=structuralType(p.value);
      events.push({type:'input',kind:type==='image'||type==='audio'?type:'sensor',ts_wall:t,ts_clock:t,
        device:d.id,property:p.name,value:p.value,unit:p.unit||undefined,status:p.status||'observed',
        source:d.source,source_id:d.sourceId,clock:'wall',metadata:p.schema||{},
        provenance:'Latest structured MCP observation; no historical media recording'});
    }
  }
  events.sort((a,b)=>a.ts_wall-b.ts_wall);
  if(!events.length)throw Error('Нужны полученные MCP-измерения. Откройте источник и дождитесь данных.');
  return [{type:'run_start',ts_wall:events[0].ts_wall,ts_clock:events[0].ts_clock,clock:'real',
    origin:'Запись наблюдений MCP · без трассы агента',source_kind:'mcp_capture',policy:{},captured_at:now},...events,
    {type:'run_end',ts_wall:events[events.length-1].ts_wall,ts_clock:events[events.length-1].ts_clock}];
}
