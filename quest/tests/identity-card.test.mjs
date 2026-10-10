import test from 'node:test';
import assert from 'node:assert/strict';
import { IdentityCards } from '../src/identity-card.js';

function jpeg(width = 112, height = 112) {
  // A bounded SOF header; FakeImage controls the asynchronous decoder boundary.
  return Buffer.from([255,216,255,192,0,17,8,height >> 8,height & 255,width >> 8,width & 255,
    3,1,17,0,2,17,0,3,17,0,255,217]).toString('base64');
}
const thumbnail = jpeg();
const face = changes => ({name:'Mehdi',person_id:'person-1',x:.1,y:.2,w:.2,h:.3,conf:.99,thumbnail,...changes});
const packet = changes => ({type:'identity',source:'phone',frame:1,received:10,gallery_generation:0,
  age_ms:100,fresh:true,error:null,faces:[face()],...changes});

function fixture(overrides = {}) {
  let now = 0;
  let changes = 0;
  let timerID = 0;
  const timers = new Map();
  const sockets = [];
  const images = [];
  class Socket {
    constructor(url) { this.url = url; this.closed = false; sockets.push(this); }
    send(value) { this.onmessage?.({data:typeof value === 'string' ? value : JSON.stringify(value)}); }
    disconnect() { this.onclose?.(); }
    close() { this.closed = true; }
  }
  class Image {
    constructor() { this.naturalWidth=112; this.naturalHeight=112; images.push(this); }
    set src(value) { this.value=value; }
    get src() { return this.value; }
    removeAttribute() { this.value=undefined; }
    complete() { this.onload?.(); }
    fail() { this.onerror?.(); }
  }
  const cards = new IdentityCards({onChange:()=>changes++,clock:()=>now,SocketCtor:Socket,ImageCtor:Image,
    setTimeoutFn:(fn,delay)=>{ const id=++timerID; timers.set(id,{fn,at:now+delay}); return id; },
    clearTimeoutFn:id=>timers.delete(id),...overrides});
  cards.connect('ws://relay/ws/identity');
  function advance(to) {
    while (true) {
      const due=[...timers].filter(([,timer])=>timer.at<=to).sort((a,b)=>a[1].at-b[1].at)[0];
      if (!due) break;
      now=due[1].at; timers.delete(due[0]); due[1].fn();
    }
    now=to;
  }
  const context = () => {
    const text=[], drawn=[];
    return {text,drawn,save(){},restore(){},beginPath(){},roundRect(){},fill(){},stroke(){},fillRect(){},
      measureText:value=>({width:[...value].length*8}),fillText(value){text.push(value);},
      drawImage(image){drawn.push(image);}};
  };
  function draw(source='phone') {
    const ctx=context();
    const count=cards.draw(ctx,{now,source,x:10,y:20,width:300});
    return {count,...ctx};
  }
  return {cards,sockets,images,advance,draw,timers,get changes(){return changes;}};
}

test('matched name is shown with its decoded original face, not a live-camera box', () => {
  const f=fixture();
  assert.equal(f.sockets[0].url,'ws://relay/ws/identity');
  f.sockets[0].send(packet());
  assert.equal(f.draw().count,0,'A provided thumbnail must decode before its name appears');
  f.images[0].complete();
  const drawn=f.draw();
  assert.equal(drawn.count,1);
  assert.deepEqual(drawn.text,['Mehdi','LAST SEEN 0.1s']);
  assert.deepEqual(drawn.drawn,[f.images[0]]);
  assert.equal(f.images[0].src,'data:image/jpeg;base64,'+thumbnail);
  f.cards.close();
});

test('duplicate heartbeats and replay after expiry never renew the observation age', () => {
  const f=fixture();
  f.sockets[0].send(packet({age_ms:200})); f.images[0].complete();
  f.advance(700);
  f.sockets[0].send(packet({age_ms:0}));
  assert.deepEqual(f.draw().text,['Mehdi','LAST SEEN 0.9s']);
  assert.equal(f.images.length,1);
  f.advance(800);
  assert.equal(f.draw().count,0);
  f.sockets[0].send(packet({age_ms:0}));
  assert.equal(f.draw().count,0);
  assert.equal(f.images.length,1);
  f.cards.close();
});

test('a higher server-reported age can expire a heartbeat earlier, never later', () => {
  const f=fixture();
  f.sockets[0].send(packet()); f.images[0].complete();
  f.advance(100); f.sockets[0].send(packet({age_ms:600}));
  f.advance(499); assert.equal(f.draw().count,1);
  f.advance(500); assert.equal(f.draw().count,0);
  f.cards.close();
});

test('only two cards are decoded and known names take priority over Unknown', () => {
  const f=fixture();
  f.sockets[0].send(packet({faces:[face({name:'Unknown',person_id:null}),
    face({name:'Mehdi'}),face({name:'Aziz',person_id:'person-2'})]}));
  assert.equal(f.images.length,2);
  f.images.forEach(image=>image.complete());
  const drawn=f.draw();
  assert.equal(drawn.count,2);
  assert.deepEqual(drawn.text,['Mehdi','LAST SEEN 0.1s','Aziz','LAST SEEN 0.1s']);
  f.cards.close();
});

test('Unknown remains an explicit last-seen card when a thumbnail is absent', () => {
  const f=fixture();
  f.sockets[0].send(packet({faces:[face({name:'Unknown',person_id:null,thumbnail:undefined})]}));
  const drawn=f.draw();
  assert.equal(drawn.count,1);
  assert.ok(drawn.text.includes('Unknown'));
  assert.ok(drawn.text.includes('LAST SEEN 0.1s'));
  assert.equal(drawn.drawn.length,0);
  f.cards.close();
});

