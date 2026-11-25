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
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) {
    const t = await r.text();
    alert(`操作被拒绝：${r.status} ${t}`);
  }
  poll();
}

document.querySelectorAll("button[data-act]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const kind = btn.closest(".panel").id.replace("panel-", "");
    if (btn.dataset.act === "start") {
      let url = `/api/jobs/${kind}/start`;
      if (kind === "emergency" && document.getElementById("simulate-fail").checked) {
        url += "?simulate_fail=true";
      }
      post(url);
    } else if (btn.dataset.act === "reset") {
      post(`/api/jobs/${kind}/reset`);
    }
  });
});

async function poll() {
  let s;
  try {
    const r = await fetch("/api/status");
    s = await r.json();
  } catch (e) {
    return;
  }
  document.getElementById("global").textContent =
    "当前渲染进程：" + (s.running_kind ? KIND_CN[s.running_kind] : "无（空闲）");
  for (const kind of KINDS) {
    const j = s.jobs[kind];
    const panel = document.getElementById(`panel-${kind}`);
    panel.dataset.status = j.status;
    panel.querySelector(".badge").textContent = CN[j.status] || j.status;
    panel.querySelector(".fill").style.width = j.percent + "%";
    let info = `${j.published}/${j.total}（${j.percent}%）`;
    if (j.current_tile) info += ` · 正在渲染 ${j.current_tile.join("/")}`;
    if (j.plan) info += ` · 本轮计划 ${j.plan.remaining}/${j.plan.total} 张`;
    if (j.error) info += ` · 错误：${j.error}`;
    panel.querySelector(".info").textContent = info;
    const pub = new Set(j.tiles);
    for (const key in cells[kind]) {
      const a = cells[kind][key];
      const has = pub.has(key);
      if (has && !a.dataset.on) {
        a.dataset.on = "1";
        a.classList.remove("empty");
        const img = document.createElement("img");
        img.src = `/tiles/${kind}/${key}.png`;
        img.alt = key;
        a.href = img.src;
        a.appendChild(img);
      } else if (!has && a.dataset.on) {
        delete a.dataset.on;
        a.classList.add("empty");
        a.innerHTML = "";
        a.removeAttribute("href");
      }
    }
  }
}

setInterval(poll, 500);
poll();
