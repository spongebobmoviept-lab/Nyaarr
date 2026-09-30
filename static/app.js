const state = { series: [], activeSeriesId: null, activeSeriesTitle: null };

function $(sel) { return document.querySelector(sel); }

// Release titles, TMDB titles and log details come from third parties
// (indexers, TMDB); escape everything interpolated into innerHTML.
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  const res = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error((data && data.detail) || res.statusText || "Request failed");
  return data;
}

function showBanner(message, isError) {
  const el = $("#status-banner");
  el.textContent = message;
  el.className = "status-banner visible" + (isError ? " error" : "");
  setTimeout(() => el.classList.remove("visible"), 4000);
}

// ---- Tabs ----
function activateTab(tab) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab === "series") loadSeries();
  if (tab === "movies") loadMovies();
  if (tab === "history") loadHistory();
  if (tab === "settings") { loadConnections(); loadSettings(); }
  if (tab === "log") loadLog();
}
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

// ---- Series / Episodes ----
async function loadSeries() {
  const list = $("#series-list");
  try {
    state.series = await api("/api/series");
  } catch (e) {
    list.innerHTML = `<p class="muted">Couldn't load series: ${esc(e.message)}</p>`;
    return;
  }
  list.innerHTML = "";
  if (!state.series.length) {
    list.innerHTML = `<p class="muted">No anime series found. Check your anime tag name in Settings, or that seriesType is set correctly in Sonarr.</p>`;
    return;
  }
  for (const s of state.series) {
    const row = document.createElement("div");
    row.className = "series-row";
    row.innerHTML = `
      <span class="series-title">${esc(s.title)}</span>
      <span class="series-pref-badge ${esc(s.preference)}">${esc(s.preference.replace("_", " "))}</span>
    `;
    row.addEventListener("click", () => openSeries(s.id, s.title, s.preference));
    list.appendChild(row);
  }
}

async function openSeries(seriesId, title, preference) {
  state.activeSeriesId = seriesId;
  state.activeSeriesTitle = title;
  $("#episode-panel").style.display = "block";
  $("#episode-panel-title").textContent = title;
  $("#series-pref-select").value = preference;
  $("#movie-candidates-panel").innerHTML = "";
  await loadEpisodes();
}

$("#series-pref-select").addEventListener("change", async (e) => {
  if (!state.activeSeriesId) return;
  await api(`/api/series/${state.activeSeriesId}/preference`, { method: "POST", body: { preference: e.target.value } });
  showBanner("Preference saved — re-checking episodes against the new preference…");
  await loadSeries();
  // Existing candidates were filtered/ranked under the OLD preference — a
  // dub-preferring switch, for example, needs the affected episodes
  // re-searched, not just re-displayed, since dub releases were excluded
  // from the candidate list entirely under a sub preference and won't
  // appear until they're actually looked for again.
  await recheckAllNeedsReview();
});

async function recheckAllNeedsReview() {
  // Deliberately scoped to "needs_review" only (already-cached candidates
  // that would now be mis-ranked/mis-filtered under the new preference) —
  // NOT "aired_no_release" too, which could mean a blind re-search across
  // hundreds of never-successfully-checked episodes on a long-running show.
  // Those still update correctly via the normal background scan or a
  // manual "Check Now".
  const rows = await api(`/api/series/${state.activeSeriesId}/episodes`);
  const toRecheck = rows.filter((r) => r.status === "needs_review");
  for (const ep of toRecheck) {
    await api(`/api/episodes/${ep.episode_id}/check`, { method: "POST", body: { series_title: state.activeSeriesTitle, series_id: state.activeSeriesId } }).catch(() => {});
  }
  await loadEpisodes();
}

