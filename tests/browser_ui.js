// Real Control Center + Chromium: CSP, unsaved forms, dark appearance and responsive tabs.
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
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
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
