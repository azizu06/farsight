import json,re,statistics,sys
from pathlib import Path
results=json.loads((Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name('evidence.json')).read_text())
for result in results:
 stages={}
 for event in result['debug_events']:
  line=event['line'];stage=key=None
  for candidate,token,pattern in [('demux','demuxer ->',r'pkt_pts:(-?\d+)'),('decode','decoder ->',r'(?<!_)pts:(-?\d+)'),('filter','filter ->',r'(?<!_)pts:(-?\d+)'),('encode_in','encoder <-',r'frame_pts:(-?\d+)'),('encode_out','encoder ->',r'pkt_pts_time:([\d.]+)'),('mux','muxer <-',r'pts_time:([\d.]+)')]:
   if token in line:
    match=re.search(pattern,line)
    if match:
     stage=candidate; key=round(float(match[1])*30) if candidate in ('encode_out','mux') else int(match[1])
    break
  if stage is not None: stages.setdefault(key,{})[stage]=event['arrival']
 for fragment in result['fragments']:stages.setdefault(fragment['first'],{})['fragment']=fragment['arrival']
 for key,entry in stages.items():
  if str(key) in result['inputs']:entry['input']=result['inputs'][str(key)]['begin']
 samples=[entry for key,entry in stages.items() if 30<=key<75]
 metrics={}
 for a,b in [('input','demux'),('demux','decode'),('decode','encode_in'),('encode_in','encode_out'),('encode_out','mux'),('mux','fragment'),('input','fragment')]:
  durations=[(entry[b]-entry[a])*1000 for entry in samples if a in entry and b in entry]
  if durations:metrics[a+'→'+b]=round(statistics.median(durations),3)
 print(result['case'],json.dumps(metrics), 'ordinal30',stages.get(30))
