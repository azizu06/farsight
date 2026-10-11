import json,re,html,datetime
from pathlib import Path
ROOT=Path.cwd(); D=ROOT/'data/farsight-rivals'
gallery=json.loads((D/'gallery.json').read_text()); groups=json.loads((D/'tracks.json').read_text()); projects=json.loads((D/'projects.json').read_text())
all_sponsors=json.loads((D/'all-sponsors.json').read_text())
all_members={t['track']:{c['url'] for p in t['pages'] for c in p['cards']} for t in all_sponsors}
byurl={p['url']:p for p in projects}; cs=json.loads((D/'candidates.json').read_text()); byid={i:byurl[c['url']] for i,c in enumerate(cs)}
reasons={
0:'Adaptive interview automation is useful, but less distinctive than embodied perception; live Jetson contribution needs to be made explicit.',
1:'Ambitious self-improving arm and synthetic-data pipeline; the author admits calibration makes practical manipulation difficult.',
2:'Private policy RAG has clear provenance and local embeddings, but a narrower, familiar technical scope.',
3:'Broad study utility; handwritten-note conversion is useful, but the bundle dilutes the core AI story.',
4:'MIDI supplies measured learner performance and accessible spoken coaching; camera-based technique assessment is explicitly unfinished.',
5:'Accessible comics combine panel sequencing, narration and face input; strong user story, less physical integration.',
6:'Interactive drawing feedback and generated references suit teaching; differentiation rests on the actual feedback quality.',
7:'A trained classifier over 3,526 projects is more than a prompt wrapper, but winner-like prose is an uncertain proxy for real judging.',
8:'Sophisticated multi-sensor delivery stack; the full delivery chain is reported in simulation, with a smaller proven physical layer.',
9:'EEG communication has high impact and difficult signal processing; real end-to-end acquisition and decoding evidence is less concrete.',
10:'Inspection replay and anomaly review are useful; this is a provided-data dashboard rather than a new sensing robot.',
11:'Real phone motion and video become synced crash evidence in about 20 seconds by the team’s account; estimates are clearly labelled.',
12:'Local Qwen and SigLIP2 make grocery interaction useful; narrower novelty and hardware challenge than a rover workflow.',
13:'Spatial captions, directional haptics and a measured open-model bakeoff create a strong accessibility story and concrete sensing loop.',
14:'Stereo neural hand tracking drives a real arm; reported latency and pick-and-place results make the hard robotics work easy to assess.',
15:'Group concert discovery is useful product work; the sponsor AI story is less central than specialized perception.',
16:'Custom coding-model work is directly relevant to open AI; comparative coding quality and deployed reliability remain uncertain.',
17:'Practice judging is interactive, but simulated rivals and placements are not validation of real-world predictive performance.',
18:'Semantic exploration of 57,000 projects and team critique is substantive software; weaker direct sensing and embodied differentiation.',
19:'One camera grades human and robot builds with the same rules; a complete, understandable feedback loop and measured limits.',
20:'Offline cited survival guidance and local plant vision fit edge AI; practical value depends on answer quality, not model count.',
21:'Live left/right EOG selection and deliberate blink-to-speak provide a compelling assistive demo; electrode drift constrains robustness.',
22:'Deterministic SEC filing diffs with semantic matching have clear provenance; a more conventional application than embodied AI.',
23:'An editable local DAW with AI presets is usable creative tooling; model contribution is narrower than the strongest open-AI entries.',
24:'Local interactive story generation with a child’s drawing is engaging; actual on-Jetson throughput and scene consistency matter.',
25:'Pose-based dance coaching is a natural perception training use; scored movement quality needs a convincing comparison.',
26:'Mobile form coaching offers clear user utility; injury-related benefit and generalization are not established by the write-up.',
27:'Webcam boxing makes perception immediately visible; serious-training value and technical claims need demonstration.',
28:'Bias-aware bin placement is a useful analytical story, with estimates correctly distinguished from measured environmental outcomes.',
29:'Broad construction intelligence ambition; integrated delivered behavior is harder to isolate from the extensive platform vision.',
30:'Team-aware task assignment and planning are coherent software, but less technically differentiated than custom sensing and robotics.',
31:'Camera plus RF monitoring is real sensor integration; gaze and RF activity do not independently establish cheating.',
32:'Offline video redaction has published real-clip coverage and baseline comparisons; unusually concrete evaluation for an AI application.',
33:'Open models explain supplied inspection data with evidence; patrol is explicitly simulated, so compare it as software.',
34:'Wearable visual Q&A has accessibility value; server-side assistance and broad claims are less distinctive than a measured local loop.',
35:'Offline indoor AR combines pose, walls and sign reading; a strong spatial-computing rival if relocalization works live.',
36:'Disaster dispatch combines physical-world simulation, noisy reports and agent decisions scored against hidden truth; excellent training fit.',
37:'Video speech coaching is useful; the write-up offers less evidence of distinctive model or sensor engineering.',
38:'Face memory, saved frames and spatial guidance overlap Farsight’s evidence story; recognition reliability needs careful demonstration.',
39:'In-browser image analysis for sewing-pattern risk is focused and private; reliability against real listings is the key uncertainty.',
40:'Circuit compilation over multiple seeds and many metrics shows serious engineering; niche impact and judging accessibility may limit reach.',
41:'Joint-angle estimation is a clear, modest prototype; weaker novelty and end-to-end scope than the field leaders.',
42:'Appearance-to-music classification is entertaining, but prediction validity and meaningful real-world benefit are weaker.',
43:'A physical Braille cell and learner-data pipeline are compelling; next steps still include object-to-cell and teaching integration.',
44:'Airport taxi pathfinding, conflicts and controller monitoring give a concrete serious-simulation workflow.',
45:'Two-camera audience feedback plus participant signals is coherent; attention estimates need validation beyond head pose.',
46:'An axe-core accessibility audit plus explanatory AI offers practical value; much of the detection comes from established tooling.',
47:'Wi-Fi fingerprinting and hybrid routing are relevant to spatial navigation; cross-building accuracy is the crucial missing comparison.',
48:'Custom belt and ring integrate depth, object memory and last-meter guidance; substantial sensor fusion with a clear wearer interaction.',
49:'Real hazard sensors and offline guidance have a strong resilience story; hurricane benchmark claims need broader external validation.',
50:'Evidence-backed inspection briefs and hidden-failure analysis are substantial, but rely on supplied mission data.',
51:'VR rover control, onboard perception and local voice plus LiDAR-seeded rooms have exceptional breadth; live reliability and demo focus decide the result.',
52:'Deterministic Werewolf replay with model-deception labels is a useful AI experiment; social-game results have limited external validity.',
53:'Persistent edge event tracking is meaningful Jetson work; a fixed-camera workflow has less mechanical breadth but may demo more reliably.',
54:'Spare-device sound and pose alerts have clear accessibility utility; end-to-end sensing and caption attribution are key uncertainties.',
55:'CoreML conversion and on-device parity are substantive; real unseen deepfake accuracy matters more than a confident verdict.',
56:'Security-operator training with gaze tracking fits serious simulation; show that perception changes the trainee’s task.',
57:'Large multi-agent governance simulation is ambitious; plausible-looking behavior is not evidence of realistic social dynamics.',
58:'A real shell-game reveal tests 3D memory directly; reported 7 recorded and 3 live successes plus custom quantization make a formidable rival.',
59:'Open-weight campus video generation demonstrates deployment work; primarily an imaginative visual experience rather than sensing or safety.',
60:'Weather and camera evidence govern a simulated robot round with explicit human review; focused perception and operations fit.',
61:'Jetson smoke detection and real weather sensing feed small alert packets; the satellite link and most fleet units are simulated.',
62:'Camera waste recognition has environmental utility; familiar detector-plus-task-list scope limits differentiation.',
63:'Route optimization and object recognition support cleanup simulation; it is not evidence of a physically working cleanup robot.',
64:'Jetson memory, cited answers and multiple connectors have quantified soak and throughput claims; training on AMD is separated from edge runtime.',
65:'The closest architecture rival: VR survey rover, SLAM, Nav2 and open plant models; staged surveys and past power/network problems temper confidence.',
66:'LiDAR-to-property digital twins are relevant spatial software; no new capture hardware or field accuracy is established here.',
67:'Attack-graph visualization and model summaries are useful, but the log stream is simulated and the hardware connection is weak.',
68:'Park camera recognition and community sightings are useful ecology software; sensing deployment and label quality are unclear.',
69:'Offline Jetson vision, ultrasonic haptics and local Q&A have a clear wearer benefit; loose wiring and broad testing remain limitations.',
70:'DNS-only app-contact visualization is unusually clear and privacy-limited; AI explanation is supporting functionality.',
71:'A custom BLE fidget controller and phone games have tangible interaction polish; captured data has narrower downstream use.',
72:'Sightings and a campus radar create a playful product; sensor/model novelty and independent measurement are limited.',
73:'Timestamped source-video matching preserves evidence; the exact-match workflow is stronger than unconstrained fact-checking claims.',
74:'A trained posture model on a 2 GB Nano connects to nurse response; reported locked-test metrics and live 13 FPS strengthen execution evidence.',
75:'Tracker visualization is understandable privacy tooling; model novelty and hardware capture are limited.',
76:'A franchise constellation is approachable product design; comparatively little technical or sensing difficulty.',
77:'Custom flight electronics and live thermal detection are strong SAR hardware; stable hover and camera tests are not an integrated autonomous search flight.',
78:'A playable extraction game with pathfinding is tangible; the serious-training purpose is weaker than purpose-built safety simulations.',
79:'A Bluetooth Bop It controller plus custom rhythm game offers a direct physical loop, but weaker serious-training scope.',
80:'Shopping-route optimization addresses mobility costs and uses pathfinding; a comparatively narrow simulation.',
81:'Multiple accessible inputs drive 2048; a functional beginner scope rather than a large integrated perception system.',
82:'Face-expression input directly drives gameplay; serious-training benefit is less explicit than the perception mechanic.',
83:'Coding lessons and an AI companion make a coherent educational game; show the role of perception/pathfinding for this sponsor.',
84:'Custom PPO swarms, GPU simulation and held-out comparisons offer deep technical work with an excellent serious-training story.',
85:'Live registered-face matching is a clear perception loop; attendance utility is conventional and misidentification remains important.',
86:'Depth, head tracking and mathematical effects create distinct visual tooling; serious-simulation purpose is less direct.',
87:'EMG-driven VR archery and haptic feedback have strong physical interaction; rehabilitation or training transfer is not demonstrated.',
88:'Blink-controlled Morse practice makes a specific hands-free learning task; narrower scope than integrated field training.',
89:'A small puzzle-platform game has path-planning flavor; the write-up provides little technical or serious-simulation evidence.',
90:'Gesture and voice manipulation make a tangible 3D workspace; a concrete training goal is less clear.',
91:'A scored evacuation drill and deterministic replay are strong teaching design; physical Quest testing and fuller pathfinding remain future work.',
92:'Tower-defense design has potential, but the page describes major gameplay in future tense.',
93:'Wearable follow/orbit drone control is ambitious embodied interaction; integrated flight demonstration needs close scrutiny.',
94:'Reef-avoiding routes are a relevant environmental simulation; real navigation accuracy and user validation remain open.',
95:'A coding-themed defense game uses pathfinding; educational outcomes and polish are less documented.',
96:'Moisture sensors turn watering into a physical game; creative and tangible, but less computational breadth.',
97:'Projected rehabilitation exercises and Jetson hand tracking have clear utility; fuller robot integration is still future work.',
98:'Jetson hand tracking turns a projector into a teaching surface; easy to demonstrate, but less autonomous reasoning.',
99:'Construction rover perception is directly relevant; the page explicitly leaves autonomous driving and full sensor wiring/testing for later.',
100:'A Jetson camera cart connects product recognition to dietary/budget help; recognition and complete checkout reliability are uncertain.',
101:'A Pi camera and microphone capture course evidence for a grounded chatbot; practical end-to-end data use with modest edge compute.',
102:'Smart glasses and a rep-sensing clip connect habits to feedback; the broad longevity score is less validated than the measured reps.',
103:'A physical triage rover combines patient search, lidar steering and eye monitoring; medical and generalized navigation claims remain prototype-level.',
104:'Rescue drone coordination overlaps the mission, but the public story offers less compact measurement evidence than VIGIL.',
105:'An ESP32 wearable camera and voice interface are tangible; cloud visual assistance has less local-robotics depth.',
106:'A handheld dual-radar prototype captures target positions; vital-to-target association and through-wall rescue performance remain bounded.',
107:'Seat presence and temperature produce a local alarm plus escalation workflow; no clinical or fleet deployment validation.',
108:'Quest ghost hands make physical learning visible; reported sub-five-second analysis is narrower than validated skill transfer.',
109:'Custom tendon-driven finger actuation and ESP32 multimodal streaming are impressive; approved AI-to-physical assistance remains a long-term goal.',
110:'Water sensors plus custom SDR decoding create a strong data-capture chain; the algae-risk model is less established.',
111:'Thermoelectric harvesting and persistent telemetry have a measurable physical output; reported 1.2 Wh used a staged heat/cold source.',
112:'Real GPS walks and local OLED feedback form a complete modest device; weaker technical novelty.',
113:'Real ECG drives a live 3D heart and printed report; strong measured input, while generated patient rhythms are simulation.',
}
# Every entrant receives an ordinal rank. The order after the leading band is deliberately low confidence.
global_order=[58,13,19,14,65,64,77,84,51,74,48,69,32,111,11,21,35,106,103,49,61,110,4,101,55,38,44,36,43,9,97,98,53,73,18,40,22,24,8,108,87,91,31,109,102,113,54,52,59,60,33,50,10,16,93,1,100,34,47,5,25,26,90,45,105,107,70,71,12,20,6,46,7,39,2,99,104,66,68,63,62,28,30,23,17,85,56,86,88,80,83,27,82,79,81,0,3,15,37,41,42,72,75,76,57,67,78,96,95,92,89,112,94,29]
assert set(global_order)==set(range(114))
tops={'Hugging Face':[13,58,65,64,14,32,51,69,74,48,21,35,55,16,59,19], 'NVIDIA':[58,65,19,64,51,74,48,69,61,53,21,8,97,98], 'Encompass':[13,77,58,65,19,48,51,106,111,69,21,11,110,103,101], 'Full Sail':[84,36,74,44,91,51,19,5,35,87,56,14,25,88]}
meta={
'Hugging Face':{'rank':7,'band':'3–12','count':78,'winners':3,'verdict':'Strong open-model application; outside the base-case podium.','pitch':'Make the Hub artifacts and their jobs concrete: Qwen3-VL sees, Whisper hears, Piper speaks. Demonstrate one view-grounded question on the Jetson. Explain the local/privacy value and memory scheduling, then show the reconstruction/evidence workflow. Do not count YOLO branding alone as meaningful Hugging Face integration.','risk':'Here has a model bakeoff; Permanence has custom quantization; Understory uses specialist BioCLIP. Farsight needs task-specific results, not just a model list.','fit':'Meaningful open-model use, creativity and execution.'},
'NVIDIA':{'rank':5,'band':'2–8','count':21,'winners':3,'verdict':'Best podium opportunity, but a crowded edge-robotics field.','pitch':'Point to the onboard Orin Nano, then show camera → TensorRT person detection → motor/navigation behavior and a local spoken answer. Say exactly what runs on the phone, Jetson and Mac. Show actual onboard throughput under the demonstrated workload if a receipt is available.','risk':'Permanence has an exceptionally checkable reveal; Understory and Apprentice have focused physical feedback loops; Atlas reports strong soak evidence. Mac splat training is not Jetson computation.','fit':'Jetson must be central to perception, AI or robotics.'},
'Encompass':{'rank':7,'band':'3–12','count':43,'winners':3,'verdict':'Excellent capture-to-review story; custom sensing rivals are formidable.','pitch':'Show synchronized measured RGB, LiDAR depth and pose becoming a real person observation, a face pin, and the original boxed frame. The strongest sentence is: “This pin opens the actual image that created it.” Explain measured geometry versus learned splats.','risk':'Here, VIGIL, Pulse and AquaWave can showcase unusual sensor hardware; Farsight mainly integrates commercial sensing. Its advantage is useful spatial evidence, not inventing LiDAR.','fit':'Meaningful real-world capture, creative hardware integration and useful downstream data.'},
'Full Sail':{'rank':6,'band':'3–12','count':36,'winners':1,'verdict':'Most vulnerable fit: the sponsor asks for a serious game or training simulation.','pitch':'Present a responder training goal, the drill, how pathfinding and face detection affect decisions, and the resulting debrief. Show timed drill scoring only if it actually runs. Use the walkable room and sighting evidence to explain what a trainee learns.','risk':'Swarm, KYU-BOT, HearthWatch and Hold Short make simulation/training central. A teleoperated rover alone is a weaker fit. CrowdClear has strong teaching structure but admits physical Quest testing and fuller pathfinding are future work.','fit':'A serious game/training simulation with meaningful pathfinding and/or face detection.'}}
track_ids={t['track']:{c['url'] for p in t['pages'] for c in p['cards']} for t in groups}
track_ranks={}
for t in groups:
 name=t['track'];eligible=[i for i in global_order if byid[i]['url'] in track_ids[name]]
 order=[i for i in tops[name] if i in eligible]+[i for i in eligible if i not in tops[name]]
 assert order.index(51)+1==meta[name]['rank'];assert len(order)==meta[name]['count']
 track_ranks[name]=order

