import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {DRIVE,ForwardRamp,JOYSTICK_PACE,STEER,forwardGain,joystickDrive,manualDrive,steerCurve} from '../src/manual-drive.js';
const root=fileURLToPath(new URL('../../',import.meta.url));
const python=`${root}.venv/bin/python`;
function motorOutputs(inputs) {
  return JSON.parse(execFileSync(python,['-c',`
import json,sys
from jetson.control.state import ControlState
c=ControlState(simulated=True,max_pwm=255,min_pwm=70,autonomy_max_pwm=255,clock=lambda:10,camera_ready=lambda:True,proximity_stop=False,pan_mount='none',demo_mode=True)
c.connected=True;c.deadline=11
rows=[]
for d in json.loads(sys.argv[1]):
 c.v=d['v'];c.w=d['w'];rows.append(list(c.output()[:2]))
print(json.dumps(rows))
`,JSON.stringify(inputs)],{cwd:root,encoding:'utf8'}));
}
test('straight, pivot and forward arcs preserve full configured PWM',()=>{
  const cases=[manualDrive(1,0),manualDrive(-1,0),manualDrive(0,1),manualDrive(0,-1),manualDrive(1,.5),manualDrive(1,-.5),manualDrive(1,.25)];
  assert.deepEqual(motorOutputs(cases),[[255,255],[-255,-255],[255,-255],[-255,255],[255,132],[132,255],[255,181]]);
});
test('forward joystick diagonals keep translation instead of becoming pure spins',()=>{
  // Actual Quest inputs after its .25 deadzone: 30- and 45-degree forward diagonals.
  const forward=(Math.sqrt(3)/2-.25)/.75, diagonal=(Math.SQRT1_2-.25)/.75;
  const cases=[manualDrive(forward,1/3),manualDrive(forward,-1/3),manualDrive(diagonal,diagonal),manualDrive(diagonal,-diagonal)];
  assert.deepEqual(motorOutputs(cases),[[255,148],[148,255],[255,0],[0,255]]);
});
test('center and invalid inputs produce zero physical output',()=>{
  assert.deepEqual(motorOutputs([manualDrive(0,0),manualDrive(NaN,Infinity)]),[[0,0],[0,0]]);
});

test('right-stick steering: deadzone without a jump, expo curve, straight-ahead lock, full pivot',()=>{
  const close=(a,b,m)=>assert.ok(Math.abs(a-b)<1e-9,`${m}: ${a} vs ${b}`);
  // Deadzone: a small sideways wobble on its own never turns; just past it starts from ~0.
  assert.equal(steerCurve(0.2,0),0);assert.equal(steerCurve(-0.25,0),0);
  assert.ok(steerCurve(0.26,0)>0&&steerCurve(0.26,0)<0.02,'continuous at the deadzone edge');
  // Expo: half-way past the deadzone gives 0.5^2.5 of full turn, sign kept; full sideways is a full pivot.
  close(steerCurve(0.625,0),0.5**STEER.expo,'expo');close(steerCurve(-0.625,0),-(0.5**STEER.expo),'sign');
  assert.equal(steerCurve(1,0),1);assert.equal(steerCurve(-1,0),-1);
  assert.equal(steerCurve(1,-0.3),1,'a hard sideways push with some forward still pivots fully');
  // Axis lock: pushing forward (or back) with drift under 20 degrees drives straight.
  for(const [x,y] of [[0.3,-1],[-0.34,-1],[0.3,1],[0.2,-0.6]])assert.equal(steerCurve(x,y),0,`locked at ${x},${y}`);
  const just=Math.tan(21*Math.PI/180);assert.ok(Math.abs(steerCurve(just,-1))>0,'past 20 degrees steering returns');
  assert.ok(steerCurve(just,-1)<0.2,'and starts gently');
  assert.equal(steerCurve(NaN,0),0);assert.equal(steerCurve(0.9,Infinity),steerCurve(0.9,0),"invalid y counts as no forward push");
});
test('forward stick stays full power with the new steering: straight push is straight at 255/255',()=>{
  const forward=y=>manualDrive(-Math.min(1,Math.max(0,(Math.abs(y)-.25)/.75))*Math.sign(y),steerCurve(0.3,y));
  assert.deepEqual(motorOutputs([forward(-1),forward(1)]),[[255,255],[-255,-255]],'drifting straight push stays straight');
  assert.deepEqual(motorOutputs([manualDrive(0,steerCurve(1,0)),manualDrive(0,steerCurve(-1,0))]),[[255,-255],[-255,255]],'hard sideways is a full pivot');
});
test('rover.ts feeds raw stick axes to the steering curve, joystick capped, keyboard uncapped',async()=>{
  const {readFileSync}=await import('node:fs');
  const source=readFileSync(new URL('../src/rover.ts',import.meta.url),'utf8');
  assert.match(source,/joystickDrive\(forward, steerCurve\(stickX, stickY\)\)/);
  assert.match(source,/manualDrive\(Number\(this\.keys\.has\("KeyW"\)\)/,'keyboard keeps full authority');
});
test("joystick pace: full stick matches Dechante's manual run (181 straight, ±181 spin, 255/0 arc)",()=>{
  assert.equal(JOYSTICK_PACE,0.6);
  const fwd=y=>Math.min(1,Math.max(0,(Math.abs(y)-.25)/.75));
  const cases=[joystickDrive(fwd(-1),steerCurve(0.3,-1)),joystickDrive(-fwd(1),steerCurve(0.3,1)),
    joystickDrive(0,steerCurve(1,0)),joystickDrive(0,steerCurve(-1,0)),joystickDrive(1,steerCurve(1,-1)),joystickDrive(1,.5)];
  assert.deepEqual(motorOutputs(cases),[[181,181],[-181,-181],[181,-181],[-181,181],[255,0],[236,126]]);
  // Shape is kept: deadzone, lock and ease-in still apply before the cap; zero stays exactly zero.
  assert.deepEqual(joystickDrive(0,steerCurve(0.2,0)),{v:0,w:0});
  assert.ok(!Object.is(joystickDrive(-0,-0).v,-0)&&!Object.is(joystickDrive(-0,-0).w,-0),'no -0');
  assert.deepEqual(joystickDrive(NaN,Infinity),{v:0,w:0});
  const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-12,`${a} vs ${b}`);
  close(joystickDrive(forwardGain(.3),0).v,JOYSTICK_PACE);
});

