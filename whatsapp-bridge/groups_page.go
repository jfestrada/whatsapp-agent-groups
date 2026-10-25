package main

const linkPage = `<!DOCTYPE html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp groups</title>
<style>
  :root { color-scheme: light; }
  body { font: 16px/1.45 system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; color: #1c1917; }
  h1 { font-size: 1.4rem; margin-bottom: .4rem; }
  #qr { width: min(360px, 100%); height: auto; background: #fff; }
  input[type="search"] { width: 100%; box-sizing: border-box; padding: .55rem .7rem; margin: .8rem 0; border: 1px solid #d6d3d1; border-radius: .4rem; }
  ul { list-style: none; padding: 0; margin: 0; max-height: 28rem; overflow: auto; border: 1px solid #e7e5e4; border-radius: .4rem; }
  li { border-top: 1px solid #f5f5f4; }
  li:first-child { border-top: 0; }
  label { display: flex; gap: .7rem; align-items: center; padding: .55rem .75rem; cursor: pointer; }
  label:hover { background: #fafaf9; }
  button { margin-top: 1rem; padding: .55rem 1rem; border: 0; border-radius: .4rem; background: #166534; color: #fff; font: inherit; cursor: pointer; }
  button:disabled { opacity: .6; cursor: default; }
  #status { min-height: 1.4em; color: #44403c; }
  .hidden { display: none; }
</style>
<p id="wait">Scan this from WhatsApp: Settings, Linked devices, Link a device. The image refreshes by itself. Scan the image on this page, not a screenshot.</p>
<img id="qr" alt="WhatsApp QR code">
<section id="picker" class="hidden">
  <h1>Choose groups</h1>
  <p>Tick the groups this assistant may read. The choice is stored on this server and kept after a restart.</p>
  <input id="q" type="search" placeholder="Search by name" aria-label="Search groups by name">
  <ul id="list"></ul>
  <button id="save" type="button">Save groups</button>
  <p id="status" role="status"></p>
</section>
<script>
const wait = document.getElementById("wait");
const qr = document.getElementById("qr");
const picker = document.getElementById("picker");
const list = document.getElementById("list");
const search = document.getElementById("q");
const save = document.getElementById("save");
const status = document.getElementById("status");
let qrTimer = null;
let loaded = false;
let loading = false;

function showQR() {
  wait.classList.remove("hidden");
  qr.classList.remove("hidden");
  picker.classList.add("hidden");
  loaded = false;
  if (!qrTimer) {
    const refresh = function () { qr.src = "/qr.png?t=" + Date.now(); };
    refresh();
    qrTimer = setInterval(refresh, 15000);
  }
}

function showPicker() {
  wait.classList.add("hidden");
  qr.classList.add("hidden");
  picker.classList.remove("hidden");
  if (qrTimer) {
    clearInterval(qrTimer);
    qrTimer = null;
  }
  if (!loaded && !loading) loadGroups();
}

function selectedCount() {
  return list.querySelectorAll("input:checked").length;
}

async function loadGroups() {
  loading = true;
  status.textContent = "Loading groups…";
  save.disabled = true;
  try {
    const response = await fetch("/api/groups?fresh=1", {cache: "no-store"});
    if (response.status === 409) {
      loaded = false;
      showQR();
      return;
    }
    if (!response.ok) {
      status.textContent = "Groups are not available yet. This page will try again.";
      return;
    }
    const body = await response.json();
    list.replaceChildren();
    const groups = body.groups || [];
    groups.forEach(function (group) {
      const item = document.createElement("li");
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = group.jid;
      box.checked = !!group.selected;
      const title = document.createElement("span");
      title.textContent = group.name || "Untitled group";
      label.append(box, title);
      item.append(label);
      list.append(item);
    });
    loaded = true;
    save.disabled = false;
    status.textContent = groups.length ? selectedCount() + " selected." : "No groups were found on this account.";
  } catch (err) {
    status.textContent = "Groups are not available yet. This page will try again.";
  } finally {
    loading = false;
  }
}

search.addEventListener("input", function () {
  const query = search.value.trim().toLowerCase();
  list.querySelectorAll("li").forEach(function (item) {
    const name = item.textContent.toLowerCase();
    item.classList.toggle("hidden", query !== "" && name.indexOf(query) === -1);
  });
});

save.addEventListener("click", async function () {
  const jids = Array.from(list.querySelectorAll("input:checked")).map(function (box) { return box.value; });
  save.disabled = true;
  status.textContent = "Saving…";
  try {
    const response = await fetch("/api/groups", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({jids: jids})
    });
    if (!response.ok) {
      status.textContent = "Could not save the groups.";
      save.disabled = false;
      return;
    }
    status.textContent = "Saved " + jids.length + " groups. They stay selected after a restart.";
  } catch (err) {
    status.textContent = "Could not save the groups.";
  }
  save.disabled = false;
});

async function health() {
  try {
    const response = await fetch("/health", {cache: "no-store"});
    const body = await response.json();
    if (body.status === "connected") showPicker();
    else showQR();
  } catch (err) {
    status.textContent = "The bridge is not responding.";
  }
}

health();
setInterval(health, 3000);
</script>
`