overall=[58,13,19,14,65,64,77,84,51,74]
clock=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'); E=html.escape

def link(p):return f'[{p["title"]}]({p["url"]})'
def track_suffix(i,name):
 if name=='Full Sail' and i not in [84,36,74,44,91,51,56,80,94,95,88,25,82,5]:return ' Exact serious-game and pathfinding/face fit is less explicit.'
 if name=='NVIDIA' and i in [0,24,96]:return ' The opt-in alone does not establish central GPU use.'
 if name=='Hugging Face' and i in [3,15,17,29,30,37,42,72,75,76,67]:return ' Meaningful Hub use is less visible in the published story.'
 if name=='Encompass' and i in [66,91,93]:return ' Distinguish real captured measurements from simulated or proposed hardware behavior.'
 return ''

def clean_excerpt(p,n=900):
 t=p['text'];m=re.search(r'What it does',t,re.I);a=m.start() if m else max(0,t.find('Comment')+7)
 return re.sub(r'\s+',' ',t[a:a+n]).strip()
hwpatterns=[r'Jetson(?: Orin)?(?: Nano)?(?: Super)?',r'Raspberry Pi(?: [45])?',r'ESP32(?:-S3)?',r'iPhone(?: 17)?',r'Meta Quest(?: [23]S?)?',r'Quest(?: [23]S?)?',r'LiDAR',r'RPLIDAR(?: [A-Z0-9]+)?',r'Arduino(?: Uno| Nano(?: R4)?)?',r'Lepton(?: 3\.5)?',r'FLIR',r'XREAL',r'SO[-–](?:ARM)?10[1]',r'BioAmp EXG Pill',r'EEG',r'EOG',r'EMG',r'MyoWare(?: 2\.0)?',r'Polar H10',r'CC1101',r'ADALM.Pluto',r'DHT11',r'INA219',r'DS18B20',r'LD2450',r'C1001',r'ReSpeaker XVF3800',r'camera',r'microphone',r'ultrasonic',r'projector',r'servo',r'GPS',r'radar',r'thermoelectric']
def hardware(p):
 precise={'Farsight':'ELEGOO rover, LiDAR iPhone, Jetson Orin Nano, Arduino Uno, Meta Quest, Mac', 'Understory':'Raspberry Pi 5 rover, table-side Jetson Orin Nano, ESP32, RPLIDAR C1, Brio camera, mecanum chassis', 'Here':'ReSpeaker XVF3800, RTX laptop, chest-mounted iPhone, ESP32 haptic belt', 'Permanence':'Jetson Orin Nano 8 GB, LiDAR iPhone via Record3D, Arduino Uno, L298N, XREAL glasses', 'Mimisis':'SO-ARM101 with six STS3215 servos, two Elgato Facecam MK.2 webcams, local or AMD cloud GPU', 'Apprentice':'Jetson, overhead webcam, AprilTag board, SO-101 robot arm', 'VIGIL':'Custom STM32 flight board, FLIR Lepton 3.5, XIAO ESP32-S3, ExpressLRS radio, GPS', 'fpga_LangModel':'Digilent Arty A7-100T FPGA, USB-UART cable', 'Pulse':'Raspberry Pi 4 2 GB, 7-inch Hosyond touchscreen, HLK-LD2450, DFRobot C1001, USB power bank', 'Jolteon':'Four SP1848 TEGs, staged hot/cold source, NiMH battery, ESP32, INA219 and DS18B20 sensors'}
 if p['title'] in precise:return precise[p['title']]
 story=p['text'].split('Built With')[0]; terms=[]
 for pat in hwpatterns:
  m=re.search(pat,story,re.I)
  if m:terms.append(m.group(0))
 return ', '.join(terms[:14]) or 'No capture hardware identified in skim.'