async function loadEpisodes() {
  const body = $("#episode-table-body");
  body.innerHTML = `<tr><td colspan="4" class="muted">Loading…</td></tr>`;
  let episodes;
  try {
    episodes = await api(`/api/series/${state.activeSeriesId}/episodes`);
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4" class="muted">Couldn't load episodes: ${esc(e.message)}</td></tr>`;
    return;
  }
  body.innerHTML = "";
  if (!episodes.length) {
    body.innerHTML = `<tr><td colspan="4" class="muted">Nothing missing — fully monitored and complete, or nothing monitored yet.</td></tr>`;
    return;
  }
  for (const ep of episodes) {
    const tr = document.createElement("tr");
    const airDate = ep.air_date_utc ? new Date(ep.air_date_utc).toLocaleDateString() : "—";
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `ep-status ${ep.status}`;
    badge.textContent = ep.status.replace(/_/g, " ");
    statusCell.appendChild(badge);
    if (ep.status === "not_yet_checked" || ep.status === "aired_no_release") {
      const checkBtn = document.createElement("button");
      checkBtn.className = "btn-trigger";
      checkBtn.style.marginLeft = "0.5rem";
      checkBtn.textContent = "Check Now";
      checkBtn.addEventListener("click", async () => {
        checkBtn.disabled = true;
        checkBtn.textContent = "Checking…";
        await api(`/api/episodes/${ep.episode_id}/check`, { method: "POST", body: { series_title: state.activeSeriesTitle, series_id: state.activeSeriesId } });
        await loadEpisodes();
      });
      statusCell.appendChild(checkBtn);
    }
    tr.innerHTML = `
      <td>S${String(ep.season).padStart(2, "0")}E${String(ep.episode).padStart(2, "0")}</td>
      <td>${esc(ep.title)}</td>
      <td>${airDate}</td>
    `;
    tr.appendChild(statusCell);
    body.appendChild(tr);
    if (ep.status === "needs_review" && ep.candidates && ep.candidates.length) {
      const candRow = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.appendChild(renderCandidateList(ep.candidates, async (guid, indexerId) => {
        const episodeLabel = `S${String(ep.season).padStart(2, "0")}E${String(ep.episode).padStart(2, "0")}`;
        await api(`/api/episodes/${ep.episode_id}/grab`, { method: "POST", body: { guid, indexer_id: indexerId, series_title: state.activeSeriesTitle, episode_label: episodeLabel, series_id: state.activeSeriesId } });
        showBanner(`Grabbed for ${state.activeSeriesTitle} S${ep.season}E${ep.episode}.`);
        await loadEpisodes();
      }));
      candRow.appendChild(td);
      body.appendChild(candRow);
    }
  }
}

function renderCandidateList(candidates, onGrab) {
  const wrap = document.createElement("div");
  wrap.className = "candidate-list";
  for (const c of candidates) {
    const card = document.createElement("div");
    card.className = "candidate-card";
    const cls = c.classification;
    card.innerHTML = `
      <div class="candidate-main">
        <div class="candidate-title"><span class="lang-badge ${esc(cls.kind)}">${esc(cls.kind.replace("_", " "))}</span>${esc(c.title)}</div>
        <div class="candidate-meta">${esc(c.seeders)} seeders · ${esc(c.size_gb)} GB · ${esc(cls.reason)}</div>
      </div>
    `;
    const btn = document.createElement("button");
    btn.className = "btn-gold";
    btn.textContent = "Grab";
    btn.addEventListener("click", () => onGrab(c.guid, c.indexer_id));
    card.appendChild(btn);
    wrap.appendChild(card);
  }
  return wrap;
}

$("#run-scan-now").addEventListener("click", async () => {
  await api("/api/debug/run-scan-now", { method: "POST" });
  $("#scan-status").textContent = "Scan started — refresh in a moment.";
  setTimeout(() => { $("#scan-status").textContent = ""; }, 5000);
});

// ---- Franchise movie discovery ----
$("#find-movies-btn").addEventListener("click", async () => {
  const panel = $("#movie-candidates-panel");
  panel.innerHTML = `<p class="muted">Searching TMDB…</p>`;
  let candidates;
  try {
    candidates = await api(`/api/series/${state.activeSeriesId}/find-movies`, { method: "POST" });
  } catch (e) {
    panel.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    return;
  }
  if (!candidates.length) {
    panel.innerHTML = `<p class="muted">No plausible movie matches found for "${esc(state.activeSeriesTitle)}".</p>`;
    return;
  }
  panel.innerHTML = `<h3 style="margin-top:0.5rem;">Related Movies Found — confirm before adding</h3>`;
  const grid = document.createElement("div");
  grid.className = "movie-candidate-grid";
  for (const c of candidates) {
    const card = document.createElement("div");
    card.className = "movie-candidate-card";
    card.innerHTML = `
      ${c.poster_url ? `<img src="${esc(c.poster_url)}" alt="">` : ""}
      <div class="movie-candidate-body">
        <div class="movie-candidate-title">${esc(c.title)} (${esc(c.year)})</div>
        <div class="movie-candidate-meta">${esc(c.runtime_minutes)} min · ${esc(c.vote_count)} votes</div>
      </div>
    `;
    const btn = document.createElement("button");
    btn.className = "btn-gold";
    btn.textContent = "Add to Radarr";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Adding…";
      try {
        // quality_profile_id / root_folder_path are omitted here on purpose —
        // the backend falls back to the defaults set once in Settings
        // ("Movie library defaults"), populated from Radarr's own live list.
        await api("/api/movies/add", {
          method: "POST",
          body: { tmdb_id: c.tmdb_id, source_series_id: state.activeSeriesId },
        });
        showBanner(`Added "${c.title}" to Radarr.`);
        btn.textContent = "Added";
      } catch (e) {
        showBanner(e.message, true);
        btn.disabled = false;
        btn.textContent = "Add to Radarr";
      }
    });
    card.querySelector(".movie-candidate-body").appendChild(btn);
    grid.appendChild(card);
  }
  panel.appendChild(grid);
});

