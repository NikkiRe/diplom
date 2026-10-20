"""Deterministic data transforms and untrusted evidence exports, independent of the browser."""
from pathlib import Path
import shutil
import subprocess
import unittest

MODEL = Path(__file__).resolve().parents[1] / "web/lib/lab_analysis.js"


@unittest.skipUnless(shutil.which("node"), "Node required for frontend model checks")
class LabAnalysisTest(unittest.TestCase):
    def node(self, code):
        prelude = r'''
import assert from 'node:assert/strict';
const m = await import(process.argv[1]);
const sensor = (device,t,value,extra={}) => ({id:device+'-'+t,t,type:'input',kind:'sensor',device,property:'temperature',value,...extra});
const run = {id:'r',label:'Real trace',origin:'recorded fixture',clock:'virtual',t_start:0,config:{seed:7},
 events:[sensor('a',0,2,{unit:'Cel'}),sensor('a',1,4,{unit:'Cel'}),sensor('a',2,null,{unit:'Cel'}),sensor('a',3,8,{unit:'Cel'}),sensor('a',4,10,{unit:'Cel'})]};
const signal = m.discoverSignals(run).signals[0];
const values = result => result.samples.map(p=>p.value);
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", prelude + code, MODEL.as_uri()],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_observed_values_units_and_metric_schema(self):
        self.node(r'''
const actual={...run,events:[sensor('a',0,0),sensor('a',1,false),sensor('a',2,null),
 {id:'l',t:3,type:'llm_request',prompt_tokens_est:40,context_snapshot:{tools:[{properties:{power:{minimum:0,maximum:100}}}]}}],
 scenario_truth:{header:{devices:[{id:'a',properties:{temperature:{type:'number',unit:'Cel'}}}]}}};
const found=m.discoverSignals(actual).signals;
assert.equal(found[0].unit,'Cel');assert.equal(found[0].type,'mixed');
assert.deepEqual(found[0].samples.map(p=>p.value),[0,false,null]);
assert.ok(found.every(s=>!s.label.includes('context_snapshot')));
assert.equal(found.find(s=>s.label.includes('prompt_tokens_est')).unit,'tokens');
assert.equal(m.classifyValue({x:1,y:2,z:3}).type,'raw','Shape is not proof of pose semantics');
assert.equal(m.classifyValue({position:[1,2,3]},{semantic_type:'pose'}).type,'pose');
assert.equal(m.classifyValue({mimeType:'image/png',data:'AAAA'}).type,'image');
assert.equal(m.recordedMediaSource({mimeType:'image/png',data:'AAAA'}),'data:image/png;base64,AAAA');
assert.equal(m.recordedMediaSource('javascript:alert(1)',{mimeType:'image/png'}),'');
assert.equal(m.recordedMediaSource('data:image/svg+xml;base64,AAAA'), '');
''')

    def test_causal_average_and_derivative_keep_missing_data(self):
        self.node(r'''
assert.deepEqual(values(m.applyPipeline(signal,[{op:'average',windowS:10,maxGapS:2}])),[2,3,null,8,9]);
const derivative=m.applyPipeline(signal,[{op:'derivative',maxGapS:2}]);
assert.deepEqual(values(derivative),[null,2,null,null,2]);assert.equal(derivative.signal.unit,'Cel/s');
const gap={...signal,samples:[{t:0,value:2},{t:9,value:11},{t:9,value:13},{t:10,value:14}]};
assert.deepEqual(values(m.applyPipeline(gap,[{op:'derivative',maxGapS:2}])),[null,null,null,1]);
assert.deepEqual(values(m.applyPipeline(gap,[{op:'average',windowS:20,maxGapS:2}])),[2,11,12,38/3]);
assert.equal(m.applyPipeline(signal,[{op:'derivative'}],[],{until:1}).summary.samples,2);
''')

    def test_threshold_hold_resets_on_null_and_observation_gap(self):
        self.node(r'''
const input={...signal,samples:[{t:0,value:5},{t:1,value:5},{t:2,value:5},{t:3,value:null},{t:4,value:5},{t:8,value:5},{t:10,value:5}]};
const result=m.applyPipeline(input,[{op:'threshold',value:4,compare:'gt',holdS:2,maxGapS:3}]);
assert.deepEqual(values(result),[false,false,true,null,false,false,true]);
assert.equal(result.signal.type,'boolean');assert.equal(result.summary.trueCount,2);assert.equal(result.summary.missing,1);
assert.ok(m.applyPipeline(result.signal,[{op:'derivative'}]).errors.length);
''')

    def test_difference_requires_units_clock_and_fresh_real_reference(self):
        self.node(r'''