def memberships(url):return [t for t in track_ids if url in track_ids[t]]
def all_memberships(url):return [t for t in all_members if url in all_members[t]]

intro='''Farsight is a credible finalist-style project, not a comfortable favorite. My base-case overall rank is **9th of 212**, with a plausible **5th–15th** band if the integrated demo is stable. The strongest sponsor opportunity is **NVIDIA: about 5th, with a 2nd–8th band**. These are subjective ordinal forecasts, not official results or calibrated probabilities.

The nearest architecture rival is **Understory**. The hardest checkable robotics rival is **Permanence**. **Here**, **Apprentice**, **Mimisis**, **Atlas**, **VIGIL**, and **HearthWatch** raise the bar on impact, focused demonstrations, custom engineering or evidence. Farsight’s best differentiator is combining live rescue telepresence with a spatial review record that links reconstructed rooms and person sightings back to real images.
'''
method='''Audited all 9 gallery pages: 24 projects on each of pages 1–8 and 20 on page 9, totaling 212 unique URLs. Page 9’s next control points to “#”, establishing the end of pagination. Read all gallery cards; retrieved and skimmed 134 plausible or same-track project pages, all HTTP 200. Every one of the 114 unique entrants in the four requested sponsor pools is included, plus 20 other plausible/general contenders. The remaining 78 received card screening only.

Track membership comes from the event’s exact sponsor filters, paginated to the end: Hugging Face 78 (4 pages), Full Sail 36 (2), NVIDIA 21 (1), Encompass 43 (2). These are opt-in counts, not proof of eligibility, judging attendance, or winners. The public project pages’ “Submitted to” sections show the event, not a complete per-prize list; Farsight’s story separately names all four tracks. I therefore use the filtered galleries for verified prize membership and do not pretend it was visible under every project’s “Submitted to”. A second pass also paginated all 19 sponsor-prize filters, so the census and dossiers include every publicly filter-visible sponsor opt-in, not only the four ranked pools. General prizes do not require a separate sponsor opt-in.

Evidence is first-party Devpost prose, embedded/linked demo availability and published metrics. I did not independently replay every video, inspect rival repositories, operate rival demos or observe judging. Numbers attributed to teams are self-reported. A video link is not proof that every claimed feature works. This is a complete gallery/target-track census and a public-evidence ranking audit, not a physical validation audit.

The live event rubric is Originality, Technical Understanding, Functionality, Design and Wow Factor. I combine those with impact and exact sponsor fit; no official numerical weights are published on that page. Integrated functioning and narrow checkable claims are preferred to long feature lists. Order beyond the leading ~15 is especially uncertain; the complete lists provide triage, not meaningful one-place precision. Entry age, likes, comments and page position are not scoring inputs. No official winners were used or inferred.
'''
farsight='''Published baseline: ELEGOO rover, iPhone ARKit RGB/depth/pose, onboard Jetson Orin Nano with YOLO11n-pose/TensorRT, navigation integration, cellular/Tailscale transport, Quest/WebXR operator control, local Qwen3-VL-2B-Instruct/Whisper/Piper, and Mac/Metal Brush reconstruction with face-linked browser review. The page describes immersive Quest splat walking, but bringing the browser’s face-evidence interaction into the immersive viewer is still a next step.

Timing must stay bounded: Farsight reports 114 seconds for a **saved 60-second, 119-photo sample**, including 112 seconds of Brush training and 62 seconds of CPU face processing in parallel. A 10-second idle allowance/local-copy stand-in gives 124 seconds, **excluding real Jetson transfer and browser loading**; the sample reuses a longer run’s LiDAR seed. It is not a demonstrated live one-minute-drive-to-ready-room result.

The brief flags rover power instability during demos as a concern. I did not independently observe that failure, its frequency or a repaired state. Treat it as a supplied operational concern, not a measured audit result. If the rover resets or cannot move during judging, the base-case 9th could fall roughly into the 15th–30th range, with sponsor podium chances materially worse. That downside band is judgment, not an empirical distribution. Other teams also report power, calibration or integration failures, notably Understory; apply the same proof standard to both sides.
'''
strengths=[('Breadth with a coherent mission','A responder drives, finds a person, asks about the view, then reviews a spatial record. The components can tell one story rather than compete for attention.'),('Measured spatial capture plus learned appearance','LiDAR/pose and original images can ground the room and sightings; the splat adds visual context. Understory’s public roadmap still puts full Gaussian-splat sites in future work.'),('Useful provenance','A face pin opening its actual boxed camera frame is more persuasive than an unexplained map dot or a generated medical note.'),('Substantial edge integration','Vision, navigation, streaming, motor commands and local voice share a constrained onboard device; explain the real partition clearly.')]
weaknesses=[('Reliability can erase breadth','Functionality explicitly asks whether the project reliably works end to end. Power instability or a long restart can outweigh several impressive subsystems.'),('No equally compact truth test','Permanence can hide a ball and reveal the answer. Farsight needs an equally obvious person → location → original-frame demonstration.'),('Reconstruction timing is narrower than a headline','The saved-sample build is useful engineering evidence, but not a live end-to-end turnaround receipt.'),('Limited field validation','No verified responder study, broad SAR localization test, repeated physical mission benchmark or clinical validation was established in this audit.'),('Full Sail fit needs a training outcome','VR presence and Nav2 alone do not make a serious training simulation. Demonstrate an actual learner task and debrief; describe unshown scoring as unverified.'),('Open AI is integrated, not uniquely trained','Several rivals contribute model evaluation, custom quantization, training or specialist pipelines; model-name breadth alone will not win Hugging Face.')]
recommendation='''Use one short, continuous mission in the pitch: show the rover and its onboard Jetson; drive to a person; show the live detection and a grounded local answer; open that sighting in the room and click through to the original image. If reconstruction is prebuilt, label it as a recorded run and state the bounded timing. Present only the portions that work in the available demo. Show a repeatable live movement/perception slice before adding optional features. This is a presentation recommendation, not a request to change control behavior or add runtime checks.

Lead NVIDIA with onboard compute, Encompass with capture-to-evidence, Hugging Face with the models’ concrete roles and observed usefulness, and Full Sail with the trainee’s goal and debrief. Do not tell every room the same six-minute feature list. No external submission, post, like, comment, code change, push or PR was made by this audit.
'''
lines=['# Farsight — Knight Hacks IX competitor and placement audit',f'\nCaptured: {clock}. Read-only scout. Rankings are estimates.\n',intro,'## Coverage and method\n',method,'Official event and rubric: https://knight-hacks-ix.devpost.com/\nGallery: https://knight-hacks-ix.devpost.com/submissions/search?utf8=%E2%9C%93\n','## All sponsor census\n', *[f'- {t["track"]}: {sum(len(p["cards"]) for p in t["pages"])} entries across {len(t["pages"])} page(s). Source: {t["pages"][0]["url"]}' for t in all_sponsors], '\n## Farsight baseline and reliability\n',farsight,link(byid[51])+'\n','## Overall top ten\n']
for rank,i in enumerate(overall,1):lines.append(f'{rank}. **{link(byid[i])}** — {reasons[i]}')
lines+=['\nHonorable threats: '+', '.join(link(byid[i]) for i in [48,69,32,111,11,35,21])+'. fpga_LangModel is a specialist technical threat (complete custom neural inference on an FPGA); Fobo, He Can See Us and the other polished software/game entries can outperform this forecast on design or presentation. Their lower position here is uncertainty, not evidence of inferior execution.\n','## Sponsor ranking cards\n']
for name in ['NVIDIA','Encompass','Hugging Face','Full Sail']:
 m=meta[name];t=next(x for x in groups if x['track']==name)
 lines += [f'### {name}: Farsight ~{m["rank"]}/{m["count"]}; plausible {m["band"]}; {m["winners"]} prize positions\n',m['verdict']+' '+m['risk']+'\n','Judge emphasis: '+m['pitch']+'\n',f'Exact membership census: {t["pages"][0]["url"]}\n','Full ordering (all opted-in entrants; lower positions have weak confidence):\n']
 for rank,i in enumerate(track_ranks[name],1):lines.append(f'{rank}. {link(byid[i])} — {reasons[i]}{track_suffix(i,name)}')
 lines.append('')