// ---- Movies tab ----
async function loadMovies() {
  const list = $("#movies-list");
  let movies;
  try {
    movies = await api("/api/movies");
  } catch (e) {
    list.innerHTML = `<p class="muted">Couldn't load movies: ${esc(e.message)}</p>`;
    return;
  }
  list.innerHTML = "";
  if (!movies.length) {
    list.innerHTML = `<p class="muted">No movies added yet — use "Find Related Movies" from a series page.</p>`;
    return;
  }
  for (const m of movies) {
    const row = document.createElement("div");
    row.className = "queue-row";
    row.innerHTML = `
      <div></div>
      <div class="queue-main">
        <div class="queue-title-row"><span class="queue-title">${esc(m.title)}</span><span class="ep-status ${esc(m.status)}">${esc(m.status.replace(/_/g, " "))}</span></div>
      </div>
      <div class="queue-actions"></div>
    `;
    if (m.status === "needs_review" && m.candidates && m.candidates.length) {
      row.querySelector(".queue-main").appendChild(
        renderCandidateList(m.candidates, async (guid, indexerId) => {
          await api(`/api/movies/${m.movie_id}/grab`, { method: "POST", body: { guid, indexer_id: indexerId, title: m.title } });
          showBanner(`Grabbed for ${m.title}.`);
          await loadMovies();
        })
      );
    }
    if (m.status === "not_yet_checked" || m.status === "aired_no_release") {
      const checkBtn = document.createElement("button");
      checkBtn.className = "btn-trigger";
      checkBtn.textContent = "Check Now";
      checkBtn.addEventListener("click", async () => {
        checkBtn.disabled = true;
        checkBtn.textContent = "Checking…";
        await api(`/api/movies/${m.movie_id}/check`, { method: "POST" });
        await loadMovies();
      });
      row.querySelector(".queue-actions").appendChild(checkBtn);
    }
    list.appendChild(row);
  }
}

// ---- History ----
async function loadHistory() {
  const list = $("#history-list");
  const history = await api("/api/history").catch(() => []);
  list.innerHTML = "";
  if (!history.length) {
    list.innerHTML = `<p class="muted">Nothing decided yet.</p>`;
    return;
  }
  for (const h of history) {
    const row = document.createElement("div");
    row.className = "queue-row";
    row.innerHTML = `
      <div></div>
      <div class="queue-main">
        <div class="queue-title-row"><span class="queue-title">${esc(h.title)}</span><span class="outcome-badge ${h.outcome === 'grabbed' ? 'done' : ''}">${esc(h.outcome.replace(/_/g, " "))}</span></div>
        <div class="queue-detail">${esc(h.detail)}</div>
      </div>
      <div class="queue-detail">${new Date(h.at).toLocaleString()}</div>
    `;
    list.appendChild(row);
  }
}

// ---- Settings: Connections ----
const CONNECTIONS_LABELS = {
  sonarr_url: "Sonarr URL (e.g. http://192.168.1.10:8989)",
  radarr_url: "Radarr URL (e.g. http://192.168.1.10:7878)",
  sonarr_api_key: "Sonarr API Key",
  radarr_api_key: "Radarr API Key",
  tmdb_api_key: "TMDB API Key (optional — needed for Find Related Movies)",
  discord_webhook_url: "Discord Webhook URL (optional)",
};

async function loadConnections() {
  const form = $("#connections-form");
  const data = await api("/api/connections").catch(() => ({}));
  form.innerHTML = "";
  for (const [key, info] of Object.entries(data)) {
    const field = document.createElement("div");
    field.className = "settings-field";
    const label = document.createElement("label");
    label.textContent = CONNECTIONS_LABELS[key] || key;
    const input = document.createElement("input");
    input.type = info.is_secret ? "password" : "text";
    input.name = key;
    input.placeholder = info.is_secret ? (info.is_set ? `Currently set (••••${info.value.slice(-4)})` : "Not set") : "";
    input.value = info.is_secret ? "" : info.value;
    field.appendChild(label);
    field.appendChild(input);
    form.appendChild(field);
  }
}

