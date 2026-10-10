// Cosmetic evidence cards: the name belongs to the shown face thumbnail, never
// to an unrelated position in a newer live camera image. No control decisions.
const MAX_AGE_MS = 1000;
const MAX_MESSAGE_CHARS = 128 * 1024;
const MAX_THUMBNAIL_BYTES = 8192;

function thumbnailInfo(encoded) {
  if (typeof encoded !== 'string' || !encoded.length || encoded.length % 4
      || encoded.length > Math.ceil(MAX_THUMBNAIL_BYTES / 3) * 4
      || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) return null;
  let bytes;
  try { bytes = atob(encoded); } catch { return null; }
  const byte = i => bytes.charCodeAt(i);
  const word = i => (byte(i) << 8) | byte(i + 1);
  if (bytes.length > MAX_THUMBNAIL_BYTES || bytes.length < 12 || word(0) !== 0xffd8
      || word(bytes.length - 2) !== 0xffd9) return null;
  // Bound decoded dimensions before assigning Image.src, not only after decode.
  let cursor = 2;
  while (cursor < bytes.length - 2) {
    if (byte(cursor) !== 0xff) return null;
    while (byte(cursor) === 0xff) cursor++;
    const marker = byte(cursor++);
    if (marker === 0xda || marker === 0xd9 || marker === 0x00 || marker === 0xd8
        || (marker >= 0xd0 && marker <= 0xd7) || cursor + 2 > bytes.length) return null;
    const length = word(cursor);
    if (length < 2 || cursor + length > bytes.length) return null;
    if ([0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf].includes(marker)) {
      if (length < 8) return null;
      const height = word(cursor + 3), width = word(cursor + 5);
      if (!width || !height || width > 112 || height > 112) return null;
      return {width, height, url:'data:image/jpeg;base64,' + encoded};
    }
    cursor += length;
  }
  return null;
}

function validFace(face) {
  if (!face || typeof face.name !== 'string' || !face.name.trim() || [...face.name].length > 48
      || /\p{C}/u.test(face.name)) return null;
  if (![face.x,face.y,face.w,face.h].every(Number.isFinite) || face.x < 0 || face.y < 0
      || face.w <= 0 || face.h <= 0 || face.x + face.w > 1 || face.y + face.h > 1) return null;
  if (face.conf !== undefined && (!Number.isFinite(face.conf) || face.conf < 0 || face.conf > 1)) return null;
  const thumbnail = face.thumbnail === undefined || face.thumbnail === null ? null : thumbnailInfo(face.thumbnail);
  if (face.thumbnail != null && !thumbnail) return null;
  return {name:face.name, known:typeof face.person_id === 'string' && face.name !== 'Unknown',
    thumbnail, image:null, loader:null, failed:false};
}

function fitText(context, value, width) {
  if (context.measureText(value).width <= width) return value;
  const letters = [...value];
  while (letters.length > 1 && context.measureText(letters.join('') + '…').width > width) letters.pop();
  return letters.join('') + '…';
}

export class IdentityCards {
  constructor({onChange=()=>{},clock=()=>performance.now(),ImageCtor=globalThis.Image,
    SocketCtor=globalThis.WebSocket,setTimeoutFn=(fn,delay)=>globalThis.setTimeout(fn,delay),
    clearTimeoutFn=id=>globalThis.clearTimeout(id)} = {}) {
    this.onChange=onChange; this.clock=clock; this.ImageCtor=ImageCtor; this.SocketCtor=SocketCtor;
    this.setTimer=setTimeoutFn; this.clearTimer=clearTimeoutFn;
    this.socket=null; this.url=null; this.closed=false; this.packet=null; this.last=null;
    this.entries=[]; this.revision=0; this.expiry=null; this.expiryDeadline=null; this.reconnect=null;
  }

  connect(url) {
    if (typeof url !== 'string' || !url) throw new TypeError('Identity WebSocket URL is required');
    if (!this.closed && this.url === url && this.socket) return;
    if (this.url !== url) this.last=null;
    this._disconnect(); this._clear(); this.closed=false; this.url=url;
    this._open();
  }

  _open() {
    if (this.closed || !this.url) return;
    let socket;
    try { socket=new this.SocketCtor(this.url); }
    catch { this._clear(); this._retry(); return; }
    this.socket=socket;
    socket.onmessage = event => {
      if (this.closed || this.socket !== socket) return;
      if (typeof event.data !== 'string' || event.data.length > MAX_MESSAGE_CHARS) { this._clear(); return; }
      try { this._receive(JSON.parse(event.data)); } catch { this._clear(); }
    };
    const lost = () => {
      if (this.closed || this.socket !== socket) return;
      this._disconnectSocket(); this._clear(); this._retry();
    };
    socket.onclose=lost; socket.onerror=lost;
  }

  _retry() {
    if (this.closed || this.reconnect !== null) return;
    this.reconnect=this.setTimer(() => { this.reconnect=null; this._open(); },500);
    this.reconnect?.unref?.();
  }

  _disconnectSocket() {
    const socket=this.socket;
    this.socket=null;
    if (!socket) return;
    socket.onmessage=socket.onerror=socket.onclose=socket.onopen=null;
    try { socket.close(); } catch { /* Connection may already be closed. */ }
  }

  _disconnect() {
    if (this.reconnect !== null) this.clearTimer(this.reconnect);
    this.reconnect=null; this._disconnectSocket();
  }

