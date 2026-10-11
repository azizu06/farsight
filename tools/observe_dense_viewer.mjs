// Repeatable live-measured desktop comparison. Uses an isolated headless browser,
// never the headset or signed-in Chrome. All network requests are audited.
import { chromium } from '../quest/node_modules/playwright/index.mjs';
import { mkdir,writeFile,appendFile } from 'node:fs/promises';
import { resolve, basename } from 'node:path';
const args=process.argv.slice(2),option=(name,fallback)=>{const i=args.indexOf(name);return i<0?fallback:args[i+1];};
const origin=option('--url','http://127.0.0.1:8086');
const out=resolve(option('--out','.cache/dense-lab'));
const interval=Number(option('--interval','10')),rounds=Number(option('--rounds','1'));
const sizes=option('--sizes','0.012,0.024,0.04,0.06').split(',').map(Number);
const freeze=!args.includes('--live');const mode=option('--mode','local');
const compare=args.includes('--compare-surfaces');
if(!Number.isInteger(rounds)||rounds<1||!Number.isFinite(interval)||interval<0||sizes.some(s=>!Number.isFinite(s)||s<.012||s>.1))throw Error('Invalid rounds/interval/sizes');
if(compare&&(!freeze||mode!=='raw'))throw Error('--compare-surfaces requires frozen --mode raw; omit --live');
await mkdir(out,{recursive:true});
await writeFile(`${out}/samples.jsonl`,'');
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--autoplay-policy=no-user-gesture-required']});
const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[],requests=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('websocket',s=>requests.push({method:'WEBSOCKET',path:new URL(s.url()).pathname}));
page.on('request',r=>{const u=new URL(r.url());requests.push({method:r.method(),path:u.pathname});});
const records=[];let failure=null,initial=null;
const fingerprint=s=>JSON.stringify([s.mode,s.raw_identity,s.raw_frame,s.raw_points,s.raw_triangles,s.uploaded_points]);
const forbidden=()=>requests.filter(r=>r.method!=='GET'||r.path.includes('/ws/control')||r.path.includes('/ws/macview-pose'));
try{
  await page.goto(origin);
  await page.evaluate(({mode,pointsOnly})=>{window.denseLab.setMode(mode);window.denseLab.setLayers({surfaces:!pointsOnly});},{mode,pointsOnly:args.includes('--points-only')});
  await page.waitForFunction(()=>window.denseLab?.stats().uploaded_points>0,{timeout:30000});
  await page.waitForTimeout(3000);
  if(freeze)await page.evaluate(()=>window.denseLab.setFrozen(true));
  initial=await page.evaluate(()=>window.denseLab.stats());
  await writeFile(`${out}/initial.json`,JSON.stringify(initial,null,2));
  for(let round=0;round<rounds;round++){
    for(const size of sizes)for(let pose=0;pose<3;pose++)for(const surfaces of (compare?[false,true]:[!args.includes('--points-only')])){
      await page.evaluate(({size,pose,surfaces})=>{window.denseLab.setSize(size);window.denseLab.pose(pose);window.denseLab.setLayers({surfaces});},{size,pose,surfaces});
      await page.waitForTimeout(1100);
      const stats=await page.evaluate(()=>window.denseLab.stats());
      if(compare&&(!stats.frozen||fingerprint(stats)!==fingerprint(initial)))throw Error('Captured geometry changed during surface comparison');
      const coverage=await page.evaluate(()=>window.denseLab.coverage());
      const record={round,pose,splat_m:size,at:new Date().toISOString(),...stats,...coverage};
      const suffix=compare?(surfaces?'-textured':'-points'):'';
      const file=`r${round}-p${pose}-${Math.round(size*1000)}mm${suffix}.png`;record.screenshot=`${out}/${file}`;
      await page.locator('#viewport').screenshot({path:record.screenshot});
      records.push(record);await appendFile(`${out}/samples.jsonl`,JSON.stringify(record)+'\n');
      console.log(JSON.stringify({pose,size,points:record.points_drawn,coverage:record.coverage,holes:record.hole_count,fps:record.fps,screenshot:record.screenshot}));
    }
    if(round+1<rounds)await page.waitForTimeout(interval*1000);
  }
  if(forbidden().length)throw Error(`Non-readonly requests: ${JSON.stringify(forbidden())}`);
  if(errors.length)throw Error(errors.join('\n'));
}catch(error){failure=String(error);throw error;}finally{
  const finalStats=await page.evaluate(()=>window.denseLab?.stats()).catch(()=>null);
  const comparisons=compare?records.filter(r=>r.raw_surfaces).map(textured=>{
    const points=records.find(r=>!r.raw_surfaces&&r.round===textured.round&&r.pose===textured.pose&&r.splat_m===textured.splat_m);
    return {round:textured.round,pose:textured.pose,splat_m:textured.splat_m,raw_identity:textured.raw_identity,raw_frame:textured.raw_frame,
      points_screenshot:points.screenshot,textured_screenshot:textured.screenshot,points_coverage:points.coverage,textured_coverage:textured.coverage,
      coverage_delta:textured.coverage-points.coverage};
  }):[];
  const scope='Measured RGB-D / isolated desktop Chromium SwiftShader; capture source recorded below. Not headset FPS or geometry completeness.';
  await writeFile(`${out}/report.json`,JSON.stringify({scope,frozen:freeze,initial,records,comparisons,errors,requests,read_only:forbidden().length===0,failure,final_stats:finalStats},null,2));
  const fields=['round','pose','splat_m','raw_surfaces','raw_triangles','points_drawn','uploaded_points','coverage','object_coverage','hole_count','fps','data_age_ms','capture_age_ms','screenshot'];
  await writeFile(`${out}/samples.csv`,fields.join(',')+'\n'+records.map(r=>fields.map(f=>JSON.stringify(r[f]??null)).join(',')).join('\n'));
  const escape=value=>String(value??'unknown').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const percent=value=>`${(value*100).toFixed(2)}%`;
  await writeFile(`${out}/index.html`,`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Farsight measured surface comparison</title>
<style>:root{color-scheme:dark;font:15px system-ui;background:#10151b;color:#e5ecf5}body{max-width:1400px;margin:32px auto;padding:0 20px}h1{font-size:24px}small,p{color:#b6cbdc}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:20px}figure{margin:0;background:#1b2836;padding:12px}img{width:100%}code{overflow-wrap:anywhere}.failure{color:#ffb2ac}</style>
<h1>Measured surface comparison</h1><p>${escape(scope)}</p><p>${freeze?'Frozen historical sample':'Live samples'} · ${escape(initial?.raw_source??initial?.source)} · frame ${escape(initial?.raw_frame)} · ${escape(initial?.uploaded_points)} measured points</p><p><code>${escape(initial?.raw_identity??initial?.capture_identity)}</code></p>
${failure?`<p class="failure">Incomplete: ${escape(failure)}</p>`:''}
<p>Read-only request audit: ${forbidden().length?'FAILED':'passed'}. <a href="report.json">Full report</a> · <a href="samples.csv">CSV</a></p>
${comparisons.map(c=>`<p>Pose ${c.pose}, ${Math.round(c.splat_m*1000)} mm: points ${percent(c.points_coverage)} → textured ${percent(c.textured_coverage)} canvas coverage (${(c.coverage_delta*100).toFixed(2)} percentage points). Empty regions remain unknown.</p>`).join('')}
<main>${records.map(r=>`<figure><a href="${escape(basename(r.screenshot))}"><img loading="lazy" src="${escape(basename(r.screenshot))}" alt="Pose ${r.pose}, ${r.raw_surfaces?'textured surfaces and points':'points'}"></a><figcaption>Round ${r.round} / pose ${r.pose} · ${Math.round(r.splat_m*1000)} mm · ${r.raw_surfaces?'textured surfaces + points':'points'}<br>${percent(r.coverage)} canvas coverage · ${escape(r.raw_triangles)} measured faces · ${Math.round(r.fps)} desktop fps</figcaption></figure>`).join('')}</main></html>`);
  await browser.close();
}
