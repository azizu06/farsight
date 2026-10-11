import assert from 'node:assert/strict';
import {test} from 'node:test';
import {HeadSteering} from '../src/head-steering.js';
import {manualDrive} from '../src/manual-drive.js';

const rad = degrees => degrees * Math.PI / 180;
const pose = (degrees, extra = {}) => ({yaw_rad:rad(degrees), age_ms:0, source:'arkit',
  session_id:'phone', map_epoch:1, socket_generation:1, ...extra});
const step = (control, head, rover, extra={}) => control.step(rad(head),pose(rover,extra),100,100);

test('head steering is opt-in and aligns two independent coordinate frames on activation',()=>{
  const c=new HeadSteering();
  assert.equal(step(c,40,120).w,0);
  c.setEnabled(true);
  assert.equal(step(c,40,120).w,0);
  assert.ok(step(c,70,120).w<0,'left head turn commands left (negative normalized w)');
  assert.ok(step(c,10,120).w>0,'right head turn commands right');
});
test('measured rover rotation closes the loop, including turns across the pi boundary',()=>{
  const c=new HeadSteering();c.setEnabled(true);step(c,10,170);
  const before=step(c,40,170), caught=step(c,40,-160);
  assert.ok(before.w<0);assert.equal(caught.w,0);
  assert.ok(Math.abs(caught.cameraYaw-rad(40))<1e-9,'camera screen follows the measured rover heading');
  assert.ok(step(c,-10,170).w>0,'short right turn, not a full-circle left turn');
});
test('forward and reverse retain joystick authority while head steering turns in place or on an arc',()=>{
  const c=new HeadSteering();c.setEnabled(true);step(c,0,0);
  const {w}=step(c,-30,0);
  assert.ok(w>0);
  assert.equal(manualDrive(1,w).v,1);assert.equal(manualDrive(-1,w).v,-1);
  assert.deepEqual(manualDrive(0,w),{v:0,w});
  assert.equal(step(c,.2,0).w,0,'head tracking jitter does not create a turn');
});
test('joystick steering overrides head follow and hands back at the new orientation',()=>{
  const c=new HeadSteering();c.setEnabled(true);step(c,0,0);
  assert.equal(c.step(rad(30),pose(10),100,100,.6).w,.6);
  assert.equal(step(c,30,10).w,0,'no snap back after releasing a deliberate stick turn');
  assert.ok(step(c,50,10).w<0);
  c.recenter();assert.equal(step(c,50,10).w,0);
  c.setEnabled(false);assert.equal(step(c,80,10).w,0);
});
test('only current measured phone pose supplies heading, and a new sensor world realigns',()=>{
  const c=new HeadSteering();c.setEnabled(true);step(c,0,0);
  assert.equal(c.step(rad(40),null,100,100).ready,false);
  assert.equal(c.step(rad(40),pose(0,{age_ms:1000}),100,100).w,0);
  assert.equal(c.step(rad(40),pose(0,{yaw_rad:NaN}),100,100).ready,false);
  assert.equal(c.step(rad(40),pose(0),1200,100).ready,false,'receipt delay counts toward pose age');
  assert.equal(step(c,40,0).w,0,'pose recovery aligns instead of replaying an old target');
  assert.equal(step(c,70,90,{map_epoch:2}).w,0);
  assert.equal(step(c,90,100,{socket_generation:2}).w,0);
});
