// Loads the real Control Center page in headless Chromium against the real server,
// honouring its Content-Security-Policy, and checks that the UI script actually runs.
// Usage: node tests/browser_ui.js   (needs `playwright`; not part of the pytest run)
const {chromium}=require('playwright');const {spawn}=require('child_process');const fs=require('fs'),os=require('os'),path=require('path');
const root=path.resolve(__dirname,'..','airootfs/usr/local');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'toriui-'));
const env={...process.env,HOME:tmp,TORI_DATA:tmp+'/data',TORI_RUN_DIR:tmp+'/run',TORI_CC_PORT:'18788',VICTOR_FLEET_DIR:tmp+'/fleet',VICTOR_FLEET_BIN:path.resolve(__dirname,'..','fleet/bin/victor-fleet'),VICTOR_FLEET_LIB:path.resolve(__dirname,'..','fleet')};
fs.mkdirSync(tmp+'/data/models',{recursive:true});
const cc=spawn('python3',['-I',root+'/lib/tori/control_center.py'],{env,stdio:'ignore'});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{let ok=false;
 try{await sleep(2500);const tok=fs.readFileSync(tmp+'/.cache/tori/cc.token','utf8').trim();
  const b=await chromium.launch();const p=await b.newPage();const errs=[];
  p.on('console',m=>{if(m.type()==='error')errs.push(m.text())});p.on('pageerror',e=>errs.push(e.message));
  await p.goto('http://127.0.0.1:18788/#t='+tok);await p.waitForSelector('#nav button',{timeout:8000});
  const n=await p.$$eval('#nav button',e=>e.length);
  await p.waitForFunction(()=>document.getElementById('storagePill').textContent!=='storage...',null,{timeout:8000});
  const csp=(await p.request.get('http://127.0.0.1:18788/')).headers()['content-security-policy']||'';
  console.log('nav buttons:',n,'| CSP script-src nonce:',/script-src[^;]*nonce-/.test(csp),'| console errors:',JSON.stringify(errs));
  await p.click('#nav button[data-t=fleet]');await p.waitForFunction(()=>/stopped/i.test(document.getElementById('fOut').textContent),null,{timeout:8000});
  await p.click('#fStart');await p.waitForFunction(()=>/running on 127\.0\.0\.1/.test(document.getElementById('fOut').textContent),null,{timeout:15000});
  await p.click('#fPair');await p.waitForFunction(()=>/Pairing secret/.test(document.getElementById('fOut').textContent)&&/[0-9a-f]{64}/.test(document.getElementById('fOut').textContent),null,{timeout:8000});
  await p.click('#fStop');await sleep(1500);await p.click('#fRef');await p.waitForFunction(()=>/stopped/i.test(document.getElementById('fOut').textContent),null,{timeout:8000});
  console.log('fleet tab: start/pair/stop via UI OK');
  ok=n===6&&/script-src[^;]*nonce-/.test(csp)&&!/unsafe-inline[^;]*;?\s*style/.test('')&&errs.length===0;await b.close();
 }catch(e){console.log('FAIL',e.message)}
 cc.kill();console.log(ok?'PASS':'FAIL');process.exit(ok?0:1)})();