lines+=['## Closest direct competitors\n']
for i in [65,58,77,103,99,104,14,48,69,64,53,35,38]:
 p=byid[i];lines.append(f'- **{link(p)}** — {reasons[i]} Target tracks: {", ".join(memberships(p["url"])) or "None of the requested four"}.')
lines+=['\n## Strengths\n']+[f'- **{a}:** {b}' for a,b in strengths]+['\n## Weaknesses\n']+[f'- **{a}:** {b}' for a,b in weaknesses]+['\n## Recommendation\n',recommendation,'## Evidence and reproducibility\n','Commands used: `chrome-devtools-axi --help`, `CHROME_DEVTOOLS_AXI_SESSION=farsight-rivals CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1 chrome-devtools-axi pages` (failed: no DevToolsActivePort), read-only Harness rediscovery/health calls, then `chrome-devtools-axi stop`. AXI could not discover the live endpoint; the maintained attach skill permits Harness fallback. Existing Chrome was healthy and was not restarted. `browser-harness` listed tabs, created exactly one background task tab (05471FB9F9CAA8A8BE8FF114A47AD4A8), navigated only that tab and used read-only browser `fetch` plus DOM parsing. A 5-second bulk-evaluation timeout was corrected with a 60-second response budget; all 134 pages subsequently captured successfully. No form submissions were made.\n','Capture files inside the disposable worktree: `data/farsight-rivals/gallery.json` (9 pages/212 unique cards), `tracks.json` (four paginated membership censuses), `projects.json` (134 HTTP-200 page texts and tech/video links), `all-sponsors.json` (all 19 paginated sponsor censuses), `event.txt` (live prize counts and rubric). `build_report.py` contains the exact ordinal order and reasoning. Because worktree files can disappear, all 212 card summaries and all 134 retrieved project dossiers follow below; the portable HTML source is appended to the surviving report.\n','Source guide and design references: `docs/hackers-guide.md` (local track-purpose summary), `quest/src/style.css:1` (dark system font and muted blue/green palette). Current live Devpost confirms three prizes for HF/NVIDIA/Encompass and one for Full Sail; credit amounts should not be confused with the page’s generic “cash” labels. No runtime safety or behavior checks were added.\n','## All 212 gallery entries\n']
allcards=[c for p in gallery for c in p['cards']]
assert len(allcards)==212 and len({c['url'] for c in allcards})==212
for pg in gallery:
 lines.append(f'### Gallery page {pg["page"]} ({len(pg["cards"])} projects)\n')
 for c in pg['cards']:
  summary=re.sub(r'\s+\d+\s+\d+$','',c['text']);summary=summary[len(c['title']):].strip()
  track=', '.join(all_memberships(c['url'])) or 'No sponsor-prize opt-in found'
  screening='Project page retrieved' if c['url'] in byurl else 'Card screening only; tech/hardware not verified'
  lines.append(f'- **{link(c)}** — {summary} | {track}. {screening}.')
