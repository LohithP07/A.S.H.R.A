// Local-only PDF renderer for the A.S.H.R.A PRD.
// Uses Chrome DevTools Protocol so we control page size, margins and the footer.
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const root = __dirname;
const htmlPath = path.join(root, 'ashra-prd.html');
const outPath = process.argv[2] || path.join(root, 'ashra-prd.pdf');

if (!fs.existsSync(htmlPath)) {
  console.error('Missing HTML:', htmlPath);
  process.exit(1);
}

const PORT = 9400 + Math.floor(Math.random() * 200);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ashra-print-'));
const fileUrl = 'file:///' + htmlPath.replace(/\\/g, '/');

const chrome = spawn(CHROME, [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-extensions',
  '--hide-scrollbars',
  '--force-color-profile=srgb',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  'about:blank',
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function browserWs() {
  for (let i = 0; i < 120; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      const info = await res.json();
      if (info.webSocketDebuggerUrl) return info.webSocketDebuggerUrl;
    } catch (_) { /* not up yet */ }
    await sleep(150);
  }
  throw new Error('Chrome did not expose a debugging endpoint');
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id) {
        const slot = this.pending.get(msg.id);
        if (!slot) return;
        this.pending.delete(msg.id);
        if (msg.error) slot.reject(new Error(msg.error.message));
        else slot.resolve(msg.result);
      } else {
        const fn = this.listeners.get(msg.method);
        if (fn) fn(msg.params, msg.sessionId);
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.seq;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
    });
  }
  on(method, fn) { this.listeners.set(method, fn); }
}

(async () => {
  try {
    const wsUrl = await browserWs();
    const ws = new WebSocket(wsUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', reject, { once: true });
    });

    const cdp = new CDP(ws);
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });

    await cdp.send('Page.enable', {}, sessionId);
    await cdp.send('Emulation.setEmulatedMedia', { media: 'print' }, sessionId);

    const loaded = new Promise((resolve) => cdp.on('Page.loadEventFired', () => resolve()));
    await cdp.send('Page.navigate', { url: fileUrl }, sessionId);
    await loaded;

    await cdp.send('Runtime.evaluate', {
      expression: 'document.fonts.ready.then(() => true)',
      awaitPromise: true,
    }, sessionId);
    await sleep(400);

    const footer = `
      <div style="width:100%; padding:0 18mm; font-family:'Segoe UI',Arial,sans-serif;
                  font-size:7.5px; color:#94a3b8; display:flex; justify-content:space-between;">
        <span>A.S.H.R.A &nbsp;·&nbsp; Product Requirements &nbsp;·&nbsp; v1.0 Draft</span>
        <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>
      </div>`;

    const { data } = await cdp.send('Page.printToPDF', {
      printBackground: true,
      preferCSSPageSize: true,
      displayHeaderFooter: true,
      headerTemplate: '<div></div>',
      footerTemplate: footer,
      marginTop: 0.71,
      marginBottom: 0.63,
      marginLeft: 0.71,
      marginRight: 0.71,
      scale: 1,
    }, sessionId);

    fs.writeFileSync(outPath, Buffer.from(data, 'base64'));
    const kb = (fs.statSync(outPath).size / 1024).toFixed(1);
    console.log(`Wrote ${outPath} (${kb} KB)`);

    try { await cdp.send('Browser.close'); } catch (_) { /* closing */ }
    ws.close();
    chrome.kill();
  } catch (err) {
    console.error('Render failed:', err.message);
    try { chrome.kill(); } catch (_) { /* ignore */ }
    process.exitCode = 1;
  } finally {
    setTimeout(() => {
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch (_) { /* ignore */ }
    }, 500);
  }
})();
