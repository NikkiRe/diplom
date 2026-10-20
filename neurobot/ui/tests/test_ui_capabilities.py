"""Structural instruments preserve missing values and never infer physical meaning."""
import shutil
import subprocess
import unittest
from pathlib import Path

MODEL = Path(__file__).resolve().parents[1] / "web/lib/capabilities.js"


@unittest.skipUnless(shutil.which("node"), "Node required")
class CapabilityTest(unittest.TestCase):
    def test_types_media_paths_and_capture(self):
        code = r'''
import assert from 'node:assert/strict';
const m=await import(process.argv[1]);
assert.equal(m.structuralType(0),'scalar');
assert.equal(m.structuralType(false),'boolean');
assert.equal(m.structuralType(null),'raw');
assert.equal(m.structuralType({position:{x:1,y:2}}),'raw','Shape does not infer a physical pose');
assert.equal(m.structuralType([1,null,0]),'vector');
assert.deepEqual(m.plotPoints([1,null,0],'vector'),[[0,1],[1,null],[2,0]]);
assert.equal(m.mediaURL({mimeType:'image/svg+xml',data:'PHN2Zz4='},'image'),null);
assert.equal(m.mediaURL('https://remote.test/image.png','image'),null);
assert.equal(m.mediaURL({mimeType:'image/png',data:'YWJj'},'image'),'data:image/png;base64,YWJj');
assert.equal(m.mediaURL('data:image/png;base64,YWJj','image'),'data:image/png;base64,YWJj');
assert.equal(m.mediaURL('data:image/png;base64,YWJj','audio'),null);
const device={id:'d',clock:'wall',source:'s',sourceId:'source-1',properties:[{name:'driver.x',value:{'a/b':[0,false,null]}}],signals:[{key:'zero',name:'x',unit:'Cel'}]};
const fields=m.instrumentFields(device);
assert(fields.some(f=>f.path[1]==='driver.x'));
assert.deepEqual(m.instrumentValue(device,['properties','driver.x','a/b']),[0,false,null]);
const hist=new Map([['d',[{t:10,values:{zero:0}},{t:11,values:{zero:false}},{t:12,values:{},gap:true}]]]);
const capture=m.captureObservations([device],hist,13);
assert.deepEqual(capture.filter(e=>e.type==='input').map(e=>e.value),[0,false,null]);
assert.deepEqual(capture.filter(e=>e.type==='input').map(e=>e.ts_wall),[10,11,12]);
assert.equal(capture[3].status,'missing');
assert.throws(()=>m.captureObservations([],hist,13));
assert.equal(capture.some(e=>e.type==='llm_request'),false);
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code, MODEL.as_uri()],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