  _clear() {
    const hadData=Boolean(this.packet || this.entries.length);
    this.revision++;
    if (this.expiry !== null) this.clearTimer(this.expiry);
    this.expiry=null; this.expiryDeadline=null;
    for (const entry of this.entries) {
      if (entry.loader) { entry.loader.onload=entry.loader.onerror=null; entry.loader.removeAttribute?.('src'); }
      entry.loader=entry.image=null;
    }
    this.packet=null; this.entries=[];
    // Keep last as a tombstone: replaying an expired/cleared frame cannot renew it.
    if (hadData) this.onChange();
  }

  _age(now) { return this.packet.ageBase + Math.max(0, now - this.packet.arrival); }

  _scheduleExpiry() {
    if (!this.packet) return;
    const deadline=this.packet.arrival + MAX_AGE_MS - this.packet.ageBase;
    if (this.expiry !== null && this.expiryDeadline === deadline) return;
    if (this.expiry !== null) this.clearTimer(this.expiry);
    this.expiryDeadline=deadline;
    const packet=this.packet, revision=this.revision;
    this.expiry=this.setTimer(() => {
      this.expiry=null; this.expiryDeadline=null;
      if (this.packet !== packet || this.revision !== revision) return;
      if (this._age(this.clock()) >= MAX_AGE_MS) this._clear();
      else this._scheduleExpiry();
    },Math.max(1,deadline-this.clock()));
    this.expiry?.unref?.();
  }

  _receive(packet) {
    if (!packet || packet.type !== 'identity' || packet.fresh !== true || packet.error
        || typeof packet.source !== 'string' || !packet.source || packet.source.length > 128
        || !Number.isSafeInteger(packet.frame) || packet.frame < 0
        || !Number.isFinite(packet.received) || packet.received < 0
        || !Number.isSafeInteger(packet.gallery_generation) || packet.gallery_generation < 0
        || !Number.isFinite(packet.age_ms) || packet.age_ms < 0 || packet.age_ms >= MAX_AGE_MS
        || !Array.isArray(packet.faces) || packet.faces.length > 8) { this._clear(); return; }
    const key=JSON.stringify([packet.source,packet.frame,packet.received,packet.gallery_generation]);
    const now=this.clock();
    if (key === this.last?.key) {
      if (!this.packet) return;
      const ageBase=Math.max(this.packet.ageBase,packet.age_ms-Math.max(0,now-this.packet.arrival));
      if (ageBase > this.packet.ageBase) { this.packet.ageBase=ageBase; this._scheduleExpiry(); this.onChange(); }
      return;
    }
    if (this.last?.source === packet.source && (packet.received < this.last.received
        || (packet.received === this.last.received && packet.frame < this.last.frame))) return;
    this._clear();
    this.packet={key,source:packet.source,frame:packet.frame,received:packet.received,
      generation:packet.gallery_generation,arrival:now,ageBase:packet.age_ms};
    this.last=this.packet;
    this.entries=packet.faces.map(validFace).filter(Boolean).sort((a,b)=>Number(b.known)-Number(a.known)).slice(0,2);
    this._scheduleExpiry();
    for (const entry of this.entries) this._decode(entry);
    this.onChange();
  }

  _decode(entry) {
    if (!entry.thumbnail) return;
    const packet=this.packet, revision=this.revision;
    let image;
    try { image=new this.ImageCtor(); } catch { entry.failed=true; return; }
    entry.loader=image;
    const current = () => this.packet === packet && this.revision === revision && this.entries.includes(entry)
      && this._age(this.clock()) < MAX_AGE_MS;
    image.onload = () => {
      if (!current()) return;
      if (image.naturalWidth !== entry.thumbnail.width || image.naturalHeight !== entry.thumbnail.height) entry.failed=true;
      else entry.image=image;
      this.onChange();
    };
    image.onerror = () => { if (current()) { entry.failed=true; this.onChange(); } };
    image.src=entry.thumbnail.url;
  }

  /** Draw up to two bounded last-seen cards; returns the number drawn. */
  draw(context,{now=this.clock(),source='',x=0,y=0,width=300} = {}) {
    if (!this.packet) return 0;
    if (source !== this.packet.source || !Number.isFinite(now) || this._age(now) >= MAX_AGE_MS) { this._clear(); return 0; }
    if (![x,y,width].every(Number.isFinite) || width < 120) return 0;
    const cardWidth=Math.min(width,300), age=this._age(now);
    let count=0;
    context.save(); context.textAlign='left'; context.textBaseline='middle';
    for (const entry of this.entries) {
      if (entry.failed || (entry.thumbnail && !entry.image)) continue;
      const top=y+count*80;
      context.fillStyle='#081922ee'; context.strokeStyle='#6de2ca'; context.lineWidth=1;
      context.beginPath(); context.roundRect(x,top,cardWidth,72,10); context.fill(); context.stroke();
      if (entry.image) context.drawImage(entry.image,x+8,top+8,56,56);
      else { context.fillStyle='#18323d'; context.fillRect(x+8,top+8,56,56); context.fillStyle='#85a8b7'; context.font='11px system-ui'; context.fillText('FACE',x+19,top+36); }
      context.fillStyle=entry.known?'#baf7e7':'#e1ebf0'; context.font='bold 19px system-ui';
      context.fillText(fitText(context,entry.name,cardWidth-84),x+74,top+27);
      context.fillStyle='#8dacba'; context.font='11px system-ui';
      context.fillText(`LAST SEEN ${(age/1000).toFixed(1)}s`,x+74,top+49);
      count++;
    }
    context.restore(); return count;
  }

  close() { this.closed=true; this._disconnect(); this._clear(); }
}
