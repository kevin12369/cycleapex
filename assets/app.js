/* 中美金融市场牛熊仪表盘 - 前端渲染 */
(function () {
  "use strict";

  const COLORS = { bull: "#ef4b4b", bear: "#1fb86b", neutral: "#d4a017", unknown: "#666" };
  const VERDICT_TEXT = { bull: "牛市", bear: "熊市", neutral: "震荡", unknown: "待数据" };

  let DATA = null;
  let ALL_SERIES = [];
  let state = { range: 63, seriesId: 0, compareId: "" };
  let currentWeights = {};   // 用户自定义权重（localStorage 持久化）

  const klineChart = echarts.init(document.getElementById("klineChart"));
  const indChart = echarts.init(document.getElementById("indicatorChart"));
  const heatChart = echarts.init(document.getElementById("heatmapChart"));
  window.addEventListener("resize", () => {
    klineChart.resize(); indChart.resize(); heatChart.resize();
  });

  function verdictColor(v) { return COLORS[v] || COLORS.unknown; }

  function sliceArr(arr, n) { return arr.slice(Math.max(0, arr.length - n)); }

  function collectSeries() {
    const list = [];
    const m = DATA.markets;
    ["us_stocks", "cn_stocks", "bonds", "kr_stocks", "gold", "fx"].forEach((k) => {
      const mk = m[k];
      if (mk && mk.series) {
        mk.series.forEach((s) => list.push({ market: mk.label, name: s.name, s: s }));
      }
    });
    return list;
  }

  // ---------------- 仪表盘 ----------------
  let GAUGE_INSTANCES = {};   // 缓存所有 gauge 实例，重渲染前显式 dispose，避免 canvas/series 叠加

  function gaugeOption(label, score, verdict) {
    const val = Math.round((score || 0) * 100);
    return {
      series: [{
        type: "gauge", min: -100, max: 100, startAngle: 210, endAngle: -30,
        center: ["50%", "56%"], radius: "78%",
        progress: { show: true, width: 10, itemStyle: { color: verdictColor(verdict) } },
        axisLine: { lineStyle: { width: 10, color: [
          [0.33, COLORS.bear], [0.66, COLORS.neutral], [1, COLORS.bull] ] } },
        pointer: { width: 4, length: "58%" },
        axisTick: { show: false },
        splitLine: { length: 8, lineStyle: { color: "#3a4350", width: 1 } },
        axisLabel: { distance: 8, color: "#8b949e", fontSize: 9,
          formatter: (v) => (v === -100 || v === 0 || v === 100 ? v : "") },
        anchor: { show: true, size: 8, itemStyle: { color: "#888" } },
        detail: { valueAnimation: true, formatter: val + "", color: "#e6edf3",
          fontSize: 24, offsetCenter: [0, "-40%"] },
        title: { show: false },
        data: [{ value: val, name: label }]
      }]
    };
  }

  function renderGauges() {
    const m = DATA.markets;
    const cats = [
      { key: "us_stocks", label: "美股" },
      { key: "cn_stocks", label: "A股" },
      { key: "kr_stocks", label: "韩股" },
      { key: "bonds", label: "债券" },
      { key: "gold", label: "黄金" },
      { key: "sentiment", label: "情绪" },
      { key: "volatility", label: "波动率" },
      { key: "credit", label: "信用" },
      { key: "valuation", label: "估值" },
      { key: "macro", label: "宏观" },
      { key: "volume", label: "量能" },
      { key: "breadth", label: "广度" },
      { key: "yen_carry", label: "日元/套息" },
    ];
    const host = document.getElementById("gauges");
    // 先释放旧实例，避免 innerHTML 清空后 canvas 仍残留或 ECharts 内部 series 合并导致叠加
    Object.values(GAUGE_INSTANCES).forEach((inst) => { try { inst.dispose(); } catch (e) {} });
    GAUGE_INSTANCES = {};
    host.innerHTML = "";
    cats.forEach((c) => {
      const div = document.createElement("div");
      div.className = "card";
      const title = document.createElement("h3");
      let score = null, verdict = "unknown", sub = "待配置 / 无数据";
      const mk = m[c.key];
      if (mk) {
        if (c.key === "macro") {
          const d = mk.detail || {};
          const parts = Object.values(d).map((x) => x.score);
          score = parts.length ? parts.reduce((a, b) => a + b, 0) / parts.length : null;
          verdict = score == null ? "unknown" : (score >= 0.33 ? "bull" : score <= -0.33 ? "bear" : "neutral");
          sub = mk.sub || Object.entries(d).map(([k, v]) => `${k}:${(v.score * 100).toFixed(0)}`).join("  ");
        } else {
          score = mk.score; verdict = mk.verdict;
          sub = mk.sub || ("综合分 " + (score * 100).toFixed(0));
        }
      }
      title.innerHTML = `${c.label} <span class="badge ${verdict}">${VERDICT_TEXT[verdict]}</span>`;
      const chart = document.createElement("div");
      chart.className = "gauge"; chart.id = "g_" + c.key;
      const s = document.createElement("div"); s.className = "sub"; s.textContent = sub;
      div.appendChild(title); div.appendChild(chart); div.appendChild(s);
      host.appendChild(div);
      const inst = echarts.init(chart);
      GAUGE_INSTANCES[c.key] = inst;
      inst.setOption(gaugeOption(c.label, score, verdict), true); // true = notMerge，防止旧数据叠加
      requestAnimationFrame(() => { try { inst.resize(); } catch (e) {} });
    });
  }

  // ---------------- 综合权重配置面板 ----------------
  const WEIGHT_LABELS = { us_stocks: "美股", cn_stocks: "A股", kr_stocks: "韩股", bonds: "债券",
    gold: "黄金", sentiment: "情绪", volatility: "波动率", credit: "信用", valuation: "估值",
    volume: "量能", breadth: "广度", macro: "宏观", yen_carry: "日元/套息" };

  function getDimScore(key) {
    const mk = DATA.markets[key];
    if (!mk) return null;
    if (key === "macro") {
      const d = mk.detail || {};
      const parts = Object.values(d).map((x) => x.score);
      return parts.length ? parts.reduce((a, b) => a + b, 0) / parts.length : null;
    }
    return (mk.score == null) ? null : mk.score;
  }

  function computeOverall(wmap) {
    let tw = 0, acc = 0;
    for (const [k, w] of Object.entries(wmap)) {
      const sc = getDimScore(k);
      if (sc != null && w > 0) { acc += w * sc; tw += w; }
    }
    if (tw === 0) return { score: 0, verdict: "unknown" };
    const score = acc / tw;
    return { score, verdict: score >= 0.33 ? "bull" : score <= -0.33 ? "bear" : "neutral" };
  }

  function renderOverall(wmap) {
    const ov = computeOverall(wmap);
    const ob = document.getElementById("overallBadge");
    if (ob) {
      ob.className = "badge " + ov.verdict;
      ob.textContent = VERDICT_TEXT[ov.verdict] + " (" + (ov.score * 100).toFixed(0) + ")";
    }
    return ov;
  }

  function renderWeightPanel() {
    const host = document.getElementById("weightPanel");
    if (!host) return;
    const defaults = (DATA.professional && DATA.professional.present_weights) || {};
    const dims = Object.keys(defaults);
    if (!dims.length) { host.innerHTML = '<div class="empty">无可用维度权重</div>'; return; }

    let saved = {};
    try { saved = JSON.parse(localStorage.getItem("cycleapex.weights") || "{}"); } catch (e) {}
    const wmap = {};
    dims.forEach((k) => { wmap[k] = (saved[k] != null && !isNaN(saved[k])) ? saved[k] : defaults[k]; });
    currentWeights = Object.assign({}, wmap);

    host.innerHTML = "";
    const rows = document.createElement("div"); rows.className = "weight-rows";
    dims.forEach((k) => {
      const row = document.createElement("div"); row.className = "wrow";
      const lab = document.createElement("span"); lab.className = "wlab"; lab.textContent = WEIGHT_LABELS[k] || k;
      const rng = document.createElement("input");
      rng.type = "range"; rng.min = 0; rng.max = 30; rng.step = 1; rng.value = Math.round(wmap[k] * 100);
      const val = document.createElement("span"); val.className = "wval"; val.textContent = String(Math.round(wmap[k] * 100));
      rng.addEventListener("input", () => {
        val.textContent = rng.value;
        wmap[k] = (+rng.value) / 100;
        currentWeights = Object.assign({}, wmap);
        renderOverall(currentWeights);
        renderHero();
        updateWeightNote(dims, wmap);
        clearTimeout(weightSaveTimer);
        weightSaveTimer = setTimeout(() => {
          try { localStorage.setItem("cycleapex.weights", JSON.stringify(wmap)); } catch (e) {}
        }, 400);
      });
      row.appendChild(lab); row.appendChild(rng); row.appendChild(val);
      rows.appendChild(row);
    });
    host.appendChild(rows);

    const note = document.createElement("div"); note.className = "weight-note"; note.id = "weightNote";
    host.appendChild(note);

    const reset = document.createElement("button"); reset.className = "btn"; reset.textContent = "恢复默认权重";
    reset.style.marginTop = "8px";
    reset.addEventListener("click", () => {
      try { localStorage.removeItem("cycleapex.weights"); } catch (e) {}
      renderWeightPanel();
    });
    host.appendChild(reset);

    updateWeightNote(dims, wmap);
    renderOverall(currentWeights);
  }

  // 维度口语化风险提示（Hero 卡用：把分值翻译成更直白的风险/顺风话术）
  // 全维度口语化风险提示（纯中文大白话，不再用英文风险术语）
  function verbalHint(key, s) {
    const POS = {
      us_stocks: "美股多头正酣·纳指领着冲",
      cn_stocks: "A股暖意上来·资金愿意买",
      kr_stocks: "韩股带情绪·半导体在躁动",
      bonds: "债市回暖·收益率往下走",
      gold: "金价狂飙·避险资金抱团",
      sentiment: "情绪上头·贪婪盖过恐惧",
      volatility: "波动睡着·市场岁月静好",
      credit: "信用利差收窄·钱愿意借",
      valuation: "估值还便宜·安全垫厚",
      macro: "宏观暖风·数据超预期",
      volume: "量能放大·资金在进场",
      breadth: "宽度铺开·个股普涨",
      yen_carry: "套息顺风顺水·资金敢冲",
    };
    const NEG = {
      us_stocks: "美股在掉头·小心回撤",
      cn_stocks: "A股还阴着·抛压没散",
      kr_stocks: "韩股在泄气·风险偏好退",
      bonds: "债市承压·收益率往上拱",
      gold: "金价趴窝·避险盘撤退",
      sentiment: "情绪慌了·恐惧占上风",
      volatility: "波动炸了·恐慌指数抬头",
      credit: "信用在裂·利差走阔",
      valuation: "估值偏贵·小心挤泡沫",
      macro: "宏观转冷·数据在走弱",
      volume: "量能萎缩·有人在派发",
      breadth: "宽度塌了·涨的只是权重",
      yen_carry: "套息在拆仓·赶紧系安全带",
    };
    const MID = {
      us_stocks: "美股原地打转", cn_stocks: "A股不上不下", kr_stocks: "韩股观望",
      bonds: "债市横着", gold: "金价没方向", sentiment: "情绪半对半",
      volatility: "波动不咸不淡", credit: "信用平稳", valuation: "估值不便宜也不离谱",
      macro: "宏观不温不火", volume: "量能平平", breadth: "宽度一般",
      yen_carry: "套息不温不火",
    };
    if (s >= 0.15) return POS[key] || null;
    if (s <= -0.15) return NEG[key] || null;
    return MID[key] || null;
  }

  // ---------------- Hero 结论卡（顶部 AI 综合结论） ----------------
  function renderHero() {
    const hero = document.getElementById("hero");
    if (!hero) return;
    const ov = computeOverall(currentWeights);
    const badge = document.getElementById("heroBadge");
    if (badge) {
      badge.className = "hero-badge " + ov.verdict;
      badge.textContent = VERDICT_TEXT[ov.verdict];
    }
    const sc = document.getElementById("heroScore");
    if (sc) sc.textContent = "综合分 " + Math.round(ov.score * 100) + " · " + (ov.score >= 0 ? "偏多" : "偏空");
    const st = document.getElementById("heroStructural");
    if (st && DATA.structural_signal) st.textContent = DATA.structural_signal;
    const chips = document.getElementById("heroChips");
    if (chips) {
      const m = DATA.markets;
      const order = ["us_stocks", "cn_stocks", "kr_stocks", "bonds", "gold",
        "sentiment", "volatility", "credit", "valuation", "macro", "volume", "breadth", "yen_carry"];
      const items = [];
      order.forEach((k) => {
        const mk = m[k]; if (!mk) return;
        let s = mk.score;
        if (k === "macro" && mk.detail) {
          const parts = Object.values(mk.detail).map((x) => x.score);
          s = parts.length ? parts.reduce((a, b) => a + b, 0) / parts.length : null;
        }
        if (s == null) return;
        if (Math.abs(s) >= 0.15) items.push({ label: WEIGHT_LABELS[k] || k, score: s, verbal: verbalHint(k, s) });
      });
      items.sort((a, b) => b.score - a.score);
      chips.innerHTML = items.length ? items.map((it) => {
        const v = it.score >= 0 ? "bull" : "bear";
        const num = (it.score >= 0 ? "+" : "") + Math.round(it.score * 100);
        const text = it.verbal ? (it.verbal + " " + num) : (it.label + " " + num);
        return '<span class="chip ' + v + '">' + text + "</span>";
      }).join("") : '<span class="chip neutral">各维度均处中性区间</span>';
    }
  }

  let weightSaveTimer = null;
  function updateWeightNote(dims, wmap) {
    const note = document.getElementById("weightNote");
    if (!note) return;
    const tot = dims.reduce((a, k) => a + (wmap[k] || 0), 0) || 1;
    const eff = dims.map((k) => `${(WEIGHT_LABELS[k] || k)} ${((wmap[k] || 0) / tot * 100).toFixed(0)}%`).join(" · ");
    const ov = computeOverall(wmap);
    note.innerHTML = `有效权重（已自动归一化，无需各档和=100）：<br/>${eff}<br/>` +
      `按当前权重，综合结论：<b class="badge ${ov.verdict}" style="padding:1px 8px">${VERDICT_TEXT[ov.verdict]} (${Math.round(ov.score * 100)})</b>`;
  }

  // ---------------- K线 + 均线 + 量 ----------------
  function renderKline() {
    const item = ALL_SERIES[state.seriesId];
    if (!item) return;
    const s = item.s;
    const n = state.range;
    const dates = sliceArr(s.dates, n);
    const o = sliceArr(s.open, n), c = sliceArr(s.close, n),
          h = sliceArr(s.high, n), l = sliceArr(s.low, n);
    const candle = dates.map((_, i) => [o[i], c[i], l[i], h[i]]);
    const ma20 = sliceArr(s.ma20, n), ma60 = sliceArr(s.ma60, n), ma200 = sliceArr(s.ma200, n);

    const series = [{
      name: "K线", type: "candlestick", data: candle,
      itemStyle: { color: COLORS.bull, color0: COLORS.bear,
        borderColor: COLORS.bull, borderColor0: COLORS.bear }
    }, { name: "MA20", type: "line", data: ma20, smooth: true, showSymbol: false,
         lineStyle: { width: 1, color: "#f5a623" } },
       { name: "MA60", type: "line", data: ma60, smooth: true, showSymbol: false,
         lineStyle: { width: 1, color: "#3b82f6" } },
       { name: "MA200", type: "line", data: ma200, smooth: true, showSymbol: false,
         lineStyle: { width: 1, color: "#a855f7" } },
       { name: "成交量", type: "bar", data: sliceArr(s.vol || [], n),
         xAxisIndex: 1, yAxisIndex: 1, itemStyle: { color: "#3a4350" } }];

    let yAxisExtra = [];
    if (state.compareId !== "") {
      const cmp = ALL_SERIES[state.compareId];
      if (cmp) {
        const cc = sliceArr(cmp.s.close, n);
        const base = cc[0] || 1;
        const norm = cc.map((v) => (v / base - 1) * 100);
        series.push({ name: "对比:" + cmp.name, type: "line", data: norm,
          yAxisIndex: 2, showSymbol: false, smooth: true,
          lineStyle: { width: 1.5, color: "#22d3ee", type: "dashed" } });
        yAxisExtra = [{ type: "value", name: "对比%", position: "right",
          axisLabel: { formatter: "{value}%" }, splitLine: { show: false } }];
      }
    }

    klineChart.setOption({
      backgroundColor: "transparent",
      legend: { data: ["MA20", "MA60", "MA200"].concat(state.compareId !== "" ? ["对比"] : []),
        textStyle: { color: "#8b949e" }, top: 0 },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      grid: [{ left: 50, right: 20, top: 36, height: "58%" },
             { left: 50, right: 20, top: "72%", height: "16%" }],
      xAxis: [{ type: "category", data: dates, axisLine: { lineStyle: { color: "#2a313c" } },
               axisLabel: { color: "#8b949e" } },
              { type: "category", gridIndex: 1, data: dates, axisLabel: { show: false },
               axisLine: { lineStyle: { color: "#2a313c" } } }],
      yAxis: [{ type: "value", scale: true, axisLabel: { color: "#8b949e" },
               splitLine: { lineStyle: { color: "#1c2330" } } },
              { type: "value", gridIndex: 1, axisLabel: { color: "#8b949e" },
               splitLine: { show: false } }].concat(yAxisExtra),
      dataZoom: [{ type: "inside", xAxisIndex: [0, 1] },
                 { type: "slider", xAxisIndex: [0, 1], bottom: 0, height: 14 }],
      series: series
    }, true);
  }

  // ---------------- RSI + MACD ----------------
  function renderIndicator() {
    const item = ALL_SERIES[state.seriesId];
    if (!item) return;
    const s = item.s, n = state.range;
    const dates = sliceArr(s.dates, n);
    const rsi = sliceArr(s.rsi, n);
    const hist = sliceArr(s.macdHist, n);
    const dif = sliceArr(s.macd, n), dea = sliceArr(s.macdSignal, n);
    indChart.setOption({
      backgroundColor: "transparent",
      legend: { data: ["RSI", "MACD", "DIF", "DEA"], textStyle: { color: "#8b949e" }, top: 0 },
      tooltip: { trigger: "axis" },
      grid: { left: 44, right: 44, top: 30, bottom: 28 },
      xAxis: { type: "category", data: dates, axisLabel: { color: "#8b949e" } },
      yAxis: [{ type: "value", min: 0, max: 100, position: "left",
                axisLabel: { color: "#8b949e", formatter: "{value}" },
                splitLine: { lineStyle: { color: "#1c2330" } } },
              { type: "value", position: "right", axisLabel: { color: "#8b949e" },
                splitLine: { show: false } }],
      series: [
        { name: "RSI", type: "line", data: rsi, yAxisIndex: 0, showSymbol: false,
          lineStyle: { color: "#f5a623" },
          markLine: { silent: true, symbol: "none", data: [
            { yAxis: 70, lineStyle: { color: "#ef4b4b", type: "dashed" } },
            { yAxis: 30, lineStyle: { color: "#1fb86b", type: "dashed" } } ] } },
        { name: "MACD", type: "bar", data: hist, yAxisIndex: 1,
          itemStyle: { color: (p) => p.data >= 0 ? COLORS.bull : COLORS.bear } },
        { name: "DIF", type: "line", data: dif, yAxisIndex: 1, showSymbol: false,
          lineStyle: { color: "#3b82f6" } },
        { name: "DEA", type: "line", data: dea, yAxisIndex: 1, showSymbol: false,
          lineStyle: { color: "#a855f7" } }
      ]
    }, true);
  }

  // ---------------- 牛熊维度分条图（横向条形，可缩放）----------------
  function scoreColor(s) {
    return s >= 0.15 ? COLORS.bull : (s <= -0.15 ? COLORS.bear : COLORS.neutral);
  }
  function renderHeatmap() {
    const mk = DATA.markets || {};
    let dims = Object.keys(mk).map((k) => {
      const m = mk[k];
      return { key: k, label: WEIGHT_LABELS[k] || m.label || k,
        score: typeof m.score === "number" ? m.score : 0,
        verdict: m.verdict, sub: m.sub || "", series: m.series || [] };
    });
    if (!dims.length) {
      heatChart.setOption({ title: { text: "暂无维度数据", left: "center", top: "center",
        textStyle: { color: "#8b949e" } } });
      return;
    }
    // 按综合分从高到低排序（最偏牛在顶，最偏熊在底）
    dims.sort((a, b) => b.score - a.score);

    // 从 heatmap 收集每个维度的子指标，用于行内微点 + tooltip
    const subMap = {};
    (DATA.heatmap || []).forEach(([g, nm, sc]) => {
      (subMap[g] = subMap[g] || []).push({ name: nm, score: sc });
    });

    const cats = dims.map((d) => d.label);
    const pos = dims.map((d) => Math.max(0, d.score));   // 偏多部分（向右）
    const neg = dims.map((d) => Math.min(0, d.score));   // 偏空部分（向左）
    const dotData = [];
    dims.forEach((d, i) => {
      (subMap[d.label] || []).forEach((s) => {
        dotData.push({ value: [s.score, i], name: s.name,
          itemStyle: { color: scoreColor(s.score) } });
      });
    });

    heatChart.setOption({
      backgroundColor: "transparent",
      grid: { left: 72, right: 60, top: 30, bottom: 26 },
      // 整体顶部多/空方向提示（中国惯例：多=红·涨 / 空=绿·跌）
      graphic: [
        { type: "text", left: "14%", top: 6,
          style: { text: "◀ 空", fill: COLORS.bear, fontSize: 13, fontWeight: 700,
            textAlign: "center" } },
        { type: "text", right: "14%", top: 6,
          style: { text: "多 ▶", fill: COLORS.bull, fontSize: 13, fontWeight: 700,
            textAlign: "center" } }
      ],
      tooltip: {
        trigger: "item",
        formatter: (p) => {
          if (p.seriesType === "scatter") {
            return `${p.data.name}<br/>子指标得分：${Math.round(p.data.value[0] * 100)}`;
          }
          const d = dims[p.dataIndex];
          const hint = verbalHint(d.key, d.score) || "";
          let html = `<b style="color:#c9d1d9">${d.label}</b> ${VERDICT_TEXT[d.verdict] || ""}<br/>` +
            `综合分：${(d.score >= 0 ? "+" : "") + Math.round(d.score * 100)}`;
          if (hint) html += `<br/><span style="color:#8b949e;font-size:11px">${hint}</span>`;
          const subs = subMap[d.label] || [];
          if (subs.length) {
            html += `<br/><span style="color:#6e7681;font-size:11px">子指标：` +
              subs.map((s) => `${s.name} ${Math.round(s.score * 100)}`).join(" / ") + `</span>`;
          }
          return html;
        }
      },
      xAxis: {
        type: "value", min: -1.1, max: 1.1,
        axisLabel: { color: "#8b949e", fontSize: 11,
          formatter: (v) => (v > 0 ? "+" + v : "" + v) },
        splitLine: { lineStyle: { color: "#21262d" } },
        axisLine: { lineStyle: { color: "#30363d" } }
      },
      yAxis: {
        type: "category", data: cats, inverse: true,
        axisLabel: { color: "#c9d1d9", fontSize: 12 },
        axisLine: { lineStyle: { color: "#30363d" } },
        axisTick: { show: false }
      },
      series: [
        { name: "偏空", type: "bar", stack: "t", data: neg, barWidth: "56%",
          itemStyle: { color: COLORS.bear },
          label: { show: true, position: "left", color: COLORS.bear, fontSize: 11,
            formatter: (p) => (p.value < 0 ? Math.round(p.value * 100) : "") } },
        { name: "偏多", type: "bar", stack: "t", data: pos, barWidth: "56%",
          itemStyle: { color: COLORS.bull },
          label: { show: true, position: "right", color: COLORS.bull, fontSize: 11,
            formatter: (p) => (p.value > 0 ? "+" + Math.round(p.value * 100) : "") },
          markLine: { silent: true, symbol: "none",
            lineStyle: { color: "#6e7681", type: "dashed", width: 1 },
            data: [{ xAxis: 0 }], label: { show: false } } },
        { name: "子指标", type: "scatter", data: dotData, symbolSize: 7, z: 5,
          tooltip: { show: true } }
      ]
    }, true);
  }

  // ---------------- 明细表 ----------------
  function renderTable() {
    const tb = document.querySelector("#detailTable tbody");
    tb.innerHTML = "";
    ALL_SERIES.forEach((it) => {
      const s = it.s;
      const n = s.close.length - 1;
      const ma = (s.ma20[n] != null && s.ma60[n] != null && s.ma200[n] != null)
        ? ((s.close[n] > s.ma20[n] ? 1 : 0) + (s.ma20[n] > s.ma60[n] ? 1 : 0) + (s.ma60[n] > s.ma200[n] ? 1 : 0))
        : null;
      const maTxt = ma == null ? "—" : ["空头排列", "", "", "", "多头排列"][ma];
      const rsi = s.rsi[n] != null ? s.rsi[n] : "—";
      const macd = s.macdHist[n] != null ? (s.macdHist[n] > 0 ? "红柱" : "绿柱") : "—";
      const vs = s.volScore != null ? s.volScore : null;
      const volCls = vs == null ? "neutral" : (vs > 0.2 ? "bull" : vs < -0.2 ? "bear" : "neutral");
      const volTxt = vs == null ? "—" : (vs > 0.2 ? "放量配合↑" : vs < -0.2 ? "派发/缩量↓" : "中性");
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${it.market}</td><td>${s.name}</td><td>${s.latest}</td>
        <td class="score-pill ${s.verdict}">${(s.score * 100).toFixed(0)}</td>
        <td><span class="badge ${s.verdict}">${VERDICT_TEXT[s.verdict]}</span></td>
        <td>${maTxt}</td><td>${rsi}</td><td>${macd}</td>
        <td class="score-pill ${volCls}">${volTxt}</td>`;
      tb.appendChild(tr);
    });
  }

  // ---------------- 范式周期 (Pérez) ----------------
  function renderParadigm() {
    const tl = document.getElementById("paradigmTimeline");
    const ph = document.getElementById("paradigmPhases");
    const note = document.getElementById("paradigmNote");
    if (!tl || !ph || !note) return;
    fetch("./data/paradigm.json", { cache: "no-store" })
      .then((r) => r.json())
      .then((p) => {
        tl.innerHTML = p.revolutions.map((rv) => {
          const cur = rv.current ? " current" : "";
          return `<div class="rev${cur}"><div class="rev-year">${rv.start}</div>` +
            `<div class="rev-name">${rv.name}</div><div class="rev-tech">${rv.tech}</div></div>`;
        }).join('<div class="rev-arrow">→</div>');
        ph.innerHTML = p.phases.map((pg) => {
          const cur = pg.current ? " current" : "";
          return `<div class="phase${cur}"><div class="phase-name">${pg.name}` +
            `<small>${pg.period}</small></div><div class="phase-desc">${pg.desc}</div></div>`;
        }).join("");
        note.innerHTML = `<b>当前定位：</b>${p.interpretation}` +
          `<br/><span class="tag">来源：${p.meta.source}（${p.meta.updated} 复核）</span>`;
        const sig = document.getElementById("paradigmStructural");
        if (sig && DATA && DATA.structural_signal) {
          sig.textContent = "结构信号（数据校验）：" + DATA.structural_signal;
        }
      })
      .catch(() => { /* 非关键模块，静默降级 */ });
  }

  // ---------------- 交互 ----------------
  function buildSelectors() {
    const sel = document.getElementById("seriesSelect");
    const cmp = document.getElementById("compareSelect");
    sel.innerHTML = "";
    ALL_SERIES.forEach((it, i) => {
      const opt = document.createElement("option");
      opt.value = i; opt.textContent = `${it.market} · ${it.name}`;
      sel.appendChild(opt);
      const co = document.createElement("option");
      co.value = i; co.textContent = `${it.market} · ${it.name}`;
      cmp.appendChild(co);
    });
    sel.value = state.seriesId;
    sel.onchange = (e) => { state.seriesId = +e.target.value; renderKline(); renderIndicator(); };
    cmp.onchange = (e) => { state.compareId = e.target.value; renderKline(); };
    document.querySelectorAll(".btn.range").forEach((b) => {
      b.onclick = () => {
        document.querySelectorAll(".btn.range").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.range = +b.dataset.r; renderKline(); renderIndicator();
      };
    });
  }

  // ---------------- 启动 ----------------
  fetch("./data/market.json", { cache: "no-store" })
    .then((r) => r.json())
    .then((d) => {
      DATA = d;
      document.getElementById("updated").textContent = d.generated_at || "—";
      ALL_SERIES = collectSeries();
      if (!ALL_SERIES.length) {
        document.getElementById("gauges").innerHTML =
          '<div class="empty">未检索到可用序列，请检查数据源配置（见 README）。</div>';
        return;
      }
      renderGauges();
      buildSelectors();
      renderKline();
      renderIndicator();
      renderHeatmap();
      renderTable();
      renderParadigm();
      renderWeightPanel();
      renderHero();
    })
    .catch((e) => {
      document.getElementById("gauges").innerHTML =
        '<div class="empty">数据加载失败：' + e + '（请确认 data/market.json 已生成）</div>';
    });
})();
