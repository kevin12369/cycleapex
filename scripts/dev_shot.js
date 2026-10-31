// 用 CDP 直接驱动本地 chromium：真实等待渲染完成后截图，并收集控制台错误。
//
// 为什么需要它：本机没有 playwright，而 `chrome --screenshot --virtual-time-budget=N`
// 不等第二个异步 fetch 完成，截图永远停在「K 线数据加载中」那一帧，
// 无法验证 series.json 懒加载这类链路。CDP 可以轮询到页面真的渲染完再截。
//
// 用法（chrome 与脚本必须在同一次 shell 里启动，否则 chrome 会随上一条命令结束被回收）：
//   (cd /d/Coder/CycleApex)
//   CHROME="$HOME/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"
//   "$CHROME" --headless --no-sandbox --disable-gpu \
//     --remote-debugging-port=9333 --window-size=1400,1800 about:blank &
//   sleep 5
//   node scripts/dev_shot.js "http://127.0.0.1:8765/" out.png 4000
//
// 第三个参数是「页面渲染完成后再等多久（ms）」，留给懒加载与图表动画。
// 本地预览必须先起 HTTP 服务（file:// 下 fetch 会被浏览器拦掉）。
const http = require("http");

function getJSON(url) {
  return new Promise((res, rej) => {
    http.get(url, (r) => {
      let b = "";
      r.on("data", (c) => (b += c));
      r.on("end", () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
    }).on("error", rej);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const target = process.argv[2] || "http://127.0.0.1:8765/";
  const out = process.argv[3] || "_cdp.png";
  const waitMs = +(process.argv[4] || 5000);

  let tabs;
  for (let i = 0; i < 40; i++) {
    try { tabs = await getJSON("http://127.0.0.1:9333/json/list"); if (tabs.length) break; }
    catch (e) { /* 端口还没起来 */ }
    await sleep(250);
  }
  if (!tabs || !tabs.length) { console.error("CDP 端口未就绪"); process.exit(1); }
  const page = tabs.find((t) => t.type === "page") || tabs[0];

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const logs = [];

  const send = (method, params = {}) => new Promise((res) => {
    const msgId = ++id;
    pending.set(msgId, res);
    ws.send(JSON.stringify({ id: msgId, method, params }));
  });

  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); return; }
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      logs.push("console.error: " + m.params.args.map((a) => a.value || a.description).join(" "));
    }
    if (m.method === "Runtime.exceptionThrown") {
      logs.push("exception: " + (m.params.exceptionDetails.exception?.description
        || m.params.exceptionDetails.text));
    }
    if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
      logs.push("log: " + m.params.entry.text);
    }
  };

  let loaded = false;
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === "Page.loadEventFired") loaded = true;
  });

  await new Promise((r) => (ws.onopen = r));
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");
  await send("Page.navigate", { url: target });

  // 等 load 事件，别靠固定 sleep 猜
  for (let i = 0; i < 100 && !loaded; i++) await sleep(100);
  console.log("load 事件:", loaded);
  await sleep(500);

  // 轮询直到页面真的渲染出东西，而不是靠固定 sleep 猜
  const ready = `(function(){
    const g = document.getElementById("gauges");
    return !!(g && g.children.length) || document.body.innerText.length > 200;
  })()`;
  let ok = false;
  for (let i = 0; i < 60; i++) {
    const r = await send("Runtime.evaluate", { expression: ready, returnByValue: true });
    if (r && r.result && r.result.value) { ok = true; break; }
    await sleep(300);
  }
  console.log("页面渲染完成:", ok);
  // 再给懒加载的 series 一点时间
  await sleep(waitMs);

  // 取一份运行时状态，确认 K 线容器里到底是图还是占位符
  const stateExpr = `(function(){
      const k = document.getElementById("klineChart");
      const canvas = k ? k.querySelector("canvas") : null;
      return JSON.stringify({
        url: location.href,
        klineCanvas: !!canvas,
        klineCanvasSize: canvas ? (canvas.width + "x" + canvas.height) : "",
        klineText: (k && !canvas) ? (k.innerText || "").slice(0,60) : "",
        indicatorCanvas: !!(document.getElementById("indicatorChart")||{querySelector:null})
          && !!document.getElementById("indicatorChart").querySelector("canvas"),
        seriesOptions: (document.getElementById("seriesSelect")||{}).options
          ? document.getElementById("seriesSelect").options.length : 0,
        gauges: document.querySelectorAll("#gauges > *").length,
        heatmapCanvas: !!(document.getElementById("heatmapChart")||{querySelector:null})
          && !!document.getElementById("heatmapChart").querySelector("canvas")
      });
    })()`;
  let state = null;
  for (let i = 0; i < 20; i++) {
    const r = await send("Runtime.evaluate", { expression: stateExpr, returnByValue: true });
    if (r && r.result) { state = r; break; }
    await sleep(300);
  }
  console.log("STATE:", state ? state.result.value : "(evaluate 一直失败)");

  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  if (shot && shot.data) require("fs").writeFileSync(out, Buffer.from(shot.data, "base64"));
  else console.log("截图失败：captureScreenshot 无返回");

  if (logs.length) { console.log("ERRORS:"); logs.forEach((l) => console.log("  " + l)); }
  else console.log("ERRORS: 无");
  ws.close();
  process.exit(0);
})();