$("#connections-save").addEventListener("click", async () => {
  const form = $("#connections-form");
  const update = {};
  form.querySelectorAll("input").forEach((input) => { update[input.name] = input.value; });
  try {
    await api("/api/connections", { method: "POST", body: update });
    $("#connections-status").textContent = "Saved.";
    await loadConnections();
  } catch (e) {
    $("#connections-status").textContent = e.message;
  }
});

// ---- Settings: Tuning ----
const SETTINGS_LABELS = {
  anime_tag_name: "Anime tag name (fallback when seriesType isn't set)",
  min_seeders: "Minimum seeders (dead releases below this are never picked)",
  known_fansub_groups: "Known fansub groups (comma-separated, trusted as sub-confidence)",
  default_language_preference: "Default preference (prefer_sub / prefer_dub / no_preference)",
  scan_interval_minutes: "Background scan interval (minutes)",
  max_episodes_per_scan: "Max episodes searched per scan cycle (paces indexer load on large libraries)",
  new_episodes_only_days: "Only scan episodes that aired within this many days (0 = whole missing backlog)",
  block_opus_audio: "Block Opus-audio releases (for players that can't play Opus)",
  auto_grab_high_confidence: "Auto-grab HIGH-confidence matches (off = always manual review)",
  movie_min_runtime_minutes: "Movie discovery: minimum runtime (minutes)",
  movie_min_vote_count: "Movie discovery: minimum TMDB vote count",
  default_movie_quality_profile_id: "Movie library default: quality profile",
  default_movie_root_folder: "Movie library default: root folder",
};

async function loadSettings() {
  const form = $("#settings-form");
  const data = await api("/api/settings").catch(() => ({}));
  const libraryOptions = await api("/api/radarr/library-options").catch(() => null);
  form.innerHTML = "";
  for (const [key, value] of Object.entries(data)) {
    const field = document.createElement("div");
    field.className = "settings-field";
    const label = document.createElement("label");
    label.textContent = SETTINGS_LABELS[key] || key;
    let input;
    if (key === "default_movie_quality_profile_id" && libraryOptions) {
      input = document.createElement("select");
      for (const p of libraryOptions.quality_profiles) {
        const opt = document.createElement("option");
        opt.value = p.id; opt.textContent = p.name;
        if (String(p.id) === String(value)) opt.selected = true;
        input.appendChild(opt);
      }
    } else if (key === "default_movie_root_folder" && libraryOptions) {
      input = document.createElement("select");
      for (const path of libraryOptions.root_folders) {
        const opt = document.createElement("option");
        opt.value = path; opt.textContent = path;
        if (path === value) opt.selected = true;
        input.appendChild(opt);
      }
    } else if (typeof value === "boolean") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = value;
      label.className = "checkbox-label";
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.value = Array.isArray(value) ? value.join(",") : value;
    }
    input.name = key;
    field.appendChild(label);
    field.appendChild(input);
    form.appendChild(field);
  }
}

$("#settings-save").addEventListener("click", async () => {
  const form = $("#settings-form");
  const update = {};
  form.querySelectorAll("input").forEach((input) => {
    if (input.type === "checkbox") { update[input.name] = input.checked; return; }
    if (input.name === "known_fansub_groups") { update[input.name] = input.value.split(",").map((s) => s.trim()).filter(Boolean); return; }
    if (["min_seeders", "scan_interval_minutes", "max_episodes_per_scan", "new_episodes_only_days", "movie_min_runtime_minutes", "movie_min_vote_count", "default_movie_quality_profile_id"].includes(input.name)) {
      update[input.name] = Number(input.value);
      return;
    }
    update[input.name] = input.value;
  });
  try {
    await api("/api/settings", { method: "POST", body: update });
    $("#settings-status").textContent = "Saved.";
    await loadSettings();
  } catch (e) {
    $("#settings-status").textContent = e.message;
  }
});

// ---- Log ----
async function loadLog() {
  const lines = await api("/api/log?lines=300").catch(() => []);
  $("#log-view").textContent = lines.join("\n");
}

// ---- Boot ----
loadSeries();
setInterval(() => { if (state.activeSeriesId && document.getElementById("tab-series").classList.contains("active")) loadEpisodes(); }, 30000);
