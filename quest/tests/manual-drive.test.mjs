import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {manualDrive} from '../src/manual-drive.js';
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
test('straight, pivot and forward arcs run at the recorded manual pace',()=>{
  const cases=[manualDrive(1,0),manualDrive(-1,0),manualDrive(0,1),manualDrive(0,-1),manualDrive(1,.5),manualDrive(1,-.5),manualDrive(1,.25),manualDrive(1,1),manualDrive(1,-1)];
  // Straight and pivot at PWM 181 (0.6); keyboard forward+turn keeps the outside wheel at 255 and the inside at 0.
  assert.deepEqual(motorOutputs(cases),[[181,181],[-181,-181],[181,-181],[-181,181],[236,126],[126,236],[209,153],[255,0],[0,255]]);
});
test('forward joystick diagonals keep translation instead of becoming pure spins',()=>{
  // Actual Quest inputs after its .25 deadzone: 30- and 45-degree forward diagonals.
  const forward=(Math.sqrt(3)/2-.25)/.75, diagonal=(Math.SQRT1_2-.25)/.75;
  const cases=[manualDrive(forward,1/3),manualDrive(forward,-1/3),manualDrive(diagonal,diagonal),manualDrive(diagonal,-diagonal)];
  assert.deepEqual(motorOutputs(cases),[[198,124],[124,198],[205,0],[0,205]]);
});
test('center and invalid inputs produce zero physical output',()=>{
  assert.deepEqual(motorOutputs([manualDrive(0,0),manualDrive(NaN,Infinity)]),[[0,0],[0,0]]);
});
