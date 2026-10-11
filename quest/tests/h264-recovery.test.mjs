import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('..',import.meta.url)),dir=root+'/src';
const require=createRequire(root+'/package.json');const ts=require('typescript');
const settings=await import('file://'+root+'/src/viewer-settings.js');
let now=1000;globalThis.performance={now:()=>now};globalThis.window={setTimeout,clearTimeout};
let h264Source=await readFile(dir+'/h264.ts','utf8');
for(const file of ['camera-view.js','mac-view-urls.js','video-requests.js','live-edge.js'])h264Source=h264Source.replaceAll(JSON.stringify('./'+file),JSON.stringify('file://'+root+'/src/'+file));
const {H264Viewer}=await import('data:text/javascript;base64,'+Buffer.from(ts.transpileModule(h264Source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText).toString('base64'));
const roverSource=await readFile(dir+'/rover.ts','utf8');
const module={exports:{}};
vm.runInNewContext(ts.transpileModule(roverSource,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText,{exports:module.exports,require:name=>name==='./viewer-settings.js'?settings:{},document:{hidden:false},performance:{now:()=>now},AbortController,window:{setTimeout,clearTimeout},WebSocket:{OPEN:1},URL:{createObjectURL:()=>'',revokeObjectURL(){}}});
const prototype=module.exports.RoverExperience.prototype;
function fixture(origin='default'){
 let callback;const video={paused:false,seeking:false,playbackRate:1,requestVideoFrameCallback(fn){callback=fn;return 1;},cancelVideoFrameCallback(){},pause(){this.paused=true;},removeAttribute(){},load(){}};
 globalThis.document={createElement:()=>video};
 const decoder=new H264Viewer(()=>{},()=>{});decoder.connect=()=>{};
 const selected=[],gate={frameAt:41,frameAge:900};
 const view=Object.create(prototype);
 Object.assign(view,{stopped:false,h264Enabled:false,h264:decoder,settings:{origin:{h264:origin}},sourceTruth:{health:{video:{enabled:true}}},gate,
  setVideo(enabled,save){selected.push([enabled,save]);this.h264Enabled=enabled;if(enabled)decoder.start();else decoder.stop();}});
 return{view,decoder,video,selected,gate,emit(mediaTime=1.05){callback(now,{mediaTime,expectedDisplayTime:now,processingDuration:.01});},segment(){decoder.segments=[{request_id:1,media_start_s:1,media_end_s:1.1,receivedAt:now-10,boundedAge:100,source:'PHONE RGB',frame:1}];}};
}
const results=[];
{
 now=1000;const f=fixture();f.view.applyRoverVideo();assert.deepEqual(f.selected,[[true,false]]);assert.equal(f.view.settings.origin.h264,'rover');f.view.applyRoverVideo();assert.equal(f.selected.length,1);
 f.emit();assert.equal(f.decoder.displayedFrame,null);assert.equal(f.decoder.hasVerifiedFrame,false);now=8001;f.view.applyRoverVideo();assert.deepEqual(f.selected,[[true,false],[false,false]]);assert.equal(f.view.settings.origin.h264,'fallback');f.view.applyRoverVideo();assert.equal(f.selected.length,2);
 results.push({case:'auto-never-matched-frame-even-with-rVFC-falls-back-after-six-seconds',passed:true});
}
{
 now=1000;const f=fixture();f.view.applyRoverVideo();f.segment();f.emit();const shown=f.decoder.displayedFrame;assert.ok(shown);assert.equal(f.decoder.hasVerifiedFrame,true);assert.equal(shown.age,110);
 now=8001;f.video.seeking=true;assert.equal(f.decoder.displayedFrame,null);f.view.applyRoverVideo();assert.equal(f.view.h264Enabled,true);assert.equal(f.selected.length,1);assert.equal(f.view.settings.origin.h264,'rover');assert.deepEqual(f.gate,{frameAt:41,frameAge:900});
 f.decoder.cleanMedia();assert.equal(f.decoder.hasVerifiedFrame,true);assert.equal(f.decoder.displayedFrame,null);f.view.applyRoverVideo();assert.equal(f.selected.length,1);
 f.video.seeking=false;f.video.paused=false;f.segment();f.emit();const recovered=f.decoder.displayedFrame;assert.ok(recovered);assert.equal(recovered.age,110);f.view.applyRoverVideo();assert.equal(f.selected.length,1);assert.deepEqual(f.gate,{frameAt:41,frameAge:900});
 results.push({case:'real-verified-frame-latches-across-later-null-cleanup-reconnect-and-recovery-without-freshness-renewal',passed:true});
 f.decoder.start();assert.equal(f.decoder.hasVerifiedFrame,false);assert.deepEqual(f.gate,{frameAt:41,frameAge:900});f.decoder.stop();
 results.push({case:'explicit-new-start-resets-capability-latch-only',passed:true});
}
for(const origin of ['saved','query']){
 now=10000;const f=fixture(origin);f.view.applyRoverVideo();assert.equal(f.selected.length,0);assert.equal(f.view.settings.origin.h264,origin);assert.deepEqual(f.gate,{frameAt:41,frameAge:900});
 results.push({case:origin+'-choice-unchanged',passed:true});
}
{
 now=1000;const f=fixture();f.decoder.start();f.segment();f.emit(7);assert.equal(f.decoder.displayedFrame,null);assert.equal(f.decoder.hasVerifiedFrame,false);
 f.emit();f.video.paused=true;assert.equal(f.decoder.displayedFrame,null);assert.equal(f.decoder.hasVerifiedFrame,false);
 f.video.paused=false;f.video.seeking=true;assert.equal(f.decoder.displayedFrame,null);assert.equal(f.decoder.hasVerifiedFrame,false);
 f.video.seeking=false;now=1300;assert.equal(f.decoder.displayedFrame,null);assert.equal(f.decoder.hasVerifiedFrame,false);f.decoder.stop();
 results.push({case:'unmatched-paused-seeking-or-old-decoder-callback-never-latches',passed:true});
}
const report={passed:true,checks:results.length,scope:'Actual production H264Viewer and RoverExperience.applyRoverVideo with synthetic video/clock/decoder callbacks; no connection, GPU, browser, deployment or headset evidence',results};
console.log(JSON.stringify(report,null,2));