test('invalid names, normalized boxes and thumbnail bounds are rejected before decoding', () => {
  const bad=[{name:''},{name:'x'.repeat(49)},{name:'Mehdi\n'},{x:NaN},{x:-.01},{w:0},{x:.9,w:.2},
    {h:2},{conf:Infinity},{thumbnail:'data:image/jpeg;base64,'+thumbnail},{thumbnail:'not-base64'},
    {thumbnail:Buffer.alloc(8193,255).toString('base64')},{thumbnail:jpeg(113,112)},
    {thumbnail:jpeg(112,0)},{thumbnail:Buffer.from([255,216,255,217]).toString('base64')}];
  for (const changes of bad) {
    const f=fixture(); f.sockets[0].send(packet({faces:[face(changes)]}));
    assert.equal(f.images.length,0,JSON.stringify(changes));
    assert.equal(f.draw().count,0,JSON.stringify(changes));
    f.cards.close();
  }
});

test('fresh=false, errors and invalid packet provenance clear current cards immediately', () => {
  const bad=[{fresh:false},{error:'camera lost'},{source:null},{frame:-1},{frame:1.1},
    {received:null},{received:-1},{gallery_generation:-1},{age_ms:-1},{age_ms:1000},{age_ms:NaN},
    {type:'other'},{faces:null}];
  for (const changes of bad) {
    const f=fixture(); f.sockets[0].send(packet()); f.images[0].complete();
    assert.equal(f.draw().count,1);
    f.sockets[0].send(packet(changes));
    assert.equal(f.draw().count,0,JSON.stringify(changes));
    f.cards.close();
  }
});

test('late decodes cannot revive a face after source or gallery generation changes', () => {
  for (const changes of [{source:'usb'},{gallery_generation:1}]) {
    const f=fixture(); f.sockets[0].send(packet());
    const late=f.images[0].onload;
    f.sockets[0].send(packet({...changes,frame:2,received:11,faces:[face({name:'Aziz'})]}));
    late(); assert.equal(f.draw(changes.source ?? 'phone').count,0);
    f.images[1].complete();
    assert.ok(f.draw(changes.source ?? 'phone').text.includes('Aziz'));
    assert.ok(!f.draw(changes.source ?? 'phone').text.includes('Mehdi'));
    f.cards.close();
  }
});

test('camera source mismatch drops cards and late decode cannot restore them', () => {
  const f=fixture(); f.sockets[0].send(packet()); const late=f.images[0].onload;
  assert.equal(f.draw('usb').count,0);
  late(); assert.equal(f.draw().count,0);
  f.sockets[0].send(packet()); assert.equal(f.draw().count,0);
  f.cards.close();
});

test('a reordered older observation cannot replace a newer face', () => {
  const f=fixture();
  f.sockets[0].send(packet({frame:2,received:11,faces:[face({name:'Aziz'})]}));
  f.images[0].complete(); f.sockets[0].send(packet());
  assert.equal(f.images.length,1);
  assert.ok(f.draw().text.includes('Aziz'));
  f.cards.close();
});

test('decoder failures and late completion after expiry cannot produce a name', () => {
  const f=fixture(); f.sockets[0].send(packet()); f.images[0].fail();
  assert.equal(f.draw().count,0);
  f.sockets[0].send(packet({frame:2,received:11})); const late=f.images[1].onload;
  f.advance(900); late(); assert.equal(f.draw().count,0);
  f.cards.close();
});

test('disconnect clears immediately, reconnects once after 500ms and close stops retries', () => {
  const f=fixture(); f.sockets[0].send(packet()); const late=f.images[0].onload;
  f.sockets[0].disconnect(); late(); assert.equal(f.draw().count,0);
  f.advance(499); assert.equal(f.sockets.length,1);
  f.advance(500); assert.equal(f.sockets.length,2);
  f.sockets[1].disconnect(); f.cards.close(); f.advance(2000);
  assert.equal(f.sockets.length,2);
  assert.equal(f.timers.size,0);
});

test('malformed or oversized websocket messages clear previously valid cards', () => {
  for (const message of ['{broken','x'.repeat(128*1024+1)]) {
    const f=fixture(); f.sockets[0].send(packet()); f.images[0].complete();
    f.sockets[0].send(message); assert.equal(f.draw().count,0);
    f.cards.close();
  }
});

test('default timers preserve the browser global receiver during admission and disposal', () => {
  const originalSet=globalThis.setTimeout, originalClear=globalThis.clearTimeout;
  globalThis.setTimeout=function(fn,delay) {
    assert.equal(this,globalThis,'Native browser timer methods require their global receiver');
    return originalSet(fn,delay);
  };
  globalThis.clearTimeout=function(timer) {
    assert.equal(this,globalThis,'Native browser timer methods require their global receiver');
    return originalClear(timer);
  };
  let f;
  try {
    f=fixture({setTimeoutFn:undefined,clearTimeoutFn:undefined});
    f.sockets[0].send(packet({faces:[face({thumbnail:undefined})]}));
    assert.equal(f.draw().count,1);
    f.cards.close(); assert.equal(f.draw().count,0);
  } finally {
    f?.cards.close(); globalThis.setTimeout=originalSet; globalThis.clearTimeout=originalClear;
  }
});
