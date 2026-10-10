import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const args=process.argv.slice(2), expectFixed=args.includes('--expect-fixed');
const repo=resolve(args.find(arg=>!arg.startsWith('--')) || fileURLToPath(new URL('../../../',import.meta.url)));
const root=resolve(repo,'quest');
const require=createRequire(root+'/package.json');
const ts=require('typescript'),THREE=require('three');
const {PoseRing}=await import(pathToFileURL(root+'/src/mac-auto-pose.js').href);
let source=await readFile(root+'/src/mac-auto.ts','utf8');
const sourceSha256=createHash('sha256').update(source).digest('hex');
source=source.replace('from "three"','from '+JSON.stringify(pathToFileURL(root+'/node_modules/three/build/three.module.js').href))
 .replace('import { H264Viewer } from "./h264.js";','class H264Viewer {}')
 .replace('import { mapWalk } from "./map-walk.js";','const mapWalk={};');
for(const file of ['mac-view-urls.js','mac-auto-pose.js'])source=source.replaceAll(JSON.stringify('./'+file),JSON.stringify(pathToFileURL(root+'/src/'+file).href));
const js=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
const {MacAutoView}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const metadata={stereo:true,layout:'sbs-left-right',eye_width:960,eye_height:1080};
const proj=new THREE.PerspectiveCamera(90,960/1080,.01,100).projectionMatrix.toArray();
const validEyes=[-.032,.032].map((x,i)=>({eye:i?'right':'left',position:[x,1.6,0],quaternion:[0,0,0,1],projection:[...proj]}));
function run(name,echo,entry,previous=false){
 const camera=new THREE.PerspectiveCamera();camera.position.set(0,1.6,0);camera.updateMatrixWorld(true);
 const group=new THREE.Group(),picture=new THREE.Mesh(new THREE.PlaneGeometry(),new THREE.MeshBasicMaterial()),label=new THREE.Mesh();
 const eyeQuads=[new THREE.Mesh(),new THREE.Mesh()];
 group.add(picture,label,...eyeQuads);
 const ring=new PoseRing();if(entry)ring.push(entry);
 let painted=null;
 const frame={id:'decoded-'+name,metadata:{...metadata,source:'SIMULATED',pose:echo},source:'SIMULATED',pose:echo,fovY:90,age:100};
 const view=Object.create(MacAutoView.prototype);
 Object.assign(view,{group,picture,label,eyeQuads,ring,active:true,stereo:true,session:'current-view-session',lastFrameAt:previous?990:-Infinity,lastFrameId:previous?'prior-valid':null,stereoShown:previous,
  head:{position:new THREE.Vector3(),quaternion:new THREE.Quaternion()},offset:new THREE.Vector3(),presenting:false,
  renderer:{xr:{getReferenceSpace:()=>null,getCamera:()=>camera}},viewer:{update(){},displayedFrame:frame,video:{videoWidth:1920,videoHeight:1080}},
  sendPose(){},paintLabel(text){painted=text;}});
 if(previous){picture.visible=false;for(const quad of eyeQuads)quad.visible=true;}
 const returned=view.update(1000,true);
 return {case:name,presenting:returned,monoVisible:picture.visible,eyeVisibility:eyeQuads.map(q=>q.visible),stereoShown:view.stereoShown,lastFrameAt:view.lastFrameAt,label:painted,ringMatched:!!ring.find(echo)};
}
function entry(eyes){return {id:7,mono:900,position:[0,1.6,0],quaternion:[0,0,0,1],eyes};}
const badEyes=validEyes.map(eye=>({...eye,projection:[...eye.projection]}));badEyes[1].projection[0]=0;
const missing=[run('null-echo',null,null),run('unknown-id',{pose_id:99,session_id:'current-view-session'},entry(validEyes)),
 run('missing-eyes',{pose_id:7,session_id:'current-view-session'},entry([])),run('invalid-eye-projection',{pose_id:7,session_id:'current-view-session'},entry(badEyes))];
missing.push(run('rejected-after-valid-lease',null,entry(validEyes),true));
const wrongSession=run('wrong-session-existing-id',{pose_id:7,session_id:'OLD-view-session'},entry(validEyes));
const valid=run('valid-session-pair',{pose_id:7,session_id:'current-view-session'},entry(validEyes));
assert.equal(valid.stereoShown,true,'positive control must render a known current-session pair');
const rejected=result=>!result.presenting&&!result.monoVisible&&result.eyeVisibility.every(value=>!value);
const repairVerified=missing.every(rejected)&&rejected(wrongSession);
if(expectFixed)assert.equal(repairVerified,true,'invalid or foreign-session SBS must restore local view, never show packed texture as mono');
const result={repairVerified, bugsReproduced:!repairVerified, sourceSha256,scope:'Offline actual transpiled MacAutoView.update/place/placeEyes using real Three.js geometry and actual PoseRing helpers, with injected decoded-frame/XR fixtures; no constructor, GPU, browser, wire or headset claim',sourceRevision:execFileSync('git',['rev-parse','HEAD'],{cwd:repo,encoding:'utf8'}).trim(),invalidStereoPlacement:missing,wrongSession,valid};
console.log(JSON.stringify(result,null,2));
