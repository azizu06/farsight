import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {DRIVE,DriveRamp,JOYSTICK_PACE,STEER,joystickDrive,manualDrive,steerCurve} from '../src/manual-drive.js';
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
  assert.match(source,/steerCurve\(stickX, stickY\), now\);\s*const drive = joystickDrive\(eased\.v, eased\.w\)/);
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
});

test('drive ramp: speed builds over 0.7 s and a release slows to rest over 0.25 s (captain, 057)',()=>{
  const close=(a,b,m,eps=1e-9)=>assert.ok(Math.abs(a-b)<eps,`${m}: ${a} vs ${b}`);
  assert.deepEqual(DRIVE,{accelSeconds:0.7,decelSeconds:0.25,steerSeconds:0.3,steerReleaseSeconds:0.15,maxStep:0.1});
  // Sampled at the 50 ms control pump: no command step is larger than 1/14 of full demand.
  const ramp=new DriveRamp(),out=[];
  for(let t=0;t<=1000;t+=50)out.push(ramp.step(1,0,t).v);
  assert.equal(out[0],0,'first tick from rest moves nothing');
  close(out[7],0.5,'half demand after 0.35 s');
  close(out[14],1,'full demand after 0.7 s');assert.equal(out.at(-1),1,'never above the stick demand');
  for(let i=1;i<out.length;i++){assert.ok(out[i]>=out[i-1]);assert.ok(out[i]-out[i-1]<=0.05/0.7+1e-12,'no big steps');}
  // Release: slows over 0.25 s, still moving (motion intent) until it reaches rest.
  close(ramp.step(0,0,1050).v,0.8,'release is not instant');assert.equal(ramp.moving,true);
  for(let t=1100;t<=1250;t+=50)ramp.step(0,0,t);
  assert.equal(ramp.v,0,'at rest 0.25 s after release');assert.equal(ramp.moving,false);
  // Partial and reverse demand ramp the same way; a stalled frame cannot skip the ramp.
  const back=new DriveRamp();back.step(-0.5,0,0);for(let t=50;t<=400;t+=50)back.step(-0.5,0,t);close(back.v,-0.5,'reverse half stick');
  const stalled=new DriveRamp();stalled.step(1,0,0);close(stalled.step(1,0,5000).v,DRIVE.maxStep/DRIVE.accelSeconds,'gap capped');
});
test('drive ramp: reversal slows through rest, steering is rate-limited, reset is an instant stop',()=>{
  const close=(a,b,m,eps=1e-9)=>assert.ok(Math.abs(a-b)<eps,`${m}: ${a} vs ${b}`);
  const ramp=new DriveRamp();
  for(let t=0;t<=1000;t+=50)ramp.step(1,0,t);
  const reverse=[];for(let t=1050;t<=2000;t+=50)reverse.push(ramp.step(-1,0,t).v);
  close(reverse[4],0,'decelerates to rest in 0.25 s first');
  assert.ok(reverse[5]<0&&reverse[5]>-0.1,'then builds up in reverse');
  for(let i=1;i<reverse.length;i++)assert.ok(reverse[i]<=reverse[i-1],'no bounce through zero');
  close(reverse.at(-1),-1,'full reverse 0.7 s after rest');
  const turn=new DriveRamp(),at=(goal,from,to)=>{let w;for(let t=from;t<=to;t+=25)w=turn.step(0,goal,t).w;return w;};
  turn.step(0,1,0);
  close(at(1,25,150),0.5,'half pivot after 0.15 s');close(at(1,175,300),1,'full pivot after 0.3 s');
  close(at(0,325,375),0.5,'steering returns twice as fast');close(at(0,400,450),0,'straight after 0.15 s');
  for(let t=0;t<=1000;t+=50)ramp.step(1,1,t+3000);
  ramp.reset();assert.deepEqual({v:ramp.v,w:ramp.w,moving:ramp.moving},{v:0,w:0,moving:false},'B / stop / deadman: instant rest');
  assert.deepEqual(ramp.step(1,1,9000),{v:0,w:0},'next push ramps again');
  assert.deepEqual(ramp.step(NaN,Infinity,9050),{v:0,w:0},'invalid demand is rest');
  assert.ok(!Object.is(new DriveRamp().step(-0,-0,0).v,-0),'no -0');
});
test('rover.ts ramps only the Quest stick, keeps intent while slowing, and resets on every stop path',async()=>{
  const {readFileSync}=await import('node:fs');
  const source=readFileSync(new URL('../src/rover.ts',import.meta.url),'utf8');
  assert.match(source,/this\.driveRamp\.step\(-stick\(stickY, XR_STICK_DEADZONE\), steerCurve\(stickX, stickY\), now\)/);
  assert.match(source,/joystickDrive\(eased\.v, eased\.w\)/);
  assert.match(source,/\|\| this\.driveRamp\.moving;/);
  assert.match(source,/stop\(reason: string, hard = false\) \{\s*this\.driveRamp\.reset\(\);/);
  assert.match(source,/if \(!moving\) this\.driveRamp\.reset\(\);/);
  assert.match(source,/if \(!tracked\) \{\s*this\.driveRamp\.reset\(\);/);
});
