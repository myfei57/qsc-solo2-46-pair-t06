const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  return response.json();
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function renderCards(entries) {
  const cards = $("overview-cards");
  cards.innerHTML = "";
  for (const [label, value, tone] of entries) {
    const card = document.createElement("div");
    card.className = "card" + (tone ? " " + tone : "");
    const labelNode = document.createElement("div");
    labelNode.className = "label";
    labelNode.textContent = label;
    const valueNode = document.createElement("div");
    valueNode.className = "value";
    valueNode.textContent = String(value);
    card.appendChild(labelNode);
    card.appendChild(valueNode);
    cards.appendChild(card);
  }
}

function setOnline(online) {
  const node = $("conn-status");
  node.textContent = online ? "已连接" : "连接中断";
  node.classList.toggle("offline", !online);
}

const STATION_LABEL = { cab: "驾驶室", remote: "遥控器" };
let lastControl = {};

async function refresh() {
  let state;
  try {
    state = await api("/api/state");
  } catch (error) {
    setOnline(false);
    return;
  }
  setOnline(true);
  const data = state.data || {};
  const zone = data.zone || {};
  const slew = data.slew || {};
  const hoist = data.hoist || {};
  const wind = data.wind || {};
  const limit = data.limit || {};
  const cluster = data.cluster || {};
  const pair = cluster.nearest_pair || {};
  const control = data.control || {};
  renderControl(control);
  renderCards([
    ["塔吊数量", Object.keys(cluster.positions || {}).length],
    ["操纵端", control.holder ? STATION_LABEL[control.holder] || control.holder : "无人接管", control.estop ? "danger" : ""],
    ["待交接", control.pending_handover ? (STATION_LABEL[control.pending_handover.offered_by] || control.pending_handover.offered_by) + " → " + (STATION_LABEL[control.pending_handover.offered_to] || control.pending_handover.offered_to) : "无", control.pending_handover ? "warn" : ""],
    ["紧急停止", control.estop ? "按下（" + (STATION_LABEL[control.estop_source] || control.estop_source) + "）" : "正常", control.estop ? "danger" : ""],
    ["分区占用", (zone.occupants || []).length, zone.conflict ? "danger" : ""],
    ["冲突闩锁", zone.latch ? "锁定" : "释放", zone.latch ? "danger" : ""],
    ["回转状态", slew.moving ? "运转" : "停止", wind.active ? "warn" : ""],
    ["落钩状态", hoist.hook_lowered ? "已落钩" : "未落钩", hoist.hook_lowered ? "" : "warn"],
    ["制动状态", hoist.brake_engaged ? "抱闸" : "松闸"],
    ["风速", (wind.level || 0).toFixed(1) + " m/s", wind.active ? "danger" : ""],
    ["限位闩锁", limit.latch ? "锁定" : "释放", limit.latch ? "danger" : ""],
    ["最近间距", pair.distance ? pair.distance.toFixed(1) + " 米" : "—", pair.below_safe ? "danger" : ""],
    ["审计记录", data.audit || 0],
  ]);
  $("crane-data").textContent = JSON.stringify(cluster, null, 2);
  $("zone-data").textContent = JSON.stringify(zone, null, 2);
  $("hoist-data").textContent = JSON.stringify(hoist, null, 2);
  $("alarm-data").textContent = JSON.stringify({ wind, limit }, null, 2);
  $("config-data").textContent = JSON.stringify(data.config || {}, null, 2);
  const audit = await api("/api/audit?limit=15");
  $("audit-data").textContent = JSON.stringify(audit.data || [], null, 2);
  $("overview-log").textContent = JSON.stringify((audit.data || []).slice(0, 5), null, 2);
}

document.querySelectorAll("#nav button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("#nav button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    document.querySelectorAll("main section").forEach((section) => section.classList.add("hidden"));
    $("page-" + button.dataset.page).classList.remove("hidden");
  });
});

$("survey-btn").addEventListener("click", async () => {
  await post("/api/cluster/survey", {
    cranes: { "crane-1": { x: 0, y: 0 }, "crane-2": { x: 82, y: 1 } },
  });
  refresh();
});

$("reset-crane-btn").addEventListener("click", async () => {
  await post("/api/crane/reset");
  refresh();
});

$("occupy-a-btn").addEventListener("click", async () => {
  await post("/api/zone/occupy", { crane_id: "crane-1" });
  refresh();
});

$("occupy-b-btn").addEventListener("click", async () => {
  await post("/api/zone/occupy", { crane_id: "crane-2" });
  refresh();
});

$("clear-conflict-btn").addEventListener("click", async () => {
  await post("/api/zone/clear");
  refresh();
});

$("slew-start-btn").addEventListener("click", async () => {
  await post("/api/slew/start");
  refresh();
});

$("lower-btn").addEventListener("click", async () => {
  await post("/api/crane/command", { source: "cab", action: "lower", axis: "hoist" });
  refresh();
});

$("feedback-btn").addEventListener("click", async () => {
  await post("/api/hoist/feedback", { speed: 1.2 });
  refresh();
});

$("release-btn").addEventListener("click", async () => {
  await post("/api/hoist/release");
  refresh();
});

$("trolley-btn").addEventListener("click", async () => {
  await post("/api/crane/command", { source: "cab", action: "trolley", axis: "trolley" });
  refresh();
});

$("hoist-stop-btn").addEventListener("click", async () => {
  await post("/api/hoist/stop");
  refresh();
});

$("wind-alarm-btn").addEventListener("click", async () => {
  await post("/api/wind/alarm", { level: 14 });
  refresh();
});

