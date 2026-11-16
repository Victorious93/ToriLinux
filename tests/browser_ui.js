// Real Control Center + Chromium: CSP, unsaved forms, brand palette, status dashboard, keyboard access and responsive tabs.
// VICTOR_SHOTS_DIR=dir saves per-tab screenshots at 1366 and 390 px.
// npm install --no-save playwright; npx playwright install chromium; node tests/browser_ui.js
// PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH selects an already installed Chromium.
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const root = path.resolve(__dirname, '..', 'airootfs/usr/local');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'victorui-'));
const env = {...process.env, HOME: tmp, XDG_CACHE_HOME: tmp + '/cache',
  VICTOR_DATA: tmp + '/data', VICTOR_RUN_DIR: tmp + '/run', VICTOR_CC_PORT: '18788',
   VICTOR_FLEET_DIR: tmp + '/fleet', VICTOR_FLEET_BIN: path.resolve(__dirname, '..', 'fleet/bin/victor-fleet'), VICTOR_FLEET_LIB: path.resolve(__dirname, '..', 'fleet')};
fs.mkdirSync(tmp + '/data/models', {recursive: true});
const cc = spawn('python3', ['-I', root + '/lib/victorlinux/control_center.py'], {env});
let output = '';
cc.stdout.on('data', b => { output += b; });
cc.stderr.on('data', b => { output += b; });
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
 let browser;
 try {
  const tokenFile = tmp + '/cache/victorlinux/cc.token';
  for (let i = 0; i < 100 && !fs.existsSync(tokenFile) && cc.exitCode === null; i++) await sleep(100);
  assert.ok(fs.existsSync(tokenFile), 'Server startup failed: ' + output);
  const token = fs.readFileSync(tokenFile, 'utf8').trim();
  browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined, args: JSON.parse(process.env.PLAYWRIGHT_CHROMIUM_ARGS || "[]")});
  const page = await browser.newPage({colorScheme: 'light', viewport: {width: 1366, height: 900}});
  const errors = [], foreign = [];
  page.on('pageerror', e => errors.push(e.message));
  // No external fonts, CDNs or network calls: every request stays on the local Control Center origin.
  page.on('request', r => { if (!/^(http:\/\/127\.0\.0\.1:18788\/|data:|blob:)/.test(r.url())) foreign.push(r.url()); });
  // The source checkout has no root helper installed: fixture this one read-only response.
  await page.route('**/api/storage', route => route.fulfill({json: {
   state: {mode: 'missing', message: 'No persistent storage selected.'}, partitions: [],
  }}));
  await page.goto('http://127.0.0.1:18788/#t=' + token);
  await page.waitForFunction(() => !['storage…', 'storage...'].includes(document.querySelector('#storagePill').textContent));
  assert.equal(await page.locator('#nav button').count(), 6);
  const csp = (await page.request.get('http://127.0.0.1:18788/')).headers()['content-security-policy'];
  assert.match(csp, /script-src[^;]*nonce-/);
  assert.doesNotMatch(csp, /script-src[^;]*unsafe-inline/);
  console.log('PASS real server, authenticated status, CSP nonce');
  // Fleet tab: start / pair / stop the real coordinator through the UI
  await page.locator('[data-t="fleet"]').click();
  await page.waitForFunction(() => /stopped/i.test(document.getElementById('fOut').textContent));
  await page.click('#fStart');
  await page.waitForFunction(() => /running on 127\.0\.0\.1/.test(document.getElementById('fOut').textContent), null, {timeout: 15000});
  await page.click('#fPair');
  await page.waitForFunction(() => /Pairing secret/.test(document.getElementById('fOut').textContent) && /[0-9a-f]{64}/.test(document.getElementById('fOut').textContent));
  await page.click('#fStop'); await sleep(1500); await page.click('#fRef');
  await page.waitForFunction(() => /stopped/i.test(document.getElementById('fOut').textContent));
  console.log('PASS fleet tab start/pair/stop');
  await page.locator('[data-t="remote"]').click();
  await page.locator('#rEn').check(); await page.locator('#rTls').check();
  await page.locator('#rBind').fill('100.64.0.9'); await page.locator('#rPort').fill('9090');
  await page.locator('#tlsHost').focus(); await page.evaluate(() => refresh());
  assert.equal(await page.locator('#rBind').inputValue(), '100.64.0.9');
  assert.equal(await page.locator('#rPort').inputValue(), '9090');
  assert.equal(await page.locator('#rEn').isChecked(), true);
  assert.equal(await page.locator('#rTls').isChecked(), true);
  console.log('PASS polling preserves unsaved remote settings');
  await page.locator('[data-t="models"]').click();
  assert.equal(await page.locator('#sCtxAuto').isChecked(), true, 'new installs default to automatic context');
  assert.equal(await page.locator('#sCtx').isDisabled(), true);
  await page.locator('#sCtxAuto').uncheck(); assert.equal(await page.locator('#sCtx').isDisabled(), false);
  await page.locator('#sCtx').fill('8192');
  await page.evaluate(() => refresh()); assert.equal(await page.locator('#sCtx').inputValue(), '8192');
  await page.locator('#bSave').click();
  await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('Settings saved'));
  await page.reload(); await page.waitForFunction(() => document.querySelector('#sCtx').value === '8192');
  assert.equal(await page.locator('#sCtxAuto').isChecked(), false, 'manual context choice persists');
  console.log('PASS runtime settings save and reload');
  await page.locator('[data-t="dash"]').click();
  await page.locator('#bAdvice').click();
  await page.waitForFunction(() => document.querySelector('#advice').textContent.includes('Memory pool:'));
  assert.equal(await page.locator('#advice').textContent().then(t => t.includes('Optional catalog (not downloaded)')), true);
  console.log('PASS host memory pool and ranked-model advice render');
  // Storage tab with several drives at once: internal SSD, external 2 TB exFAT drive, FAT32 stick, locked and duplicate volumes
  await page.unroute('**/api/storage');
  const part = (o) => Object.assign({uuid: 'U', label: '', fstype: 'ext4', size: 1e12, free: 5e11, kind: 'internal-ssd', removable: false, model: '', serial: '',
   mounted: true, mountpoint: '/mnt/victor-vol/x', mounted_ro: false, device_ro: false, locked: false, supported: true, persist_ok: true, ro_ok: true, duplicate_uuid: false}, o);
  const dest = (role, o) => ({role, subpath: '', ref: {uuid: o.uuid}, status: 'active', message: '', path: o.path, writable: true, fstype: o.fstype, free: o.free, persistent: true});
  await page.route('**/api/storage', route => route.fulfill({json: {
   state: {mode: 'persistent', message: 'Persistent storage active on /dev/sdc3 (VICTORDATA).'},
   partitions: [
    part({path: '/dev/sdc3', uuid: 'VD-1', label: 'VICTORDATA', size: 6e10, kind: 'usb-storage', removable: true, model: 'Cruzer', serial: 'SD1', mountpoint: '/mnt/victordata'}),
    part({path: '/dev/nvme0n1p2', uuid: 'SSD-1', label: 'Internal SSD', kind: 'internal-nvme', model: 'Samsung SSD 990', persist_ok: true}),
    part({path: '/dev/sdb1', uuid: 'EXF-1', label: 'Seagate 2TB', fstype: 'exfat', size: 2e12, free: 1.4e12, kind: 'usb-hdd', removable: true, model: 'Expansion', serial: 'NA9', persist_ok: false}),
    part({path: '/dev/sdd1', uuid: 'FAT-1', label: 'CARD', fstype: 'vfat', size: 6.4e10, kind: 'sd-card', persist_ok: false}),
    part({path: '/dev/sde1', uuid: 'LUKS-1', fstype: 'crypto_LUKS', locked: true, supported: false, persist_ok: false, ro_ok: false, mounted: false, mountpoint: null}),
    part({path: '/dev/sdf1', uuid: 'DUP-1', label: 'clone', duplicate_uuid: true, persist_ok: true}),
   ],
   summary: {state: {mode: 'persistent'}, destinations: {
    models: [dest('models', {uuid: 'EXF-1', path: '/mnt/victor-vol/EXF-1/LLM library', fstype: 'exfat', free: 1.4e12})],
    documents: [dest('documents', {uuid: 'SSD-1', path: '/mnt/victor-vol/SSD-1/Documents', fstype: 'ext4', free: 5e11})],
    backups: [Object.assign(dest('backups', {uuid: 'GONE', path: null, fstype: 'ntfs', free: null}), {status: 'missing', message: 'The drive with filesystem GONE (Backup HDD) is not connected.'})]}}}}));
  await page.locator('[data-t="storage"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#stTable tr').length === 7);
  const dsText = await page.locator('#dsTable').innerText();
  assert.match(dsText, /models[\s\S]*active · persistent[\s\S]*LLM library/);
  assert.match(dsText, /backups[\s\S]*missing[\s\S]*not connected/);
  const drivesText = await page.locator('#stTable').innerText();
  assert.match(drivesText, /Seagate 2TB[\s\S]*exfat/); assert.match(drivesText, /files < 4 GiB/); assert.match(drivesText, /locked \(encrypted\)/); assert.match(drivesText, /duplicate UUID/);
  assert.equal(await page.locator('#stTable button[data-use]').count(), 2);          // exFAT/FAT32/locked/duplicate cannot hold app state
  assert.equal(await page.locator('#dsDrive option').count(), 5);                     // locked volume is not offered as a destination
  assert.match(await page.locator('#stState').innerText(), /Persistent/);
  console.log('PASS storage tab: multiple drives, destinations, missing drive, FAT32/locked/duplicate flags');
  // Brand palette: #0B0F1A background, #00E5FF primary accent, #FF2BD6 secondary accent; no light surfaces anywhere.
  assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), 'rgb(11, 15, 26)');
  assert.equal(await page.locator('#bDs').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(0, 229, 255)');
  assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--mag').trim().toLowerCase()), '#ff2bd6');
  for (const tab of ['dash', 'models', 'storage', 'remote', 'fleet', 'logs']) {
   await page.locator(`[data-t="${tab}"]`).click();
   const light = await page.evaluate(() => [...document.querySelectorAll('body *')].filter(el => el.offsetParent !== null || el.tagName === 'HEADER').map(el => {
     const m = getComputedStyle(el).backgroundColor.match(/[\d.]+/g) || []; const [r, g, b, a = 1] = m.map(Number);
     return {el: el.tagName + '#' + el.id + '.' + el.className, r, g, b, a}; }).filter(c => c.a > 0.3 && c.r > 200 && c.g > 200 && c.b > 200).map(c => c.el));
   assert.deepEqual(light, [], `light background on ${tab}`);
  }
  console.log('PASS brand palette (#0B0F1A / #00E5FF / #FF2BD6), no light backgrounds on any tab');
  // Status dashboard: six live tiles (model, backend, API + bind, storage + drives, RAM, VRAM), no loading placeholders left.
  await page.locator('[data-t="dash"]').click();
  await page.evaluate(() => refresh());
  await page.waitForFunction(() => !document.querySelector('#tiles .skel'));
  assert.equal(await page.locator('#tiles .tile').count(), 6);
  for (const id of ['k-model', 'k-backend', 'k-api', 'k-storage', 'k-ram', 'k-vram'])
   assert.ok((await page.locator(`#${id} .v`).innerText()).trim().length > 0, id + ' empty');
  assert.match(await page.locator('#k-api .sub').innerText(), /127\.0\.0\.1:\d+ · local only/);
  assert.match(await page.locator('#k-storage .sub').innerText(), /6 drives · 3 destinations/);
  assert.match(await page.locator('#k-backend .v').innerText(), /^(CPU|Vulkan|CUDA|HIP|None)$/);
  assert.match(await page.locator('#upd').innerText(), /updated/);
  console.log('PASS status dashboard tiles: model, backend, API bind, storage drives, RAM, VRAM');
  // Keyboard: roving tabindex, arrows/Home/End switch tabs, visible cyan focus ring.
  await page.locator('#tab-dash').focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'tab-models');
  assert.equal(await page.locator('#t-models').isVisible(), true);
  await page.keyboard.press('End'); assert.equal(await page.locator('#t-logs').isVisible(), true);
  await page.keyboard.press('Home'); assert.equal(await page.locator('#t-dash').isVisible(), true);
  assert.equal(await page.locator('#nav button[tabindex="0"]').count(), 1);
  assert.equal(await page.evaluate(() => getComputedStyle(document.activeElement).outlineColor), 'rgb(0, 229, 255)');
  console.log('PASS keyboard tab navigation and focus ring');
  // Error state: a failed status request shows a banner with Retry; Retry recovers.
  await page.route('**/api/status', route => route.abort());
  await page.evaluate(() => refresh());
  await page.waitForSelector('#connErr');
  assert.equal(await page.locator('#upd').innerText(), 'offline');
  await page.unroute('**/api/status');
  await page.click('#bRetry');
  await page.waitForFunction(() => !document.querySelector('#connErr') && /updated/.test(document.querySelector('#upd').textContent));
  console.log('PASS connection error banner and retry');
  if (process.env.VICTOR_SHOTS_DIR) {
   fs.mkdirSync(process.env.VICTOR_SHOTS_DIR, {recursive: true});
   for (const [width, height] of [[1366, 900], [390, 844]]) {
    await page.setViewportSize({width, height});
    for (const tab of ['dash', 'models', 'storage', 'remote', 'fleet', 'logs']) {
     await page.locator(`[data-t="${tab}"]`).click(); await sleep(250);
     await page.screenshot({path: path.join(process.env.VICTOR_SHOTS_DIR, `control-center-${tab}-${width}.png`), fullPage: true});
    }
   }
  }
  if (process.env.VICTOR_STORAGE_SCREENSHOT) {
   await page.setViewportSize({width: 1366, height: 1100});
   await page.screenshot({path: process.env.VICTOR_STORAGE_SCREENSHOT, fullPage: true});
  }
  for (const width of [1366, 768, 390]) {
   await page.setViewportSize({width, height: 900});
   for (const tab of ['dash', 'models', 'storage', 'remote', 'fleet', 'logs']) {
    await page.locator(`[data-t="${tab}"]`).click();
    assert.equal(await page.locator(`#t-${tab}`).isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
      `horizontal page overflow: ${width}px / ${tab}`);
   }
   assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark');
   assert.equal(await page.locator('#nav button').first().evaluate(el => parseFloat(getComputedStyle(el).borderRadius) >= 16), true);
  }
  await page.emulateMedia({colorScheme: 'dark'});
  assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark');
  await page.evaluate(() => { sessionStorage.tab = 'obsolete-tab'; });
  await page.reload(); await page.waitForSelector('#t-dash.on');
  assert.deepEqual(errors, []);
  assert.deepEqual(foreign, [], 'external network requests');
  if (process.env.VICTOR_UI_SCREENSHOT) {
   await page.setViewportSize({width: 1366, height: 900});
   await page.screenshot({path: process.env.VICTOR_UI_SCREENSHOT, fullPage: true});
  }
  console.log('PASS all tabs, rounded navigation, 3 widths, dark with either host preference, stale tab recovery, no JS errors');
 } catch (e) { console.error(e.stack); process.exitCode = 1; }
 finally {
  if (browser) await browser.close();
  cc.kill();
  await new Promise(resolve => { if (cc.exitCode !== null) resolve(); else cc.once('exit', resolve); });
  fs.rmSync(tmp, {recursive: true, force: true});
 }
})();
