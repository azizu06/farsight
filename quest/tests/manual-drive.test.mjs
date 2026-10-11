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
test('straight and steering-priority commands reach actual configured maximum PWM',()=>{
  const cases=[manualDrive(1,0),manualDrive(-1,0),manualDrive(1,.5),manualDrive(1,-.5),manualDrive(1,.25)];
  assert.deepEqual(motorOutputs(cases),[[255,255],[-255,-255],[255,-255],[-255,255],[255,0]]);
});
test('center and invalid inputs produce zero physical output',()=>{
  assert.deepEqual(motorOutputs([manualDrive(0,0),manualDrive(NaN,Infinity)]),[[0,0],[0,0]]);
});
