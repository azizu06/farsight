// Repeatable live-measured desktop comparison. Uses an isolated headless browser,
// never the headset or signed-in Chrome. All network requests are audited.
import { chromium } from '../quest/node_modules/playwright/index.mjs';
import { mkdir,writeFile,appendFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const args=process.argv.slice(2),option=(name,fallback)=>{const i=args.indexOf(name);return i<0?fallback:args[i+1];};
const origin=option('--url','http://127.0.0.1:8086');
const out=resolve(option('--out','.cache/dense-lab'));
const interval=Number(option('--interval','10')),rounds=Number(option('--rounds','1'));
const sizes=option('--sizes','0.012,0.024,0.04,0.06').split(',').map(Number);
const freeze=!args.includes('--live');const mode=option('--mode','local');
if(!Number.isInteger(rounds)||rounds<1||!Number.isFinite(interval)||interval<0||sizes.some(s=>!Number.isFinite(s)||s<.012||s>.1))throw Error('Invalid rounds/interval/sizes');
await mkdir(out,{recursive:true});
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--autoplay-policy=no-user-gesture-required']});
const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[],requests=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('websocket',s=>requests.push({method:'WEBSOCKET',path:new URL(s.url()).pathname}));
page.on('request',r=>{const u=new URL(r.url());requests.push({method:r.method(),path:u.pathname});});
const records=[];let failure=null;
try{
  await page.goto(origin);
  await page.evaluate(({mode,pointsOnly})=>{window.denseLab.setMode(mode);window.denseLab.setLayers({surfaces:!pointsOnly});},{mode,pointsOnly:args.includes('--points-only')});
  await page.waitForFunction(()=>window.denseLab?.stats().uploaded_points>0,{timeout:30000});
  await page.waitForTimeout(3000);
  if(freeze)await page.evaluate(()=>window.denseLab.setFrozen(true));
  const initial=await page.evaluate(()=>window.denseLab.stats());
  await writeFile(`${out}/initial.json`,JSON.stringify(initial,null,2));
  for(let round=0;round<rounds;round++){
    for(const size of sizes)for(let pose=0;pose<3;pose++)for(const surfaces of (args.includes('--compare-surfaces')?[false,true]:[!args.includes('--points-only')])){
      await page.evaluate(({size,pose,surfaces})=>{window.denseLab.setSize(size);window.denseLab.pose(pose);window.denseLab.setLayers({surfaces});},{size,pose,surfaces});
      await page.waitForTimeout(1100);
      const stats=await page.evaluate(()=>window.denseLab.stats());
      const coverage=await page.evaluate(()=>window.denseLab.coverage());
      const record={round,pose,splat_m:size,at:new Date().toISOString(),...stats,...coverage};
      const suffix=args.includes('--compare-surfaces')?(surfaces?'-textured':'-points'):'';
      const file=`r${round}-p${pose}-${Math.round(size*1000)}mm${suffix}.png`;record.screenshot=`${out}/${file}`;
      await page.locator('#viewport').screenshot({path:record.screenshot});
      records.push(record);await appendFile(`${out}/samples.jsonl`,JSON.stringify(record)+'\n');
      console.log(JSON.stringify({pose,size,points:record.points_drawn,coverage:record.coverage,holes:record.hole_count,fps:record.fps,screenshot:record.screenshot}));
    }
    if(round+1<rounds)await page.waitForTimeout(interval*1000);
  }
  const forbidden=requests.filter(r=>r.method!=='GET'||r.path.includes('/ws/control')||r.path.includes('/ws/macview-pose'));
  if(forbidden.length)throw Error(`Non-readonly requests: ${JSON.stringify(forbidden)}`);
  if(errors.length)throw Error(errors.join('\n'));
}catch(error){failure=String(error);throw error;}finally{
  const finalStats=await page.evaluate(()=>window.denseLab?.stats()).catch(()=>null);
  await writeFile(`${out}/report.json`,JSON.stringify({scope:'Live measured RGB-D / desktop Chromium SwiftShader; not headset FPS or geometry completeness',frozen:freeze,records,errors,requests,failure,final_stats:finalStats},null,2));
  const fields=['round','pose','splat_m','raw_surfaces','raw_triangles','points_drawn','uploaded_points','coverage','object_coverage','hole_count','fps','data_age_ms','capture_age_ms','screenshot'];
  await writeFile(`${out}/samples.csv`,fields.join(',')+'\n'+records.map(r=>fields.map(f=>JSON.stringify(r[f]??null)).join(',')).join('\n'));
  await browser.close();
}