$("wind-clear-btn").addEventListener("click", async () => {
  await post("/api/wind/clear");
  refresh();
});

$("limit-check-btn").addEventListener("click", async () => {
  await post("/api/limit/check");
  refresh();
});

$("slew-fault-btn").addEventListener("click", async () => {
  await post("/api/slew/fault", { faulted: true });
  refresh();
});

$("slew-recover-btn").addEventListener("click", async () => {
  await post("/api/slew/fault", { faulted: false });
  refresh();
});

$("config-save-btn").addEventListener("click", async () => {
  await post("/api/config", { site_name: "华东三号工地", safe_separation: 15 });
  refresh();
});

// ----------------------------------------------------------- control station

function renderControl(control) {
  lastControl = control;
  const cards = $("control-cards");
  const holderText = control.holder ? STATION_LABEL[control.holder] : "无人接管";
  const pendingText = control.pending_handover
    ? (STATION_LABEL[control.pending_handover.offered_by] || control.pending_handover.offered_by) +
      " 已向 " +
      (STATION_LABEL[control.pending_handover.offered_to] || control.pending_handover.offered_to) +
      " 交出，等待接受"
    : "无";
  cards.innerHTML = "";
  renderCardsInto(cards, [
    ["当前操纵端", holderText, control.estop ? "danger" : ""],
    ["待处理交接", pendingText, control.pending_handover ? "warn" : ""],
    ["紧急停止", control.estop ? "已按下" : "正常", control.estop ? "danger" : ""],
    ["急停来源", control.estop_source ? STATION_LABEL[control.estop_source] || control.estop_source : "—"],
    ["交接代数", control.generation || 0],
  ]);
  $("control-data").textContent = JSON.stringify(control, null, 2);
}

function renderCardsInto(cards, entries) {
  for (const [label, value, tone] of entries) {
    const card = document.createElement("div");
    card.className = "card" + (tone ? " " + tone : "");
    const labelNode = document.createElement("div");
    labelNode.className = "label";
    labelNode.textContent = label;
    const valueNode = document.createElement("div");
    valueNode.className = "value";
    valueNode.textContent = String(value);
    card.appendChild(labelNode);
    card.appendChild(valueNode);
    cards.appendChild(card);
  }
}

function setControlMessage(text, isError) {
  const node = $("control-message");
  node.textContent = text;
  node.classList.toggle("error", Boolean(isError));
}

async function controlAction(path, body) {
  const result = await post(path, body);
  if (result.ok) {
    setControlMessage("操作成功", false);
  } else {
    setControlMessage("被拒绝：" + (result.error || "未知原因"), true);
  }
  refresh();
}

$("ctl-take-cab-btn").addEventListener("click", () =>
  controlAction("/api/control/take", { station: "cab" })
);
$("ctl-take-remote-btn").addEventListener("click", () =>
  controlAction("/api/control/take", { station: "remote" })
);
$("ctl-offer-cab-btn").addEventListener("click", () =>
  controlAction("/api/control/offer", { station: "cab" })
);
$("ctl-offer-remote-btn").addEventListener("click", () =>
  controlAction("/api/control/offer", { station: "remote" })
);
$("ctl-accept-cab-btn").addEventListener("click", () =>
  controlAction("/api/control/accept", { station: "cab" })
);
$("ctl-accept-remote-btn").addEventListener("click", () =>
  controlAction("/api/control/accept", { station: "remote" })
);
$("ctl-reject-cab-btn").addEventListener("click", () =>
  controlAction("/api/control/reject", { station: "cab" })
);
$("ctl-reject-remote-btn").addEventListener("click", () =>
  controlAction("/api/control/reject", { station: "remote" })
);
$("ctl-cancel-btn").addEventListener("click", () => {
  const station = (lastControl.pending_handover || {}).offered_by || "cab";
  return controlAction("/api/control/cancel", { station });
});
$("ctl-release-cab-btn").addEventListener("click", () =>
  controlAction("/api/control/release", { station: "cab" })
);
$("ctl-release-remote-btn").addEventListener("click", () =>
  controlAction("/api/control/release", { station: "remote" })
);
$("ctl-estop-cab-btn").addEventListener("click", () =>
  controlAction("/api/control/estop", { station: "cab" })
);
$("ctl-estop-remote-btn").addEventListener("click", () =>
  controlAction("/api/control/estop", { station: "remote" })
);
$("ctl-reset-btn").addEventListener("click", () =>
  controlAction("/api/control/reset", { station: lastControl.holder || "cab" })
);

async function probeCommand(station) {
  const result = await post("/api/crane/command", { source: station, action: "lower" });
  if (result.ok) {
    setControlMessage(STATION_LABEL[station] + " 落钩指令已执行", false);
  } else {
    setControlMessage(STATION_LABEL[station] + " 指令被拒绝：" + (result.error || ""), true);
  }
  refresh();
}

$("ctl-cab-lower-btn").addEventListener("click", () => probeCommand("cab"));
$("ctl-remote-lower-btn").addEventListener("click", () => probeCommand("remote"));

async function refreshControlAudit() {
  const params = new URLSearchParams();
  [
    "control-take",
    "control-offer",
    "control-handover",
    "control-handover-reject",
    "control-handover-cancel",
    "control-release",
    "emergency-stop",
    "emergency-stop-reset",
  ].forEach((action) => params.append("action", action));
  const result = await api("/api/audit?" + params.toString());
  $("control-audit").textContent = JSON.stringify(result.data || [], null, 2);
}

setInterval(refreshControlAudit, 3000);
refreshControlAudit();

setInterval(refresh, 3000);
refresh();