lines+=['\n## Retrieved project dossiers (134)\n','Hardware terms below are mentions in each project’s story, including prototypes and future work; they are not independent proof that every listed component was integrated. Built With values are the visible linked tags, which may omit unlinked tags. All sponsor opt-ins below are filter-verified across the 19 prize filters. The public “Submitted to” event was Knight Hacks IX.\n']
for p in projects:
 lines += [f'### {link(p)}\n',f'Card: {p["text"].split("Like")[0].strip()[:0] or next(c["text"] for c in allcards if c["url"]==p["url"])}\n',f'Tech tags: {", ".join(p["tech"]) or "None captured"}. Hardware mentions: {hardware(p)}.\n',f'Sponsor opt-ins: {", ".join(all_memberships(p["url"])) or "None"}. Submitted to: Knight Hacks IX. HTTP: {p["status"]}.\n',f'Story evidence excerpt: {clean_excerpt(p)}\n']
 vids=[v for v in p['videos'] if 'youtube.com/embed/' in v or 'vimeo.com/video/' in v]
 if vids:lines.append('Demo linked (not independently replayed in this audit): '+vids[0]+'\n')
 if p['url'] in [byid[i]['url'] for i in [58,13,19,14,65,64,77,84,51,74,48,69,106,111,35,21,11,36,44,91]]:
  for h in ['Accomplishments','Challenges',"What's next"]:
   m=re.search(re.escape(h),p['text'],re.I)
   if m:lines.append(f'{h} evidence: '+re.sub(r'\s+',' ',p['text'][m.start():m.start()+1100])+'\n')