test('forward ease-in: no step at the first push, full after 0.1 s (arrow-key feel)',()=>{
  const close=(a,b,m,eps=1e-9)=>assert.ok(Math.abs(a-b)<eps,`${m}: ${a} vs ${b}`);
  assert.deepEqual(DRIVE,{onsetGain:1,onsetSeconds:0.1,climbSeconds:0.1,maxStep:0.1});
  const ramp=new ForwardRamp(),out=[];
  for(let t=0;t<=2600;t+=10)out.push([t,ramp.step(1,t)]);
  const at=ms=>out.find(([t])=>t===ms)[1];
  assert.equal(at(0),0,'first tick from rest moves nothing');
  close(at(50),0.5,'onset rises over 0.1 s (no step)');
  close(at(100),1,'full demand at the end of the onset');
  close(at(300),1,'stays full while held');
  assert.equal(at(2600),1,'never above the stick demand');
  for(let i=1;i<out.length;i++)assert.ok(out[i][1]>=out[i-1][1]-1e-12,'monotonic while held');
  // Partial stick: the same gain applies to the demand.
  const half=new ForwardRamp();half.step(-0.5,0);for(let t=10;t<=300;t+=10)half.step(-0.5,t);close(half.v,-0.5,'reverse half stick eases in too');
  // A stalled frame cannot skip the ease-in.
  const stalled=new ForwardRamp();stalled.step(1,0);close(stalled.step(1,5000),forwardGain(DRIVE.maxStep),'gap capped');
});
test('forward ease-in: easing off, release, reversal and stops are immediate and restart it',()=>{
  const ramp=new ForwardRamp();
  for(let t=0;t<=3000;t+=10)ramp.step(1,t);
  assert.equal(ramp.v,1);
  assert.equal(ramp.step(0.4,3010),0.4,'easing off applies at once');
  assert.equal(ramp.step(0,3020),0,'release stops at once');
  assert.equal(ramp.step(1,3030),0,'and the next push starts the ease-in again');
  for(let t=3040;t<=6000;t+=10)ramp.step(1,t);
  assert.equal(ramp.step(-1,6010),0,'reversal passes through rest at once, then eases in the other way');
  assert.ok(ramp.step(-1,6060)<0&&ramp.step(-1,6060)>-0.6);
  for(let t=6120;t<=9000;t+=10)ramp.step(-1,t);
  ramp.reset();assert.equal(ramp.v,0,'B / stop / deadman reset to rest');
  assert.equal(ramp.step(-1,9010),0,'next push eases in again');
  assert.equal(ramp.step(NaN,9020),0,'invalid demand is rest');
  for(let held=0;held<=3;held+=0.05)assert.ok(forwardGain(held)<=1,'gain never exceeds 1');
});
test('rover.ts eases only the Quest stick forward demand and resets on every stop path',async()=>{
  const {readFileSync}=await import('node:fs');
  const source=readFileSync(new URL('../src/rover.ts',import.meta.url),'utf8');
  assert.match(source,/this\.forwardRamp\.step\(-stick\(stickY, XR_STICK_DEADZONE\), now\)/);
  assert.match(source,/joystickDrive\(forward, steerCurve\(stickX, stickY\)\)/);
  assert.match(source,/stop\(reason: string, hard = false\) \{\s*this\.forwardRamp\.reset\(\);/);
  assert.match(source,/if \(!moving\) this\.forwardRamp\.reset\(\);/);
  assert.match(source,/if \(!tracked\) \{\s*this\.forwardRamp\.reset\(\);/);
});
