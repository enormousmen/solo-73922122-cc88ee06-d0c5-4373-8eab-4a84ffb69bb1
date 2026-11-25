const ZOOMS = [0, 1, 2, 3];
const KINDS = ["regular", "emergency"];
const CN = {
  idle: "空闲", queued: "排队中", running: "渲染中",
  interrupted: "被抢占·待续算", done: "已完成", failed: "失败",
};
const KIND_CN = { regular: "常规底图", emergency: "应急航片" };

const cells = {};

function buildGrid(kind) {
  const host = document.querySelector(`#panel-${kind} .pyramid`);
  const map = {};
  for (const z of ZOOMS) {
    const n = 2 ** z;
    const row = document.createElement("div");
    row.className = "zrow";
    row.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
    row.style.maxWidth = `${Math.min(n, 8) * 64}px`;
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        const a = document.createElement("a");
        a.className = "cell empty";
        a.target = "_blank";
        a.title = `${kind} z${z} ${x}/${y}`;
        row.appendChild(a);
        map[`${z}/${x}/${y}`] = a;
      }
    }
    host.appendChild(row);
  }
  return map;
}

for (const k of KINDS) cells[k] = buildGrid(k);

async function post(url) {
  try {
    const r = await fetch(url, { method: "POST" });
    if (!r.ok) {
      const t = await r.text();
      alert(`操作被拒绝：${r.status} ${t}`);
    }
  } catch (e) {
    alert(`请求失败：${e}`);
  }
  poll();
}

document.querySelectorAll("button[data-act]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const kind = btn.closest(".panel").id.replace("panel-", "");
    if (btn.dataset.act === "start") {
      let url = `/api/jobs/${kind}/start`;
      const qs = [];
      if (kind === "emergency" && document.getElementById("simulate-fail").checked) {
        qs.push("simulate_fail=true");
      }
      if (kind === "regular") {
        const ft = document.getElementById("fail-tile").value.trim();
        if (ft) qs.push(`fail_tile=${encodeURIComponent(ft)}`);
      }
      if (qs.length) url += "?" + qs.join("&");
      post(url);
    } else if (btn.dataset.act === "reset") {
      post(`/api/jobs/${kind}/reset`);
    }
  });
});

function setCell(key, state, kind) {
  const a = cells[kind][key];
  if (!a) return;
  a.classList.remove("empty", "rendering", "held", "published", "failed");
  a.classList.add(state);
  if (state === "published" && a.dataset.on !== "1") {
    a.dataset.on = "1";
    const img = document.createElement("img");
    img.src = `/tiles/${kind}/${key}.png`;
    img.alt = key;
    a.href = img.src;
    while (a.firstChild) a.removeChild(a.firstChild);
    a.appendChild(img);
  } else if (state !== "published") {
    delete a.dataset.on;
    a.innerHTML = "";
    a.removeAttribute("href");
  }
}

async function poll() {
  let s;
  try {
    const r = await fetch("/api/status");
    s = await r.json();
  } catch (e) {
    return;
  }
  const procs = [];
  if (s.emergency_worker) procs.push(`应急×1`);
  if (s.regular_workers > 0) procs.push(`常规×${s.regular_workers}`);
  document.getElementById("global").textContent =
    "渲染子进程：" + (procs.length ? procs.join("，") : "无（空闲）") +
    "（常规名额 " + s.regular_slots + "）";

  for (const kind of KINDS) {
    const j = s.jobs[kind];
    const panel = document.getElementById(`panel-${kind}`);
    panel.dataset.status = j.status;
    panel.querySelector(".badge").textContent = CN[j.status] || j.status;
    panel.querySelector(".fill").style.width = j.percent + "%";

    const inflightKeys = new Set();
    const heldKeys = new Set();
    const failZ = new Set();
    for (const it of j.inflight || []) {
      inflightKeys.add(it.tile.join("/"));
    }
    for (const t of j.held || []) heldKeys.add(t);
    for (const L of j.layers || []) if (L.status === "failed") failZ.add(L.z);

    let info = `${j.published}/${j.total}（${j.percent}%）`;
    if (j.current_z !== null && j.current_z !== undefined) info += ` · 当前层 z${j.current_z}`;
    if (inflightKeys.size) info += ` · 渲染中 ${inflightKeys.size} 张`;
    if (heldKeys.size) info += ` · 待出图 ${heldKeys.size} 张`;
    if (j.error) info += ` · 错误：${j.error}`;
    panel.querySelector(".info").textContent = info;

    const pub = new Set(j.tiles);
    for (const key in cells[kind]) {
      const z = Number(key.split("/")[0]);
      let state = "empty";
      if (pub.has(key)) state = "published";
      else if (failZ.has(z)) state = "failed";
      else if (heldKeys.has(key)) state = "held";
      else if (inflightKeys.has(key)) state = "rendering";
      setCell(key, state, kind);
    }
  }
}

setInterval(poll, 400);
poll();