report='\n'.join(lines)
(D/'report.md').write_text(report)

# Dark, portable report matching the subject's Quest palette, with no CDN dependency.
css='''*{box-sizing:border-box}html{color-scheme:dark;scroll-behavior:smooth}body{margin:0;background:#242729;color:#d2d2d2;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:#a6b9d9;text-underline-offset:3px;overflow-wrap:anywhere}main{max-width:1220px;margin:auto;padding:36px 24px 80px}header{padding:35px 0 24px}h1{font-size:clamp(34px,5vw,64px);line-height:1.08;max-width:950px;letter-spacing:-.035em;margin:16px 0}h2{font-size:28px;margin:44px 0 18px}h3{margin:0 0 9px;font-size:21px}p{margin:10px 0}.eyebrow,.label{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:#b3b3b3}.lede{font-size:21px;max-width:980px}.muted,small{color:#b3b3b3}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.metric,.card{min-width:0;background:#303436;border:1px solid #555;border-radius:16px;padding:22px}.metric strong{display:block;font-size:31px;line-height:1.2;color:#acd1a6}.metric span{display:block;font-size:13px}.badge{display:inline-block;border:1px solid #68786c;color:#acd1a6;border-radius:30px;padding:3px 10px;font-size:12px;margin:4px 5px 4px 0}.warning{background:#3c3530;border-left:4px solid #d6b283;padding:20px;border-radius:10px}.focus{border:1px solid #a6b9d9;background:#30383e}.rank{display:flex;gap:14px;padding:14px 0;border-bottom:1px solid #4b5053;min-width:0}.rank:last-child{border:0}.rank>div{min-width:0}.number{flex:0 0 28px;color:#acd1a6;font-weight:700}.rank p{font-size:14px;margin:4px 0}.rank.fs{background:#354039;border-radius:8px;padding:14px 10px}.nav{display:flex;gap:8px;flex-wrap:wrap;position:sticky;top:0;background:#242729f2;padding:12px 0;backdrop-filter:blur(10px);z-index:2}.nav a{border:1px solid #555;border-radius:8px;padding:5px 11px;text-decoration:none;font-size:13px}summary{cursor:pointer;color:#a6b9d9;padding:12px 0}details{margin:8px 0}details[open]>summary{color:#acd1a6}.catalog{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.catalog article{min-width:0;padding:17px;border:1px solid #4b5053;border-radius:12px;background:#2c3032}.catalog p{font-size:13px}input{width:100%;padding:14px;margin:12px 0 22px;background:#303436;border:1px solid #777;border-radius:10px;color:#eee;font:inherit}.evidence{font-size:13px;border-left:2px solid #5b5b5b;padding-left:12px}.foot{margin-top:38px;border-top:1px solid #555;padding-top:18px;font-size:13px}section{scroll-margin-top:70px}button{font:inherit}img,svg{max-width:100%}@media(max-width:850px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.catalog{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){main{padding:22px 16px 50px}.grid,.catalog{grid-template-columns:minmax(0,1fr)}.lede{font-size:18px}}'''
def hp(p):return f'<a href="{E(p["url"])}" target="_blank" rel="noopener">{E(p["title"])}</a>'
def row(rank,i):return f'<div class="rank {"fs" if i==51 else ""}"><span class="number">{rank:02d}</span><div><b>{hp(byid[i])}</b><p>{E(reasons[i])}</p></div></div>'
def paragraphs(t):return ''.join('<p>'+E(x).replace('**','')+'</p>' for x in t.strip().split('\n\n'))
out=['<!doctype html><html lang="en" data-theme="dark"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Farsight · Field of 212</title><style>'+css+'</style><body><main>', '<div class="eyebrow">Knight Hacks IX · Read-only competitive audit · 11 Oct 2026</div><header><span class="badge">212 projects · all 9 gallery pages</span><span class="badge">134 project pages</span><span class="badge">114 unique target-track entrants</span><h1>A strong contender.<br>A serious field.</h1><p class="lede">Farsight has finalist-level breadth. A stable, focused demonstration is what turns that breadth into a prize.</p><p class="muted">Public-evidence forecast, not official results. Captured '+E(clock)+'.</p></header>', '<nav class="nav"><a href="#placement">Placement</a><a href="#overall">Top 10</a><a href="#tracks">Sponsor cards</a><a href="#rivals">Direct rivals</a><a href="#pitch">Pitch</a><a href="#method">Evidence</a><a href="#catalog">All 212</a></nav>', '<section id="placement"><div class="metrics">']
for name,rank,band in [('Overall','9 / 212','5–15'),('NVIDIA','5 / 21','2–8'),('Encompass','7 / 43','3–12'),('Hugging Face','7 / 78','3–12'),('Full Sail','6 / 36','3–12')]:out.append(f'<div class="metric"><span class="label">{name}</span><strong>{rank}</strong><span>Plausible band {band}</span></div>')
out+=['</div><p class="muted">NVIDIA, HF and Encompass award three places; Full Sail awards one. Bands are subjective scenario ranges, not statistical confidence intervals.</p><div class="warning"><b>Reliability is the swing variable.</b><p>The brief flags power instability; this audit did not observe or measure it. A reset or failed movement during judging could put Farsight around 15th–30th overall. A working continuous mission can move it into the upper band.</p></div></section>', '<section id="overall"><h2>Overall: my top ten</h2><div class="card">']
for rank,i in enumerate(overall,1):out.append(row(rank,i))
out+=['</div><p class="muted">Honorable threats: OmniBelt, Waymaker, Blurr, Jolteon, BlackBox, Doorway and Glint. Specialist FPGA engineering and polished apps/games can upset this hardware-heavy ordering on design and presentation.</p></section>', '<section id="tracks"><h2>Four different judging stories</h2><div class="grid">']
for name in ['NVIDIA','Encompass','Hugging Face','Full Sail']:
 m=meta[name];order=track_ranks[name];url=next(t['pages'][0]['url'] for t in groups if t['track']==name)
 out.append(f'<article class="card"><span class="label">{m["winners"]} prize positions · {m["count"]} opt-ins</span><h3>{name}</h3><span class="badge">Farsight #{m["rank"]} · band {m["band"]}</span><p><b>{E(m["verdict"])}</b></p><p>{E(m["risk"])}</p>')
 for rank,i in enumerate(order[:max(8,m['rank'])],1):out.append(row(rank,i))
 out.append('<div class="evidence"><b>Emphasize to judges</b><p>'+E(m['pitch'])+'</p></div>')
 out.append(f'<details><summary>Full ranking · all {len(order)} entrants</summary>')
 for rank,i in enumerate(order,1):out.append(row(rank,i).replace(E(reasons[i]),E(reasons[i]+track_suffix(i,name))))
 out.append(f'</details><small><a href="{E(url)}">Verified sponsor membership census</a> · Lower ranks are weak-confidence triage.</small></article>')
