import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {STEER,manualDrive,steerCurve} from '../src/manual-drive.js';
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
  assert.ok(steerCurve(0.26,0)>0&&steerCurve(0.26,0)<1e-4,'continuous at the deadzone edge');
  // Expo: half-way past the deadzone gives 0.5^2.5 of full turn, sign kept; full sideways is a full pivot.
  close(steerCurve(0.625,0),0.5**STEER.expo,'expo');close(steerCurve(-0.625,0),-(0.5**STEER.expo),'sign');
  assert.equal(steerCurve(1,0),1);assert.equal(steerCurve(-1,0),-1);
  assert.equal(steerCurve(1,-0.3),1,'a hard sideways push with some forward still pivots fully');
  // Axis lock: pushing forward (or back) with drift under 20 degrees drives straight.
  for(const [x,y] of [[0.3,-1],[-0.34,-1],[0.3,1],[0.2,-0.6]])assert.equal(steerCurve(x,y),0,`locked at ${x},${y}`);
  const just=Math.tan(21*Math.PI/180);assert.ok(Math.abs(steerCurve(just,-1))>0,'past 20 degrees steering returns');
  assert.ok(steerCurve(just,-1)<0.02,'and starts gently');
  assert.equal(steerCurve(NaN,0),0);assert.equal(steerCurve(0.9,Infinity),steerCurve(0.9,0),"invalid y counts as no forward push");
});
test('forward stick stays full power with the new steering: straight push is straight at 255/255',()=>{
  const forward=y=>manualDrive(-Math.min(1,Math.max(0,(Math.abs(y)-.25)/.75))*Math.sign(y),steerCurve(0.3,y));
  assert.deepEqual(motorOutputs([forward(-1),forward(1)]),[[255,255],[-255,-255]],'drifting straight push stays straight');
  assert.deepEqual(motorOutputs([manualDrive(0,steerCurve(1,0)),manualDrive(0,steerCurve(-1,0))]),[[255,-255],[-255,255]],'hard sideways is a full pivot');
});
test('rover.ts feeds raw stick axes to the steering curve, forward unchanged',async()=>{
  const {readFileSync}=await import('node:fs');
  const source=readFileSync(new URL('../src/rover.ts',import.meta.url),'utf8');
  assert.match(source,/manualDrive\(-stick\(stickY, XR_STICK_DEADZONE\), steerCurve\(stickX, stickY\)\)/);
});
