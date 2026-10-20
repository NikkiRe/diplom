"""Browser-independent invariants for schema discovery, clocks and missing samples."""
from pathlib import Path
import shutil
import subprocess
import unittest

WEB = Path(__file__).resolve().parents[1] / "web"


@unittest.skipUnless(shutil.which("node"), "Node required for frontend model checks")
class EquipmentModelTest(unittest.TestCase):
    def test_unknown_fields_clocks_and_sample_gaps(self):
        model = (WEB / "lib/equipment.js").as_uri()
        timeline = (WEB / "lib/timeline.js").as_uri()
        code = r'''
import assert from 'node:assert/strict';
const {flatten,atPath,streamDevices,catalogDevices,numericPoints,shortValue}=await import(process.argv[1]);
const {Timeline}=await import(process.argv[2]);
const raw={'driver.x':{'a/b':[0,false,null]},empty:[]};
const leaves=flatten(raw);
assert.equal(leaves.length,4);
assert.deepEqual(leaves.map(p=>atPath(raw,p.path)),[0,false,null,[]]);
assert.deepEqual(leaves.map(p=>shortValue(p.value)),['0','false','—','[]']);
const tl=new Timeline([{k:'cmd',topic:'req',t:1,v:1,w:0},{k:'cmd',topic:'out',t:2,v:0,w:0},
 {k:'unknown_probe',t:2,zero:0,enabled:false,nested:{alien:21}}]);
assert.equal(streamDevices(tl,2,false,{catalog:true}).length,0);
const devices=streamDevices(tl,2,false,{system:'a1'});
assert.equal(devices.find(d=>d.id==='stream:cmd:req').raw.v,1);
assert.equal(devices.find(d=>d.id==='stream:cmd:out').raw.v,0);
assert.equal(devices.find(d=>d.kind==='unknown_probe').signals.length,3);
const source={id:'mcp',status:'ready',last_success:100,updated_at:100};
const d={id:'mcp/probe',source:'mcp',properties:[{name:'mystery',value:21,status:'ok'}]};
let c=catalogDevices({sources:[source],devices:[d],updated_at:100})[0];
assert.equal(c.lastTs,100);
assert.equal(c.sampleTs,100);
c=catalogDevices({sources:[{...source,status:'error',updated_at:999}],devices:[d],updated_at:999})[0];
assert.equal(c.lastTs,100,'Failed source must not receive a fresh sample timestamp');
assert.equal(c.sampleTs,100);
assert.equal(c.status,'error');
c=catalogDevices({sources:[source],devices:[{...d,properties:[{name:'mystery',value:21,status:'stale',ts:20}]}]})[0];
assert.equal(c.status,'stale','Connected server is not proof of fresh device values');
c=catalogDevices({sources:[source],devices:[{...d,properties:[]}]})[0];
assert.equal(c.status,'described');assert.equal(c.lastTs,null);
const points=numericPoints({clock:'stream',rows:[{t:1,v:2},{t:2,v:null},{t:3,v:3}]},{path:['v']},3);
assert.deepEqual(points,[[1,2],[2,null],[3,3]],'Missing measurement remains a chart gap, never zero');
const h=[{t:1,values:{v:true}},{t:2,values:{}},{t:3,values:{v:false}}];
assert.deepEqual(numericPoints({clock:'wall'},{key:'v'},3,h),[[1,1],[2,null],[3,0]]);
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code, model, timeline],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