out+=['</div></section><section id="rivals"><h2>The rivals to know by name</h2><div class="grid">']
for i in [65,58,77,14,64,74,48,69,103,99,106,35]:
 p=byid[i];out.append(f'<article class="card"><h3>{hp(p)}</h3><p>{E(reasons[i])}</p><small>{E(", ".join(memberships(p["url"])) or "Outside the four target tracks")}</small><details><summary>Published evidence and hardware</summary><p>{E(clean_excerpt(p,1100))}</p><p class="muted">Hardware mentioned: {E(hardware(p))}</p></details></article>')
out+=['</div></section><section id="pitch"><h2>Make the mission easy to verify</h2><div class="card focus"><p class="lede">Drive → find a person → ask about the view → open the room → click the original sighting.</p>'+paragraphs(recommendation)+'</div><div class="grid" style="margin-top:18px"><article class="card"><h3>Farsight’s advantages</h3>']
for a,b in strengths:out.append(f'<p><b>{E(a)}</b><br>{E(b)}</p>')
out+=['</article><article class="card"><h3>What the field exposes</h3>']
for a,b in weaknesses:out.append(f'<p><b>{E(a)}</b><br>{E(b)}</p>')
out+=['</article></div><div class="warning" style="margin-top:18px"><b>The “two minutes” claim has a boundary</b><p>114 s was a saved 60 s / 119-photo local sample, reusing a longer run’s LiDAR seed. 124 s adds an idle allowance and local-copy stand-in, excluding real Jetson transfer and browser loading. Face-pin interaction is browser evidence; immersive Quest parity remains future work.</p><a href="https://devpost.com/software/farsight-t4qyli">Farsight’s published limitations</a></div></section>', '<section id="method"><h2>What this audit proves</h2><div class="card">'+paragraphs(method)+'<p><a href="https://knight-hacks-ix.devpost.com/">Live event rubric and prizes</a> · <a href="https://knight-hacks-ix.devpost.com/submissions/search?utf8=%E2%9C%93">Complete gallery</a></p><p class="muted">All 19 sponsor-prize filters were paginated to verify the exact opt-ins. Public “Submitted to” sections show the event, not a full sponsor list. No rival video was independently replayed or hardware operated.</p></div></section>', '<section id="catalog"><details><summary>All 19 sponsor-prize entry counts</summary>'+''.join('<p>'+E(t['track'])+': '+str(sum(len(p['cards']) for p in t['pages']))+'</p>' for t in all_sponsors)+'</details><h2>All 212 projects, searchable</h2><p class="muted">Every gallery card is accounted for. Expand a retrieved dossier for tech, hardware mentions, all sponsor opt-ins and a source excerpt. Hardware mentions may include future work.</p><input id="search" type="search" placeholder="Search title, description, sponsor or hardware…" aria-label="Search project census"><div class="catalog">']
for c in allcards:
 p=byurl.get(c['url']);tracks=all_memberships(c['url']);summary=re.sub(r'\s+\d+\s+\d+$','',c['text'])[len(c['title']):].strip();key=' '.join([c['title'],summary,','.join(tracks),hardware(p) if p else ''])
 out.append(f'<article data-search="{E(key.lower(),quote=True)}"><b>{hp(c)}</b><p>{E(summary)}</p><small>{E(", ".join(tracks) or "No sponsor-prize opt-in found")}</small>')
 if p:
  out.append('<details><summary>Retrieved project dossier</summary><p>Tech tags: '+E(', '.join(p['tech']) or 'None captured')+'</p><p>Hardware mentioned: '+E(hardware(p))+'</p><p class="evidence">'+E(clean_excerpt(p,1100))+'</p>')
  vids=[v for v in p['videos'] if 'youtube.com/embed/' in v or 'vimeo.com/video/' in v]
  if vids:out.append('<p><a href="'+E(vids[0])+'">Linked demo (not replayed)</a></p>')
  out.append('<small>Submitted to Knight Hacks IX · HTTP 200 · filter-verified sponsor membership.</small></details>')
 else:out.append('<p class="muted">Card screening only. Tech and hardware not verified.</p>')
 out.append('</article>')
