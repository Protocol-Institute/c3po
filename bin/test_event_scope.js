// Offline test of the worker's eventScope() against a fake registry (a 2025/2026/2027 symposium, a workshop,
// a writing month), including how a bare "the symposium" moves to next year's event. No network, no cost.
// Run: node bin/test_event_scope.js   (complements bin/probe_event_scope.py, which hits the live worker)
const fs=require('fs'),vm=require('vm');
let src=fs.readFileSync(require('path').join(__dirname,'..','api','worker.js'),'utf8').replace(/^export default \{/m,'globalThis.__w = {');
const ctx=vm.createContext({console,URL,Date,Math,fetch:()=>{},crypto:{}});
vm.runInContext(src+'\nglobalThis.__scope=eventScope;',ctx);
const ev=(id,type,title,start,end,extra={})=>({id,kind:'history',type,title,start,end,all_day:true,significance:'major',aliases:[title.toLowerCase(), id.replace(/-/g,' ')],archive_namespace:'',exclusive:false,...extra});
const reg={events:[
 ev('protocol-symposium-2025','symposium','Protocol Symposium 2025','2025-09-12','2025-09-19'),
 ev('protocol-symposium-2026','symposium','Protocol Symposium 2026','2026-09-21','2026-09-25',{archive_namespace:'symposium',exclusive:true}),
 ev('protocol-symposium-2027','symposium','Protocol Symposium 2027','2027-09-20','2027-09-24',{archive_namespace:'symposium',exclusive:true}),
 ev('edge-lanna-workshop-2024','workshop','Edge Lanna Workshop 2024','2024-10-10','2024-11-10'),
 ev('book-writing-month-2026','writing-month','Book Writing Month 2026','2026-11-01','2026-11-30'),
]};
const T=(q,now,expectId)=>{const r=vm.runInContext('__scope',ctx)(q,reg,new Date(now));const got=r.scoped?r.eventId:null;
  console.log((got===expectId?'ok  ':'FAIL'),now.slice(0,10),JSON.stringify(q),'->',got,(r.crossCorpus?'[cross]':'')+(r.workshops?'[workshops]':''), got===expectId?'':'(expected '+expectId+')')};
// as of now (Oct 2026): only 2026 and 2027 are exclusive candidates
T('What workshops are happening at the Protocol Symposium?','2026-10-09T12:00:00Z','protocol-symposium-2026');
T('Who is speaking at the symposium about robots?','2026-10-09T12:00:00Z','protocol-symposium-2026');
T('summarize the 2025 symposium','2026-10-09T12:00:00Z',null);
T('What happened at the Protocol Symposium 2025?','2026-10-09T12:00:00Z',null);
T('Which sessions run on September 23?','2026-10-09T12:00:00Z','protocol-symposium-2026');
T('Which sessions run on 2026-09-23?','2026-10-09T12:00:00Z','protocol-symposium-2026');
T('What talks are on 9/24?','2026-10-09T12:00:00Z','protocol-symposium-2026');
T('What did the MRG say about memory?','2026-10-09T12:00:00Z',null);
T('What workshops did Edge Lanna run?','2026-10-09T12:00:00Z',null);
T('What is a protocol stack?','2026-10-09T12:00:00Z',null);
T("What's on today?",'2026-09-23T16:00:00Z','protocol-symposium-2026');
T("What's on today?",'2026-10-10T16:00:00Z',null);
T('How does the symposium relate to earlier SIG discussions?','2026-10-09T12:00:00Z','protocol-symposium-2026');
// next year: the 2027 symposium is upcoming; a bare "symposium" should move to it
T('Who is speaking at the symposium?','2027-08-15T12:00:00Z','protocol-symposium-2027');
T('Who is speaking at the symposium?','2027-06-01T12:00:00Z','protocol-symposium-2026');   // 2027 not within 60 days yet; 2026 is the latest past one
T('Who was at the 2026 symposium?','2027-08-15T12:00:00Z','protocol-symposium-2026');
T('Which sessions run on September 22?','2027-09-22T10:00:00Z','protocol-symposium-2027');
T("What's on today?",'2027-09-22T16:00:00Z','protocol-symposium-2027');

// ── timeRange(): explicit phrases -> ts_unix range (Phase E) ──────────────────
const TR = vm.runInContext('timeRange', ctx);
const R = (q, now, from, to) => {
  const r = TR(q, new Date(now));
  const got = r ? `${r.from}..${r.to}` : null, want = from ? `${from}..${to}` : null;
  console.log((got === want ? 'ok  ' : 'FAIL'), now.slice(0, 10), JSON.stringify(q), '->', got, r ? `(${r.label})` : '', got === want ? '' : `(expected ${want})`);
};
const NOW = '2026-10-09T12:00:00Z';
R('What has SIGPSY discussed since June?', NOW, '2026-06-01', '2026-10-09');
R('What has happened since December?', NOW, '2025-12-01', '2026-10-09');
R('What has MRG covered since the summer?', NOW, '2026-06-01', '2026-10-09');
R('Anything new since 2025?', NOW, '2025-01-01', '2026-10-09');
R('What did the Institute publish in 2025?', NOW, '2025-01-01', '2025-12-31');
R('What was discussed in March?', NOW, '2026-03-01', '2026-03-31');
R('What was discussed in November?', NOW, '2025-11-01', '2025-11-30');
R('What happened in May 2026?', NOW, '2026-05-01', '2026-05-31');
R('What happened in the community last month?', NOW, '2026-09-01', '2026-09-30');
R('What has been posted this month?', NOW, '2026-10-01', '2026-10-09');
R('What did people discuss in the past 3 weeks?', NOW, '2026-09-18', '2026-10-09');
R('Any papers from the past year?', NOW, '2025-10-09', '2026-10-09');
R('What was published last year?', NOW, '2025-01-01', '2025-12-31');
R('What did the SIGs discuss last week?', NOW, '2026-09-28', '2026-10-04');
R('What came up yesterday?', NOW, '2026-10-08', '2026-10-08');
R('Was anything written about this before 2024?', NOW, '1970-01-01', '2023-12-31');
// must NOT be ranges
R('What is a protocol stack?', NOW, null);
R('What has SIGPSY been discussing lately?', NOW, null);
R('Summarize the 2024 symposium', NOW, null);
R('What may happen to protocols in a crisis?', NOW, null);
R('Which sessions run on September 23?', NOW, null);