const other={...signal,id:'b',samples:[{t:0,value:1,eventId:'b0'},{t:2,value:null,eventId:'b2'},{t:3,value:2,eventId:'b3'}]};
const result=m.applyPipeline(signal,[{op:'difference',signalId:'b',maxAgeS:.5}],[signal,other]);
assert.deepEqual(values(result),[1,null,null,6,null]);
assert.equal(result.samples[3].referenceEventId,'b3');
assert.ok(m.differenceCompatibility(signal,{...other,clockKey:'different:clock'}).includes('часов'));
assert.ok(m.differenceCompatibility(signal,{...other,unit:'V'}).includes('Единицы'));
assert.ok(m.differenceCompatibility(signal,{...other,unitKnown:false}).includes('явно'));
assert.deepEqual(values(m.applyPipeline(signal,[{op:'raw'}])),[2,4,null,8,10]);
assert.ok(m.applyPipeline({...signal,unitConflict:true},[{op:'average'}]).errors.length);
assert.ok(m.applyPipeline({...signal,clockKnown:false},[{op:'derivative'}]).errors.length);
assert.throws(()=>m.normalizeOperator({op:'average',windowS:-3}));
''')

    def test_recipe_is_data_and_evidence_is_source_linked(self):
        self.node(r'''
const operators=[{id:'d',op:'derivative',maxGapS:2}], analysis=m.applyPipeline(signal,operators);
const recipe=m.exportRecipe(run,signal,operators), restored=m.importRecipe(JSON.stringify(recipe),[signal]);
assert.deepEqual(restored.operators,[{id:'d',op:'derivative',maxGapS:2}]);
assert.throws(()=>m.importRecipe({...recipe,operators:[{op:'eval',code:'process.exit(1)'}]},[signal]));
const card=m.makeEvidence({run,cursor:4,event:run.events[4],signal,analysis,operators,annotation:'Observation'});
assert.equal(card.result.value,2);assert.equal(card.result.sampleT,4);assert.equal(card.eventId,'a-4');
assert.equal(card.run.source.algorithm,'fnv1a32');
assert.equal(m.makeEvidence({run,cursor:4,event:run.events[4],signal,analysis,operators}).id,card.id);
assert.notEqual(m.sourceFingerprint({...run,events:[...run.events,sensor('a',5,99)]}).value,card.run.source.value);
assert.equal(m.sourceFingerprint({...run,provenance:{raw_trace_sha256:'abc'}}).algorithm,'sha256');
assert.equal(m.sourceFingerprint({...run,provenance:{raw_trace_sha256:'abc'}}).value,'abc');
assert.equal(m.sampleAt(signal.samples,-1),null);assert.equal(m.sampleAt(signal.samples,2).value,null);
''')

    def test_notebook_limits_merge_and_safe_standalone_exports(self):
        self.node(r'''
const card=m.makeEvidence({run,cursor:4,event:run.events[4],signal,analysis:m.applyPipeline(signal,[]),annotation:'</pre><script>alert(1)</script>\n```\n[x](javascript:alert(1))'});
const doc=m.notebookDocument([card]), imported=m.validateNotebook(JSON.stringify(doc));
assert.equal(imported[0].annotation,card.annotation);
assert.equal(m.mergeNotebook([card],[{...card,annotation:'replacement'}])[0].annotation,card.annotation);
const output=m.notebookHTML(imported);
assert.ok(output.includes('&lt;script&gt;'));assert.ok(!output.includes('<script>'));assert.ok(!output.includes('<script src'));
assert.ok(output.includes('fnv1a32'));assert.ok(output.includes('Исходное событие'));
const markdown=m.notebookMarkdown(imported);assert.ok(markdown.includes('````\n'));
assert.throws(()=>m.validateNotebook({format:m.NOTEBOOK_FORMAT,cards:[...Array(41)].map((_,i)=>({...card,id:'c'+i}))}));
assert.throws(()=>m.validateNotebook({format:m.NOTEBOOK_FORMAT,cards:[{...card,cursor:'4'}]}));
assert.throws(()=>m.validateNotebook({format:m.NOTEBOOK_FORMAT,cards:[card,card]}));
''')


if __name__ == "__main__":
    unittest.main()