out+=['</div></section><div class="foot">Design source: Farsight Quest’s dark system-font palette (quest/src/style.css), adapted for a scannable report. All research was read-only. No external publication or Devpost mutation. The surviving Markdown report contains this portable HTML source.</div></main><script>document.getElementById("search").addEventListener("input",e=>{const q=e.target.value.toLowerCase().trim();document.querySelectorAll("[data-search]").forEach(a=>a.hidden=!a.dataset.search.includes(q));});</script></body></html>']
artifact=ROOT/'.lavish/farsight-rivals.html';artifact.write_text(''.join(out))
# Only the authorized external report is written outside this worktree.
external=Path('/Users/aziz.u/Developer/firstmate/data/farsight-rivals/report.md');external.parent.mkdir(parents=True,exist_ok=True)
final_report=report+'\n## Portable Lavish HTML source\n\nThe review artifact is `.lavish/farsight-rivals.html`. This self-contained source survives worktree teardown and can be saved as an HTML file.\n\n```html\n'+artifact.read_text()+'\n```\n'
external.write_text(final_report);(D/'report.md').write_text(final_report)
print(json.dumps({'artifact':str(artifact),'report':str(external),'pages':len(gallery),'cards':len(allcards),'dossiers':len(projects),'html_bytes':artifact.stat().st_size,'report_bytes':external.stat().st_size},indent=2))
