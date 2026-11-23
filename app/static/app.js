const MODE = {
  upload: "Nur hochladen",
  move: "Verschieben (Quelle löschen)",
  mirror_up: "Nach Ziel angleichen",
  download: "Nur herunterladen",
  mirror_down: "Nach Unraid angleichen",
  bisync: "Beidseitig",
};
const STATUS = {
  ok: "erfolgreich",
  error: "Fehler",
  running: "läuft",
  cancelled: "abgebrochen",
  interrupted: "unterbrochen",
};
const WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];

let state = null;
let dirty = false;
let browse = null;
let logOffset = 0;
let previewTimer = 0;

const appEl = document.getElementById("app");
const flashEl = document.getElementById("flash");
const modalEl = document.getElementById("modal");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function parseRoute() {
  const parts = (location.hash || "#/").replace(/^#/, "").split("/").filter(Boolean);
  if (parts[0] === "new") return { name: "edit", id: null };
  if (parts[0] === "job" && parts[1]) return { name: "edit", id: decodeURIComponent(parts[1]) };
  if (parts[0] === "connection" || parts[0] === "settings") return { name: "settings" };
  if (parts[0] === "history") return { name: "history" };
  if (parts[0] === "run" && parts[1]) return { name: "log", id: decodeURIComponent(parts[1]) };
  return { name: "list" };
}

function tabFor(route) {
  if (route.name === "settings") return "settings";
  if (route.name === "history" || route.name === "log") return "history";
  return "list";
}

function fmtTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("de-CH", { timeZone: state?.tz, dateStyle: "medium", timeStyle: "short" });
}

function showError(message) {
  flashEl.classList.remove("ok");
  flashEl.textContent = message || "";
  flashEl.classList.toggle("hidden", !message);
}

function showNote(message) {
  flashEl.classList.add("ok");
  flashEl.textContent = message || "";
  flashEl.classList.toggle("hidden", !message);
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* HTTP-Seiten erlauben die Zwischenablage oft nicht. */
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  area.remove();
  return copied;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    ...options,
  });
  if (response.status === 401) {
    location.href = "/login";
    throw new Error("Anmeldung erforderlich");
  }
  const data = await response.json().catch(() => ({ ok: false, error: "Antwort ungültig" }));
  if (!response.ok || data.ok === false) throw new Error(data.error || response.statusText);
  return data;
}

async function reload() {
  state = await api("/api/state");
  const meta = document.getElementById("header-meta");
  const link = state.connection.ready
    ? (state.connection.mode === "rclone" ? state.connection.rclone_remote : "WebDAV verbunden")
    : "WebDAV offen";
  const bits = [link];
  if (state.rclone) bits.push(state.rclone);
  if (state.app_version) bits.push(`Version ${state.app_version}`);
  meta.textContent = bits.join(" · ");
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabFor(parseRoute()));
  });
}

function go(hash) {
  const route = parseRoute();
  if (dirty && route.name === "edit" && !confirm("Änderungen an diesem Auftrag verwerfen?")) return;
  dirty = false;
  showError("");
  if (location.hash === hash) paint();
  else location.hash = hash;
}

function jobById(id) {
  return (state.jobs || []).find((job) => job.id === id);
}

function isActive(jobId) {
  return (state.active || []).some((run) => run.job_id === jobId);
}

function paint() {
  const route = parseRoute();
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabFor(route));
  });
  if (route.name === "edit") return renderEdit(route.id);
  if (route.name === "settings") return renderSettings();
  if (route.name === "history") return renderHistory();
  if (route.name === "log") return renderLog(route.id);
  return renderList();
}

function banners() {
  const parts = [];
  if (state.env_warning) parts.push(`<div class="banner warn">${esc(state.env_warning)}</div>`);
  if (!state.shares_ok || state.share_count === 0) {
    parts.push(`<div class="banner warn">Keine Shares sichtbar. Im Unraid-Template den Pfad Shares auf /mnt/user setzen.</div>`);
  }
  if (!state.synology_ok) {
    parts.push(`<div class="banner info">SMB-Backup ist optional. Im Template den Path <strong>Remotes</strong> auf <code>${esc(state.synology_example || "/mnt/remotes")}</code> setzen, dann im Auftrag einen Share wählen.</div>`);
  }
  if (!state.connection.ready) {
    const missing = "WebDAV ist noch nicht eingerichtet. Unter Einstellungen ein Ziel wählen oder anlegen.";
    parts.push(`<div class="banner info">${missing}</div>`);
  }
  if (!state.rclone) {
    const why = state.rclone_error ? ` ${esc(state.rclone_error)}` : "";
    parts.push(`<div class="banner warn">rclone wurde im Container nicht gefunden.${why}</div>`);
  }
  return parts.join("");
}

function renderList() {
  const settings = state.settings;
  const cards = state.jobs.map((job) => {
    const active = isActive(job.id);
    const status = active ? "running" : (job.last_status || "");
    const sources = job.mappings.map((item) => item.local || "/").join(", ");
    const kdMode = job.kdrive_mode || job.mode || "upload";
    const synMode = job.synology_mode || (job.mode === "bisync" ? "upload" : job.mode) || "upload";
    const webdavName = job.rclone_remote || state.connection?.rclone_remote || "WebDAV";
    const kdriveChip = job.kdrive_enabled !== false
      ? `<span class="target-chip on">${esc(webdavName)} · ${esc(MODE[kdMode] || kdMode)}</span>`
      : `<span class="target-chip off">WebDAV aus</span>`;
    const smbLabel = job.synology_share ? `SMB · ${job.synology_share}` : "SMB";
    const smbChip = job.synology_enabled
      ? `<span class="target-chip on">${esc(smbLabel)} · ${esc(MODE[synMode] || synMode)}</span>`
      : `<span class="target-chip off">SMB aus</span>`;
    const pairs = job.mappings.map((item) => {
      const ends = [];
      if (job.kdrive_enabled !== false) ends.push(`${webdavName}:${item.remote || "/"}`);
      if (job.synology_enabled) {
        const share = job.synology_share || "?";
        const sub = (item.synology || "").replace(/^\/+|\/+$/g, "");
        ends.push(sub ? `SMB:${share}/${sub}` : `SMB:${share}`);
      }
      return `${item.local || "/"} → ${ends.join(" · ") || "—"}`;
    }).join(", ");
    const needsResync = job.kdrive_enabled !== false && kdMode === "bisync" && !job.bisync_ready;
    return `<article class="card job">
      <div class="split">
        <h2>${esc(job.name)}</h2>
        <span class="pill ${esc(status)}">${esc(status ? STATUS[status] || status : "bereit")}</span>
      </div>
      <div class="target-chips">${kdriveChip}${smbChip}</div>
      <p class="meta">Quelle: ${esc(sources || "—")} · ${esc(job.schedule_label)} · ${job.enabled ? "aktiv" : "pausiert"}<br>${esc(pairs)}</p>
      <p class="meta">Nächster Lauf: ${esc(job.next_run ? fmtTime(job.next_run) : "—")}${job.schedule_block ? ` · ${esc(job.schedule_block)}` : ""}</p>
      ${job.last_summary ? `<p class="meta">${esc(job.last_summary)}</p>` : ""}
      <div class="row">
        <button type="button" class="primary" data-act="edit" data-id="${esc(job.id)}">Bearbeiten</button>
        ${active
          ? `<button type="button" data-act="stop" data-id="${esc(job.id)}">Stopp</button>`
          : `<button type="button" data-act="run" data-id="${esc(job.id)}">Jetzt</button>
             <button type="button" data-act="dry" data-id="${esc(job.id)}">Probelauf</button>`}
        ${needsResync ? `<button type="button" data-act="resync" data-id="${esc(job.id)}">Erstabgleich</button>` : ""}
        <button type="button" data-act="toggle" data-id="${esc(job.id)}">${job.enabled ? "Zeitplan aus" : "Zeitplan an"}</button>
      </div>
      <div class="row">
        <button type="button" data-act="copy-script" data-id="${esc(job.id)}">Skript kopieren</button>
        <button type="button" data-act="download-script" data-id="${esc(job.id)}">Skript herunterladen</button>
      </div>
      <p class="hint">Für User Scripts. Das Skript startet denselben Abgleich mit rclone und der Datei auf dem Unraid-Stick.</p>
    </article>`;
  }).join("");
  appEl.innerHTML = `${banners()}
    <div class="row" style="margin-bottom:.8rem">
      <button type="button" class="primary" data-go="#/new">Auftrag anlegen</button>
      <button type="button" data-act="pause">${settings.schedules_paused ? "Zeitpläne fortsetzen" : "Alle Zeitpläne pausieren"}</button>
    </div>
    <div class="stack">${cards || `<div class="card"><p>Noch kein Auftrag. Eine Unraid-Quelle, dann WebDAV und/oder SMB als Ziel.</p></div>`}</div>`;
}

function fieldSelect(name, options, selected) {
  return `<select name="${name}">${options.map(([value, label]) =>
    `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
}

function renderEdit(id) {
  const existing = id ? jobById(id) : null;
  if (id && !existing) {
    appEl.innerHTML = `<div class="card"><p>Auftrag nicht gefunden.</p></div>`;
    return;
  }
  const job = existing ? structuredClone(existing) : structuredClone(state.blank);
  const schedule = job.schedule || state.blank.schedule;
  const maps = job.mappings.length ? job.mappings : [{ local: "", remote: "", synology: "" }];
  const shareOptions = (state.synology?.shares || []).map((item) =>
    `<option value="${esc(item.id)}" ${item.id === (job.synology_share || "") ? "selected" : ""}>${esc(item.source || item.id)}${item.mounted ? "" : " (ausgehängt)"}</option>`
  ).join("");
  appEl.innerHTML = `<form id="job-form" class="editor" data-id="${esc(id || "")}">
    <div class="stack">
      <section class="card form">
        <label>Name<input name="name" required maxlength="80" value="${esc(job.name)}" /></label>
        <label class="inline"><input type="checkbox" name="enabled" ${job.enabled ? "checked" : ""} /> Zeitplan für diesen Auftrag aktiv</label>
      </section>
      <section class="card form source-panel">
        <div class="split">
          <div>
            <h2 class="panel-title">Quelle</h2>
            <p class="hint">Dieselbe Unraid-Quelle gilt für beide Ziele.</p>
          </div>
          <button type="button" data-act="add-map">Quelle hinzufügen</button>
        </div>
        <div id="sources">${maps.map((item, index) => sourceRow(item, index)).join("")}</div>
        <div id="confirm-root-field">
          <label class="inline check"><input type="checkbox" name="confirm_root" ${job.confirm_root ? "checked" : ""} /> Gesamten gemounteten Inhalt synchronisieren</label>
        </div>
      </section>
      <div class="target-grid">
        <section class="card form target-card" data-target="kdrive" id="target-kdrive">
          <div class="target-head">
            <div>
              <h2 class="panel-title">WebDAV</h2>
              <p class="hint" data-webdav-hint>Beliebiges WebDAV-Ziel (Infomaniak, OpenCloud, …)</p>
            </div>
            <label class="switch">
              <input type="checkbox" name="kdrive_enabled" ${job.kdrive_enabled !== false ? "checked" : ""} />
              <span class="switch-ui"></span>
              <span class="switch-label">Aktiv</span>
            </label>
          </div>
          <div class="target-body" data-target-body="kdrive">
            ${webdavSelectHtml(job.rclone_remote || "")}
            <label>Art${fieldSelect("kdrive_mode", Object.entries(MODE), job.kdrive_mode || job.mode || "upload")}</label>
            <div data-kdrive-existing>
              <label>Vorhandene Dateien${fieldSelect("kdrive_existing", [
                ["newer", "Nur neuere Quelle übernehmen"],
                ["overwrite", "Abweichende überschreiben"],
                ["skip", "Vorhandene überspringen"],
                ["version", "Alte Fassung in den Versionsordner, dann überschreiben"],
              ], job.kdrive_existing || job.existing || "newer")}</label>
            </div>
            <div data-kdrive-conflict>
              <label>Konflikt${fieldSelect("kdrive_conflict", [
                ["newer", "Neuere Datei gewinnt"],
                ["larger", "Grössere Datei gewinnt"],
                ["local", "Unraid gewinnt"],
                ["remote", "WebDAV gewinnt"],
                ["both", "Beide Fassungen behalten"],
              ], job.kdrive_conflict || job.conflict || "newer")}</label>
            </div>
            <div data-kdrive-keep-version>
              <label class="inline check"><input type="checkbox" name="kdrive_keep_version" ${(job.kdrive_existing || job.existing) === "version" ? "checked" : ""} /> Ersetzte Dateien im Versionsordner behalten</label>
            </div>
            <div data-kdrive-confirm-delete>
              <label class="inline check"><input type="checkbox" name="kdrive_confirm_delete" ${job.confirm_delete ? "checked" : ""} /> <span data-webdav-delete-label>Löschungen für WebDAV bestätigen</span></label>
              <p class="hint" data-kdrive-delete-hint></p>
            </div>
            <div id="kdrive-dests">${maps.map((item, index) => kdriveDestRow(item, index)).join("")}</div>
            <p class="hint">Zielordner darf anders heissen als die Quelle.</p>
          </div>
        </section>
        <section class="card form target-card" data-target="synology" id="target-synology">
          <div class="target-head">
            <div>
              <h2 class="panel-title">SMB</h2>
              <p class="hint">Unassigned Devices / Synology</p>
            </div>
            <label class="switch">
              <input type="checkbox" name="synology_enabled" ${job.synology_enabled ? "checked" : ""} />
              <span class="switch-ui"></span>
              <span class="switch-label">Aktiv</span>
            </label>
          </div>
          <div class="target-body" data-target-body="synology">
            <label>SMB-Share<select name="synology_share">
              <option value="">Share wählen</option>
              ${shareOptions}
            </select></label>
            <label>Art${fieldSelect("synology_mode", Object.entries(MODE).filter(([value]) => value !== "bisync"), job.synology_mode || (job.mode === "bisync" ? "upload" : job.mode) || "upload")}</label>
            <label>Vorhandene Dateien${fieldSelect("synology_existing", [
              ["newer", "Nur neuere Quelle übernehmen"],
              ["overwrite", "Abweichende überschreiben"],
              ["skip", "Vorhandene überspringen"],
              ["version", "Alte Fassung in den Versionsordner, dann überschreiben"],
            ], job.synology_existing || job.existing || "newer")}</label>
            <div data-smb-confirm-delete>
              <label class="inline check"><input type="checkbox" name="synology_confirm_delete" ${job.confirm_delete ? "checked" : ""} /> <span data-smb-delete-label>Löschungen für SMB bestätigen</span></label>
              <p class="hint" data-smb-delete-hint></p>
            </div>
            <div id="smb-dests">${maps.map((item, index) => smbDestRow(item, index)).join("")}</div>
            <p class="hint">${state.synology_ok ? "Share zuerst wählen, dann Ordner." : "Path Remotes fehlt noch (Host /mnt/remotes, Slave)."}</p>
          </div>
        </section>
      </div>
      <p class="hint target-note">Mindestens ein Ziel muss aktiv sein. Zeitplan gilt für beide Ziele gemeinsam.</p>
      <section class="card form">
        <h2 class="panel-title">Zeitplan</h2>
        <label>Zeitplan${fieldSelect("schedule_type", [
          ["manual", "Nur manuell"],
          ["interval", "Intervall"],
          ["daily", "Täglich"],
          ["weekly", "Wöchentlich"],
          ["cron", "Cron"],
        ], schedule.type)}</label>
        <div id="time-field"><label>Uhrzeit<input type="time" name="time" value="${esc(schedule.time || "03:00")}" /></label></div>
        <div id="weekday-field"><label>Wochentag${fieldSelect("weekday", WEEKDAYS.map((label, index) => [String(index), label]), String(schedule.weekday || 0))}</label></div>
        <div id="interval-field"><label>Intervall in Minuten<input name="interval_minutes" type="number" min="5" max="10080" value="${esc(schedule.interval_minutes || 60)}" /></label></div>
        <div id="cron-field"><label>Cron<input name="cron" value="${esc(schedule.cron || "0 3 * * *")}" spellcheck="false" /><span class="hint">5 Felder: Minute Stunde Tag Monat Wochentag. Beispiel 30 3 * * * ist täglich 03:30.</span></label></div>
      </section>
    </div>
    <div class="stack">
      <section class="card form">
        <h2>Vorschau</h2>
        <p id="preview" class="meta">…</p>
        <pre id="commands" class="preview"></pre>
        <div class="row">
          <button type="submit" class="primary">Speichern</button>
          <button type="button" data-go="#/">Abbrechen</button>
          ${id ? `<button type="button" class="danger" data-act="delete" data-id="${esc(id)}">Löschen</button>` : ""}
        </div>
      </section>
      <details class="card form">
        <summary>Weitere Optionen</summary>
        <label class="inline check"><input type="checkbox" name="exclude_system" ${job.exclude_system ? "checked" : ""} /> Systemdateien auslassen (@eaDir, .DS_Store, temporäre Dateien, appdata dieses Containers)</label>
        <label>Eigene Filter<textarea name="excludes" spellcheck="false" placeholder="*.bak&#10;/temp/**">${esc(job.excludes || "")}</textarea><span class="hint">Eine Regel pro Zeile. Ohne Vorzeichen wird ausgeschlossen. Eine Zeile mit + schliesst ein. Ausgeschlossene Dateien werden nicht gelöscht.</span></label>
        <label>Vergleich${fieldSelect("compare", [
          ["modtime", "Grösse und Änderungszeit"],
          ["checksum", "Prüfsumme"],
          ["size", "Nur Grösse"],
        ], job.compare)}</label>
        <label>Mindestalter<input name="min_age" value="${esc(job.min_age || "")}" placeholder="1m" /><span class="hint">Dateien, die gerade geschrieben werden, auslassen. Leer = sofort. Beispiele: 30s, 1m, 2h.</span></label>
        <label>Maximale Dateigrösse<input name="max_size" value="${esc(job.max_size || "")}" placeholder="5G" /></label>
        <label>Bandbreitenlimit<input name="bandwidth" value="${esc(job.bandwidth || "")}" placeholder="10M" /><span class="hint">rclone-Syntax, zum Beispiel 10M oder 08:00,2M 23:00,off.</span></label>
        <label>Parallele Übertragungen<input name="transfers" type="number" min="1" max="16" value="${esc(job.transfers || 4)}" /></label>
        <label>Versionsordner am WebDAV-Ziel<input name="version_dir" value="${esc(job.version_dir || "_Versionen")}" /></label>
        <label>Versionen behalten (Tage, 0 = unbegrenzt)<input name="version_keep_days" type="number" min="0" max="3650" value="${esc(job.version_keep_days ?? 30)}" /></label>
        <div id="mirror-extra">
          <label>Löschen beim Angleichen${fieldSelect("delete_mode", [
            ["after", "Nach der Übertragung"],
            ["during", "Während der Übertragung"],
            ["before", "Vor der Übertragung"],
          ], job.delete_mode || "after")}</label>
          <label class="inline check"><input type="checkbox" name="track_renames" ${job.track_renames ? "checked" : ""} /> Umbenennungen erkennen (deutlich langsamer)</label>
        </div>
        <label>Löschgrenze beidseitig (%)<input name="max_delete" type="number" min="1" max="100" value="${esc(job.max_delete || 50)}" /><span class="hint">Der beidseitige Lauf bricht ab, wenn mehr als dieser Anteil gelöscht würde.</span></label>
        <label class="inline check"><input type="checkbox" name="create_empty_dirs" ${job.create_empty_dirs ? "checked" : ""} /> Leere Ordner anlegen</label>
        <label class="inline check"><input type="checkbox" name="copy_links" ${job.copy_links ? "checked" : ""} /> Symlinks als Dateien kopieren</label>
        <label class="inline check"><input type="checkbox" name="verbose" ${job.verbose ? "checked" : ""} /> Jede Datei im Protokoll aufführen</label>
      </details>
    </div>
  </form>`;
  dirty = false;
  syncVisibility();
  schedulePreview();
}

function remoteByName(name) {
  return (state.connection?.remotes || []).find((item) => item.name === name) || null;
}

function effectiveWebdav(jobOrName) {
  const name = typeof jobOrName === "string"
    ? jobOrName
    : (jobOrName?.rclone_remote || state.connection?.rclone_remote || "");
  const remote = remoteByName(name) || remoteByName(state.connection?.rclone_remote || "");
  return {
    name: remote?.name || name || "WebDAV",
    is_kdrive: remote ? !!remote.is_kdrive : false,
    url: remote?.url || "",
    remote,
  };
}

function webdavSelectHtml(selected) {
  const remotes = state.connection?.remotes || [];
  const globalRemote = state.connection?.rclone_remote || "";
  const chosen = selected || globalRemote || "";
  if (!remotes.length) {
    return `<p class="hint">Keine WebDAV-Ziele. Unter <a href="#/settings">Einstellungen</a> anlegen oder die rclone-Datei einbinden.</p>`;
  }
  const options = remotes.map((item) => {
    const kind = item.is_kdrive ? "Infomaniak" : "WebDAV";
    const label = item.url ? `${item.name} · ${kind} · ${item.url}` : `${item.name} · ${kind}`;
    return `<option value="${esc(item.name)}" ${item.name === chosen ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");
  return `<label>WebDAV-Ziel<select name="rclone_remote">
    <option value="">Standard aus Einstellungen${globalRemote ? ` (${esc(globalRemote)})` : ""}</option>
    ${options}
  </select></label>
  <p class="hint">Infomaniak-kDrive nur, wenn das Ziel eine kDrive-Adresse hat. Andere WebDAV-Ziele laufen rein als WebDAV.</p>`;
}

function sourceLabel(local) {
  const value = (local || "").trim();
  return value || "Quelle";
}

function sourceRow(item, index) {
  return `<div class="source-row" data-map-index="${index}">
    <label>Unraid-Ordner<input name="local" value="${esc(item.local || "")}" spellcheck="false" placeholder="photos oder photos/2024" data-sync-source /></label>
    <div class="row map-actions">
      <button type="button" data-act="browse" data-field="local">Unraid wählen</button>
      <button type="button" data-act="same-path">Gleicher Pfad in beide Ziele</button>
      <button type="button" data-act="base-name">Nur Ordnername in beide Ziele</button>
      <button type="button" data-act="remove-map">Entfernen</button>
    </div>
  </div>`;
}

function kdriveDestRow(item, index) {
  return `<div class="dest-row" data-map-index="${index}">
    <div class="dest-from"><span class="dest-tag">von</span> <strong class="source-ref">${esc(sourceLabel(item.local))}</strong></div>
    <label><span data-webdav-folder-label>WebDAV-Ordner</span><input name="remote" value="${esc(item.remote || "")}" spellcheck="false" placeholder="Fotos oder Archiv/Fotos" /></label>
    <button type="button" data-act="browse" data-field="remote" data-webdav-browse>Ordner wählen</button>
  </div>`;
}

function smbDestRow(item, index) {
  return `<div class="dest-row" data-map-index="${index}">
    <div class="dest-from"><span class="dest-tag">von</span> <strong class="source-ref">${esc(sourceLabel(item.local))}</strong></div>
    <label>SMB-Ordner<input name="synology" value="${esc(item.synology || "")}" spellcheck="false" placeholder="Backup/Fotos" /></label>
    <button type="button" data-act="browse" data-field="synology">SMB wählen</button>
  </div>`;
}

function reindexMaps() {
  const form = document.getElementById("job-form");
  if (!form) return;
  [...form.querySelectorAll(".source-row")].forEach((row, index) => { row.dataset.mapIndex = String(index); });
  [...form.querySelectorAll("#kdrive-dests .dest-row")].forEach((row, index) => { row.dataset.mapIndex = String(index); });
  [...form.querySelectorAll("#smb-dests .dest-row")].forEach((row, index) => { row.dataset.mapIndex = String(index); });
  syncSourceLabels();
}

function syncSourceLabels() {
  const form = document.getElementById("job-form");
  if (!form) return;
  const locals = [...form.querySelectorAll(".source-row [name=local]")];
  locals.forEach((input, index) => {
    const label = sourceLabel(input.value);
    form.querySelectorAll(`.dest-row[data-map-index="${index}"] .source-ref`).forEach((el) => {
      el.textContent = label;
    });
  });
}

function isMirrorMode(mode) {
  return mode === "mirror_up" || mode === "mirror_down";
}

function isMoveMode(mode) {
  return mode === "move";
}

function needsDeleteConfirm(mode) {
  return isMirrorMode(mode) || isMoveMode(mode);
}

function syncVisibility() {
  const form = document.getElementById("job-form");
  if (!form) return;
  const field = (name) => form.querySelector(`[name="${name}"]`);
  const kdriveOn = field("kdrive_enabled").checked;
  const synOn = field("synology_enabled").checked;
  const kdMode = field("kdrive_mode")?.value || "upload";
  const synMode = field("synology_mode")?.value || "upload";
  const kdBisync = kdMode === "bisync";
  const kdMirror = isMirrorMode(kdMode);
  const synMirror = isMirrorMode(synMode);
  const kdConfirm = needsDeleteConfirm(kdMode);
  const synConfirm = needsDeleteConfirm(synMode);
  form.querySelector("[data-kdrive-existing]")?.toggleAttribute("hidden", kdBisync);
  form.querySelector("[data-kdrive-conflict]")?.toggleAttribute("hidden", !kdBisync);
  form.querySelector("[data-kdrive-keep-version]")?.toggleAttribute("hidden", !kdBisync);
  form.querySelector("[data-kdrive-confirm-delete]")?.toggleAttribute("hidden", !kdConfirm);
  form.querySelector("[data-smb-confirm-delete]")?.toggleAttribute("hidden", !synConfirm);
  const webdav = effectiveWebdav(field("rclone_remote")?.value || "");
  const peer = webdav.is_kdrive ? "kDrive" : (webdav.name || "WebDAV");
  const hint = form.querySelector("[data-webdav-hint]");
  if (hint) {
    hint.textContent = webdav.is_kdrive
      ? `Infomaniak kDrive (${webdav.name})`
      : (webdav.remote ? `${webdav.name} · reines WebDAV` : "Beliebiges WebDAV-Ziel");
  }
  form.querySelectorAll("[data-webdav-folder-label]").forEach((el) => {
    el.textContent = webdav.is_kdrive ? "kDrive-Ordner" : "WebDAV-Ordner";
  });
  form.querySelectorAll("[data-webdav-browse]").forEach((el) => {
    el.textContent = webdav.is_kdrive ? "kDrive wählen" : "Ordner wählen";
  });
  const conflictRemote = form.querySelector("[name=kdrive_conflict] option[value=remote]");
  if (conflictRemote) conflictRemote.textContent = `${peer} gewinnt`;
  const kdHint = form.querySelector("[data-kdrive-delete-hint]");
  if (kdHint) {
    if (isMoveMode(kdMode)) {
      kdHint.textContent = `Nach erfolgreichem Übertragen nach ${peer} werden die Quelldateien auf Unraid gelöscht.`;
    } else {
      kdHint.textContent = kdMode === "mirror_down"
        ? `Dateien auf Unraid, die im ${peer}-Ordner fehlen, werden gelöscht.`
        : `Dateien auf ${peer}, die im Unraid-Ordner fehlen, werden gelöscht.`;
    }
  }
  const synHint = form.querySelector("[data-smb-delete-hint]");
  if (synHint) {
    if (isMoveMode(synMode)) {
      synHint.textContent = "Nach erfolgreichem Übertragen nach SMB werden die Quelldateien auf Unraid gelöscht.";
    } else {
      synHint.textContent = synMode === "mirror_down"
        ? "Dateien auf Unraid, die im SMB-Ordner fehlen, werden gelöscht."
        : "Dateien auf SMB, die im Unraid-Ordner fehlen, werden gelöscht.";
    }
  }
  const kdDeleteLabel = form.querySelector("[data-webdav-delete-label]");
  if (kdDeleteLabel) {
    kdDeleteLabel.textContent = isMoveMode(kdMode)
      ? "Löschen der Unraid-Quelldateien bestätigen"
      : `Löschungen für ${peer} bestätigen`;
  }
  const synDeleteLabel = form.querySelector("[data-smb-delete-label]");
  if (synDeleteLabel) {
    synDeleteLabel.textContent = isMoveMode(synMode)
      ? "Löschen der Unraid-Quelldateien bestätigen"
      : "Löschungen für SMB bestätigen";
  }
  document.getElementById("mirror-extra").hidden = !(kdMirror || synMirror);
  const kind = field("schedule_type").value;
  document.getElementById("time-field").hidden = kind !== "daily" && kind !== "weekly";
  document.getElementById("weekday-field").hidden = kind !== "weekly";
  document.getElementById("interval-field").hidden = kind !== "interval";
  document.getElementById("cron-field").hidden = kind !== "cron";
  const root = [...form.querySelectorAll(".source-row [name=local]")].some((input) => input.value.trim() === "");
  document.getElementById("confirm-root-field").hidden = !root;
  form.querySelector("#target-kdrive")?.classList.toggle("is-off", !kdriveOn);
  form.querySelector("#target-synology")?.classList.toggle("is-off", !synOn);
  form.querySelectorAll("[data-target-body=kdrive]").forEach((el) => { el.hidden = !kdriveOn; });
  form.querySelectorAll("[data-target-body=synology]").forEach((el) => { el.hidden = !synOn; });
  syncSourceLabels();
}

function readJob() {
  const form = document.getElementById("job-form");
  const field = (name) => form.querySelector(`[name="${name}"]`);
  const kdriveOn = field("kdrive_enabled").checked;
  const synOn = field("synology_enabled").checked;
  const kdMode = field("kdrive_mode")?.value || "upload";
  const synMode = field("synology_mode")?.value || "upload";
  const keepVersion = field("kdrive_keep_version")?.checked;
  const kdExisting = kdMode === "bisync" ? (keepVersion ? "version" : "overwrite") : (field("kdrive_existing")?.value || "newer");
  const synExisting = field("synology_existing")?.value || "newer";
  let confirmDelete = true;
  if (kdriveOn && needsDeleteConfirm(kdMode)) confirmDelete = confirmDelete && !!field("kdrive_confirm_delete")?.checked;
  if (synOn && needsDeleteConfirm(synMode)) confirmDelete = confirmDelete && !!field("synology_confirm_delete")?.checked;
  const id = form.dataset.id;
  const locals = [...form.querySelectorAll(".source-row [name=local]")];
  const remotes = [...form.querySelectorAll("#kdrive-dests [name=remote]")];
  const syns = [...form.querySelectorAll("#smb-dests [name=synology]")];
  const job = {
    name: field("name").value.trim(),
    enabled: field("enabled").checked,
    kdrive_enabled: kdriveOn,
    synology_enabled: synOn,
    synology_share: field("synology_share")?.value || "",
    rclone_remote: field("rclone_remote")?.value || "",
    mappings: locals.map((input, index) => ({
      local: input.value.trim(),
      remote: remotes[index]?.value.trim() || "",
      synology: syns[index]?.value.trim() || "",
    })),
    mode: kdriveOn ? kdMode : synMode,
    existing: kdriveOn ? kdExisting : synExisting,
    conflict: field("kdrive_conflict")?.value || "newer",
    kdrive_mode: kdMode,
    kdrive_existing: kdExisting,
    kdrive_conflict: field("kdrive_conflict")?.value || "newer",
    synology_mode: synMode,
    synology_existing: synExisting,
    confirm_delete: confirmDelete,
    confirm_root: field("confirm_root").checked,
    schedule: {
      type: field("schedule_type").value,
      time: field("time").value || "03:00",
      weekday: Number(field("weekday").value || 0),
      interval_minutes: Number(field("interval_minutes").value || 60),
      cron: field("cron").value.trim(),
    },
    exclude_system: field("exclude_system").checked,
    excludes: field("excludes").value,
    compare: field("compare").value,
    min_age: field("min_age").value.trim(),
    max_size: field("max_size").value.trim(),
    bandwidth: field("bandwidth").value.trim(),
    transfers: Number(field("transfers").value || 4),
    version_dir: field("version_dir").value.trim(),
    version_keep_days: Number(field("version_keep_days").value || 0),
    delete_mode: field("delete_mode").value,
    track_renames: field("track_renames").checked,
    max_delete: Number(field("max_delete").value || 50),
    create_empty_dirs: field("create_empty_dirs").checked,
    copy_links: field("copy_links").checked,
    verbose: field("verbose").checked,
  };
  if (id) job.id = id;
  return job;
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 250);
}

async function updatePreview() {
  const preview = document.getElementById("preview");
  const commands = document.getElementById("commands");
  if (!preview) return;
  try {
    const data = await api("/api/preview", { method: "POST", body: JSON.stringify({ job: readJob() }) });
    preview.textContent = data.description;
    commands.textContent = (data.commands || []).join("\n\n");
  } catch (error) {
    preview.textContent = error.message;
    commands.textContent = "";
  }
}

function webdavManageRows(remotes, selected) {
  if (!remotes.length) {
    return `<div class="banner warn"><p>Noch keine WebDAV-Ziele. Unten eines anlegen, oder die rclone-Plugin-Datei einbinden und aktualisieren.</p></div>`;
  }
  const rows = remotes.map((item) => {
    const kind = item.is_kdrive ? "Infomaniak" : "WebDAV";
    const origin = item.managed ? "hier angelegt" : "rclone-Datei";
    return `<tr class="share-row ${item.name === selected ? "selected" : ""}">
      <td class="share-pick"><input type="radio" name="rclone_remote" value="${esc(item.name)}" ${item.name === selected ? "checked" : ""} data-act="pick-webdav" data-id="${esc(item.name)}" aria-label="${esc(item.name)}" /></td>
      <td><strong>${esc(item.name)}</strong><div class="meta">${esc(kind)} · ${esc(origin)}</div></td>
      <td><span class="path">${esc(item.url || "—")}</span><div class="meta">${esc(item.user || "")}</div></td>
      <td class="row" style="gap:.35rem;flex-wrap:wrap">
        <button type="button" data-act="edit-webdav" data-id="${esc(item.name)}">Bearbeiten</button>
        <button type="button" data-act="test-webdav" data-id="${esc(item.name)}">Test</button>
        ${item.managed ? `<button type="button" class="danger" data-act="delete-webdav" data-id="${esc(item.name)}">Löschen</button>` : ""}
      </td>
    </tr>`;
  }).join("");
  return `<div class="plan-wrap"><table class="share-table">
    <thead><tr><th></th><th>Name</th><th>Adresse</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function renderSettings() {
  const connection = state.connection;
  const synology = state.synology || {};
  const remotes = connection.remotes || [];
  const selected = connection.rclone_remote && remotes.some((item) => item.name === connection.rclone_remote)
    ? connection.rclone_remote
    : "";
  const fileState = connection.rclone_present
    ? `rclone-Datei${connection.rclone_source ? ` (${connection.rclone_source})` : ""} · ${remotes.length} Ziel${remotes.length === 1 ? "" : "e"}`
    : `Keine rclone-Plugin-Datei. Ziele können hier angelegt werden. Optional: ${connection.example_path}`;
  const shares = synology.shares || [];
  const shareRows = shares.map((item) => `<tr>
    <td><span class="path">${esc(item.source || "—")}</span></td>
    <td><span class="path">${esc(item.id)}</span></td>
    <td><span class="dot ${item.mounted ? "on" : "off"}" title="${item.mounted ? "eingehängt" : "ausgehängt"}"></span>
      <span class="meta">${item.mounted ? "eingehängt" : "ausgehängt"}</span></td>
  </tr>`).join("");
  const remotesOk = synology.remotes_ok;
  const containerRemotes = synology.remotes_root || "/synology";
  const diag = `<p class="meta">Container prüft: <code>${esc(containerRemotes)}</code> · ${remotesOk ? "Ordner vorhanden" : "Ordner fehlt"} · ${synology.dir_count || 0} Unterordner${synology.list_error ? ` · Fehler: ${esc(synology.list_error)}` : ""}</p>`;
  const synBody = shares.length
    ? `<div class="plan-wrap"><table class="share-table">
        <thead><tr><th>Quelle</th><th>Mountpoint</th><th>Status</th></tr></thead>
        <tbody>${shareRows}</tbody>
      </table></div>
      <p class="hint">Den Share wählst du im jeweiligen Auftrag unter SMB.</p>`
    : remotesOk
      ? `<div class="banner warn">
          <p>Der Path ist eingebunden, aber leer. Unter Unassigned Devices einen SMB-Share <strong>einhängen</strong>. Access Mode am Path Remotes auf <strong>Read/Write/Slave</strong> setzen, dann Apply.</p>
        </div>`
      : `<div class="banner warn">
        <p><strong>Im Container fehlt <code>${esc(containerRemotes)}</code>.</strong> Der Host-Pfad allein reicht nicht — prüfe die drei Felder:</p>
        <ol class="hint" style="margin:.4rem 0 0 1.1rem;padding:0">
          <li><strong>Name:</strong> Remotes</li>
          <li><strong>Container-Pfad:</strong> <code>/synology</code> (genau so, nicht /mnt/remotes)</li>
          <li><strong>Host-Pfad:</strong> <code>/mnt/remotes</code></li>
          <li>Access Mode: <strong>Read/Write/Slave</strong></li>
          <li><strong>Apply</strong>, Seite neu laden</li>
        </ol>
      </div>`;
  appEl.innerHTML = `${banners()}
    <div class="settings-stack">
      <section class="card form" id="webdav-manage">
        <div class="split">
          <div>
            <h2>WebDAV-Ziele</h2>
            <p class="hint">Vorhandene Ziele anzeigen, neu anlegen oder neu konfigurieren. Infomaniak nur bei kDrive-Adresse; sonst reines WebDAV.</p>
          </div>
          <button type="button" data-act="new-webdav">Neues Ziel</button>
        </div>
        <p class="meta">${esc(fileState)}</p>
        ${webdavManageRows(remotes, selected)}
        <p class="meta" id="rclone-meta"></p>
        <div class="row">
          <button type="button" class="primary" data-act="save-default-webdav">Als Standard speichern</button>
          <button type="button" data-act="refresh-remotes">Aus rclone-Datei aktualisieren</button>
          <button type="button" data-act="test-connection">Standard testen</button>
          ${state.env_ready ? `<button type="button" data-act="reset-env">Auf Container-Parameter zurücksetzen</button>` : ""}
        </div>
        <pre id="test-output" class="preview" hidden></pre>
        <form id="webdav-form" class="form" hidden>
          <h3 id="webdav-form-title">Neues WebDAV-Ziel</h3>
          <input type="hidden" name="original_name" value="" />
          <label>Name<input name="name" required maxlength="80" placeholder="OpenCloud oder Infomaniak" spellcheck="false" /></label>
          <label>WebDAV-URL<input name="url" required placeholder="https://cloud.example.com/remote.php/dav/files/user" spellcheck="false" /></label>
          <p class="hint">Infomaniak: <code>https://DEINE-ID.connect.kdrive.infomaniak.com</code> oder nur die Zahlen-ID.</p>
          <label>Benutzer<input name="user" required autocomplete="username" placeholder="E-Mail oder Benutzername" /></label>
          <label>Passwort<input name="password" type="password" autocomplete="new-password" placeholder="leer lassen = bestehendes behalten" /></label>
          <label class="inline check"><input type="checkbox" name="password_obscured" /> Wert ist schon rclone-pass (obscured), kein Klartext</label>
          <label>Vendor<input name="vendor" value="other" placeholder="other" /></label>
          <div class="row">
            <button type="submit" class="primary">Ziel speichern</button>
            <button type="button" data-act="cancel-webdav">Abbrechen</button>
          </div>
        </form>
      </section>
      <section class="card form">
        <h2>SMB Shares</h2>
        <p class="hint">Verfügbare Remotes wie unter Unassigned Devices. Pro Sync-Auftrag wählst du den Share einzeln.</p>
        ${diag}
        ${synBody}
      </section>
      <form id="settings-form" class="card form">
        <h2>Allgemein</h2>
        <label class="inline check"><input type="checkbox" name="schedules_paused" ${state.settings.schedules_paused ? "checked" : ""} /> Alle Zeitpläne pausieren. Manuelle Läufe bleiben möglich.</label>
        <label>Gleichzeitige Aufträge<input name="max_parallel" type="number" min="1" max="3" value="${esc(state.settings.max_parallel)}" /></label>
        <button type="submit" class="primary">Allgemein speichern</button>
      </form>
    </div>`;
  syncWebdavMeta();
}

function syncWebdavMeta() {
  const meta = document.getElementById("rclone-meta");
  if (!meta) return;
  const chosen = document.querySelector("#webdav-manage [name=rclone_remote]:checked");
  const remote = remoteByName(chosen?.value || "");
  if (!remote) {
    meta.textContent = "Standard-Ziel in der Liste wählen.";
    return;
  }
  const kind = remote.is_kdrive ? "Infomaniak kDrive" : "reines WebDAV";
  meta.textContent = `Standard: ${remote.name} · ${kind} · ${remote.url || "—"}`;
}

function showWebdavForm(remote) {
  const form = document.getElementById("webdav-form");
  if (!form) return;
  form.hidden = false;
  form.original_name.value = remote?.name || "";
  form.name.value = remote?.name || "";
  form.url.value = remote?.url || "";
  form.user.value = remote?.user || "";
  form.password.value = "";
  form.password_obscured.checked = false;
  form.vendor.value = remote?.vendor || "other";
  document.getElementById("webdav-form-title").textContent = remote
    ? `WebDAV-Ziel «${remote.name}» bearbeiten`
    : "Neues WebDAV-Ziel";
  form.name.focus();
}


function renderHistory() {
  const rows = state.runs.map((run) => `<tr class="clickable" data-go="#/run/${esc(run.id)}">
    <td>${esc(fmtTime(run.started_at))}</td>
    <td>${esc(run.job_name)}</td>
    <td><span class="pill ${esc(run.status)}">${esc(STATUS[run.status] || run.status)}${run.dry_run ? " · Probelauf" : ""}</span></td>
    <td>${esc(run.trigger === "schedule" ? "Zeitplan" : "manuell")}</td>
    <td>${esc(run.summary || "")}</td>
  </tr>`).join("");
  appEl.innerHTML = `<div class="card"><table>
    <thead><tr><th>Start</th><th>Auftrag</th><th>Status</th><th>Auslöser</th><th>Ergebnis</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="5">Noch keine Läufe.</td></tr>`}</tbody>
  </table></div>`;
}

function renderLog(id) {
  const known = (state.runs || []).find((run) => run.id === id);
  if (document.getElementById("logview")?.dataset.id === id) return;
  logOffset = 0;
  appEl.innerHTML = `<div class="card" id="logview" data-id="${esc(id)}">
    <div class="split"><h2>${esc(known?.job_name || "Protokoll")}</h2><button type="button" data-go="#/history">Zurück</button></div>
    <p class="meta" id="log-status">${esc(known ? (STATUS[known.status] || known.status) : "")}${known?.dry_run ? " · Probelauf, es wird nichts verändert" : ""}</p>
    <div id="plan"></div>
    <details>
      <summary>Protokoll</summary>
      <pre id="logbox" class="preview"></pre>
    </details>
  </div>`;
  if (known?.dry_run) renderPlan([], 0, known.status || "running");
  pollLog(id);
}

async function pollLog(id) {
  if (parseRoute().id !== id || parseRoute().name !== "log") return;
  try {
    const data = await api(`/api/runs/${encodeURIComponent(id)}/log?offset=${logOffset}`);
    const box = document.getElementById("logbox");
    if (!box) return;
    if (data.text) {
      box.textContent += data.text;
      logOffset = data.offset;
      box.scrollTop = box.scrollHeight;
    }
    const status = document.getElementById("log-status");
    if (status) status.textContent = `${STATUS[data.status] || data.status}${data.dry_run ? " · Probelauf, es wird nichts verändert" : ""}`;
    if (data.dry_run) renderPlan(data.plan || [], data.plan_total || 0, data.status);
    if (data.status === "running") setTimeout(() => pollLog(id), 2000);
  } catch (error) {
    showError(error.message);
  }
}

function renderPlan(actions, total, status) {
  const host = document.getElementById("plan");
  if (!host) return;
  const empty = status === "running" ? "Probelauf läuft …" : "Keine Dateien würden sich ändern.";
  const place = { exists: "vorhanden", create: "wird erstellt", overwrite: "wird überschrieben", delete: "wird gelöscht", time: "Zeitstempel" };
  const cell = (path, state) => {
    const tag = place[state] ? `<span class="st-tag">${esc(place[state])}</span>` : "";
    return `<td class="path st-${esc(state || "absent")} ${path ? "" : "empty"}">${tag}${esc(path || "—")}</td>`;
  };
  const body = actions.length ? actions.map((row) => `<tr>
    ${cell(row.local, row.local_state)}
    <td class="act ${esc(row.kind)}">${esc(row.label)}${row.target_label ? `<div class="meta">${esc(row.target_label)}</div>` : ""}</td>
    ${cell(row.remote, row.remote_state)}
  </tr>`).join("") : `<tr><td colspan="3" class="empty">${esc(empty)}</td></tr>`;
  const legend = `<p class="plan-legend"><span class="st-exists">vorhanden</span><span class="st-create">wird erstellt</span><span class="st-overwrite">wird überschrieben</span><span class="st-delete">wird gelöscht</span></p>`;
  const more = total > actions.length ? `<p class="meta">Die ersten ${actions.length} von ${total} Dateien.</p>` : "";
  host.innerHTML = `${actions.length ? legend : ""}<div class="plan-wrap"><table class="plan">
    <thead><tr><th>Unraid</th><th>Vorgang</th><th>Ziel</th></tr></thead>
    <tbody>${body}</tbody>
  </table></div>${more}`;
}

function openModal(html) {
  modalEl.innerHTML = `<div class="dialog">${html}</div>`;
  modalEl.classList.remove("hidden");
}

function closeModal() {
  modalEl.classList.add("hidden");
  modalEl.innerHTML = "";
  browse = null;
}

async function showBrowse(path) {
  const field = browse.field;
  const endpoint = field === "local" ? "/api/local" : (field === "synology" ? "/api/synology" : "/api/remote");
  const title = field === "local" ? "Unraid" : (field === "synology" ? "SMB" : "kDrive");
  const rootName = field === "local" ? "Shares" : title;
  const query = new URLSearchParams({ path: path || "" });
  if (field === "synology") {
    const share = document.querySelector("#job-form [name=synology_share]")?.value || "";
    if (!share) {
      showError("Zuerst einen SMB-Share für diesen Auftrag wählen");
      return;
    }
    query.set("share", share);
  }
  if (field === "remote") {
    const remote = document.querySelector("#job-form [name=rclone_remote]")?.value
      || state.connection?.rclone_remote
      || "";
    if (remote) query.set("rclone_remote", remote);
  }
  try {
    const data = await api(`${endpoint}?${query}`);
    const crumbs = [{ name: rootName, path: "" }];
    if (data.path) {
      const acc = [];
      data.path.split("/").forEach((part) => {
        acc.push(part);
        crumbs.push({ name: part, path: acc.join("/") });
      });
    }
    const dirs = data.dirs.map((dir) =>
      `<button type="button" data-act="enter" data-path="${esc(dir.path)}">${esc(dir.name)}</button>`).join("");
    const canMkdir = field === "remote";
    const rootHint = field === "local"
      ? "Die oberste Ebene sind die Unraid-Shares."
      : (field === "synology"
        ? "Die oberste Ebene ist der gewählte SMB-Share unter /mnt/remotes."
        : "Die oberste WebDAV-Ebene. Bei Infomaniak oft mit Papierkorb — den gewünschten Ordner darin wählen.");
    openModal(`<div class="split"><h2 id="modal-title">${title}</h2><button type="button" data-act="close-modal">Schliessen</button></div>
      <div class="crumbs">${crumbs.map((crumb) => `<button type="button" data-act="enter" data-path="${esc(crumb.path)}">${esc(crumb.name)}</button>`).join("")}</div>
      <div class="dirlist">${dirs || `<p class="hint">Keine Unterordner.</p>`}</div>
      <div class="row">
        <button type="button" class="primary" data-act="choose" data-path="${esc(data.path)}">Diesen Ordner verwenden</button>
      </div>
      ${canMkdir ? `<form id="mkdir-form" class="form" style="margin-top:.8rem"><label>Neuen Ordner hier anlegen<input name="folder" /></label><button type="submit">Anlegen</button></form>` : ""}
      ${data.path ? "" : `<p class="hint">${rootHint}</p>`}`);
    browse.path = data.path;
  } catch (error) {
    openModal(`<h2 id="modal-title">Ordner</h2><p class="flash">${esc(error.message)}</p><button type="button" data-act="close-modal">Schliessen</button>`);
  }
}

async function onClick(event) {
  const goEl = event.target.closest("[data-go]");
  if (goEl && !event.target.closest("#modal")) {
    event.preventDefault();
    go(goEl.dataset.go);
    return;
  }
  const actEl = event.target.closest("[data-act]");
  if (!actEl) return;
  const act = actEl.dataset.act;
  const id = actEl.dataset.id;
  try {
    if (act === "edit") return go(`#/job/${id}`);
    if (act === "run" || act === "dry") {
      const data = await api(`/api/jobs/${id}/run`, { method: "POST", body: JSON.stringify({ dry_run: act === "dry" }) });
      await reload();
      go(`#/run/${data.run.id}`);
      return;
    }
    if (act === "resync") {
      if (!confirm("Erstabgleich starten? Abweichende Dateien werden nach der Konfliktregel überschrieben. Die ersetzte Fassung landet im Versionsordner.")) return;
      const data = await api(`/api/jobs/${id}/resync`, { method: "POST", body: "{}" });
      await reload();
      go(`#/run/${data.run.id}`);
      return;
    }
    if (act === "stop") {
      await api(`/api/jobs/${id}/stop`, { method: "POST", body: "{}" });
      await reload();
      paint();
      return;
    }
    if (act === "toggle") {
      const job = jobById(id);
      await api(`/api/jobs/${id}`, { method: "PUT", body: JSON.stringify({ ...job, enabled: !job.enabled }) });
      await reload();
      paint();
      return;
    }
    if (act === "delete") {
      if (!confirm("Auftrag löschen? Dateien auf Unraid und am Ziel bleiben liegen.")) return;
      await api(`/api/jobs/${id}`, { method: "DELETE" });
      dirty = false;
      await reload();
      go("#/");
      return;
    }
    if (act === "pause") {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ schedules_paused: !state.settings.schedules_paused }),
      });
      await reload();
      paint();
      return;
    }
    if (act === "add-map") {
      const index = document.querySelectorAll(".source-row").length;
      const blank = { local: "", remote: "", synology: "" };
      document.getElementById("sources").insertAdjacentHTML("beforeend", sourceRow(blank, index));
      document.getElementById("kdrive-dests").insertAdjacentHTML("beforeend", kdriveDestRow(blank, index));
      document.getElementById("smb-dests").insertAdjacentHTML("beforeend", smbDestRow(blank, index));
      dirty = true;
      syncVisibility();
      schedulePreview();
      return;
    }
    if (act === "remove-map") {
      const row = actEl.closest(".source-row, .dest-row");
      const index = Number(row?.dataset.mapIndex ?? -1);
      const sources = document.querySelectorAll(".source-row");
      if (index < 0) return;
      if (sources.length === 1) {
        document.querySelector(`.source-row[data-map-index="${index}"] [name=local]`).value = "";
        document.querySelector(`#kdrive-dests .dest-row[data-map-index="${index}"] [name=remote]`).value = "";
        document.querySelector(`#smb-dests .dest-row[data-map-index="${index}"] [name=synology]`).value = "";
      } else {
        document.querySelector(`.source-row[data-map-index="${index}"]`)?.remove();
        document.querySelector(`#kdrive-dests .dest-row[data-map-index="${index}"]`)?.remove();
        document.querySelector(`#smb-dests .dest-row[data-map-index="${index}"]`)?.remove();
        reindexMaps();
      }
      dirty = true;
      syncVisibility();
      schedulePreview();
      return;
    }
    if (act === "same-path" || act === "base-name") {
      const row = actEl.closest(".source-row");
      const index = Number(row?.dataset.mapIndex ?? -1);
      if (index < 0) return;
      const local = row.querySelector("[name=local]").value.trim();
      const value = act === "same-path" ? local : (local.split("/").filter(Boolean).pop() || local);
      const remote = document.querySelector(`#kdrive-dests .dest-row[data-map-index="${index}"] [name=remote]`);
      const syn = document.querySelector(`#smb-dests .dest-row[data-map-index="${index}"] [name=synology]`);
      if (remote) remote.value = value;
      if (syn) syn.value = value;
      dirty = true;
      schedulePreview();
      return;
    }
    if (act === "browse") {
      const host = actEl.closest(".source-row, .dest-row");
      const index = Number(host?.dataset.mapIndex ?? -1);
      const fieldName = actEl.dataset.field;
      let input = null;
      if (fieldName === "local") input = host?.querySelector("[name=local]");
      if (fieldName === "remote") input = document.querySelector(`#kdrive-dests .dest-row[data-map-index="${index}"] [name=remote]`);
      if (fieldName === "synology") input = document.querySelector(`#smb-dests .dest-row[data-map-index="${index}"] [name=synology]`);
      browse = { field: fieldName, input, path: "" };
      await showBrowse((input?.value || "").trim());
      return;
    }
    if (act === "enter") {
      await showBrowse(actEl.dataset.path || "");
      return;
    }
    if (act === "choose") {
      if (browse?.input) {
        browse.input.value = actEl.dataset.path || "";
        dirty = true;
        syncVisibility();
        schedulePreview();
      }
      closeModal();
      return;
    }
    if (act === "close-modal") closeModal();
    if (act === "pick-webdav") {
      const radio = appEl.querySelector(`input[name=rclone_remote][value="${CSS.escape(actEl.dataset.id || "")}"]`);
      if (radio) {
        radio.checked = true;
        appEl.querySelectorAll("#webdav-manage .share-row").forEach((row) => {
          row.classList.toggle("selected", row.querySelector("input")?.value === actEl.dataset.id);
        });
        syncWebdavMeta();
      }
      return;
    }
    if (act === "new-webdav") {
      showWebdavForm(null);
      return;
    }
    if (act === "edit-webdav") {
      showWebdavForm(remoteByName(actEl.dataset.id || ""));
      return;
    }
    if (act === "cancel-webdav") {
      const form = document.getElementById("webdav-form");
      if (form) form.hidden = true;
      return;
    }
    if (act === "delete-webdav") {
      const name = actEl.dataset.id || "";
      if (!name || !confirm(`WebDAV-Ziel «${name}» löschen?`)) return;
      const data = await api(`/api/webdav-remotes/${encodeURIComponent(name)}`, { method: "DELETE" });
      state.connection = data.connection;
      showNote(`«${name}» gelöscht.`);
      paint();
      return;
    }
    if (act === "test-webdav") {
      const name = actEl.dataset.id || "";
      const output = document.getElementById("test-output");
      output.hidden = false;
      output.textContent = `Prüfe ${name} …`;
      try {
        const data = await api("/api/connection/test", {
          method: "POST",
          body: JSON.stringify({ rclone_remote: name }),
        });
        const dirs = (data.directories || []).join("\n") || "(keine Ordner auf der obersten Ebene)";
        output.textContent = `${data.message} (${data.remote || name})\n${dirs}`;
      } catch (error) {
        output.textContent = error.message;
      }
      return;
    }
    if (act === "save-default-webdav") {
      const chosen = document.querySelector("#webdav-manage [name=rclone_remote]:checked")?.value || "";
      if (!chosen) {
        showError("Zuerst ein WebDAV-Ziel in der Liste wählen");
        return;
      }
      const data = await api("/api/connection", {
        method: "POST",
        body: JSON.stringify({ mode: "rclone", rclone_remote: chosen }),
      });
      state.connection = data.connection;
      showNote(`Standard: ${chosen}`);
      paint();
      return;
    }
    if (act === "refresh-remotes") {
      const data = await api("/api/connection/refresh-remotes", { method: "POST", body: "{}" });
      state.connection = data.connection;
      showNote(`${data.count || 0} WebDAV-Ziel${(data.count || 0) === 1 ? "" : "e"} geladen.`);
      paint();
      return;
    }
    if (act === "test-connection") {
      const output = document.getElementById("test-output");
      output.hidden = false;
      output.textContent = "Prüfe …";
      const chosen = document.querySelector("#webdav-manage [name=rclone_remote]:checked")?.value || "";
      try {
        const data = await api("/api/connection/test", {
          method: "POST",
          body: JSON.stringify(chosen ? { rclone_remote: chosen } : {}),
        });
        const dirs = (data.directories || []).join("\n") || "(keine Ordner auf der obersten Ebene)";
        output.textContent = `${data.message}${data.remote ? ` (${data.remote})` : ""}\n${dirs}`;
      } catch (error) {
        output.textContent = error.message;
      }
      return;
    }
    if (act === "copy-script" || act === "download-script") {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/script`);
      if (act === "download-script") {
        downloadText(data.filename || "sync.sh", data.script);
        showNote("Skript heruntergeladen.");
        return;
      }
      if (await copyText(data.script)) {
        showNote("Skript kopiert. In User Scripts einfügen.");
        return;
      }
      openModal(`<h2 id="modal-title">Sync-Skript</h2><p class="hint">Das Skript ist markiert. Mit Strg+C kopieren und in User Scripts einfügen.</p><textarea class="script-copy" id="script-copy" readonly>${esc(data.script)}</textarea><div class="row"><button type="button" data-act="close-modal">Schliessen</button></div>`);
      const area = document.getElementById("script-copy");
      area.focus();
      area.select();
      return;
    }
    if (act === "reset-env") {
      if (!confirm("Verbindung durch die Container-Parameter ersetzen?")) return;
      await api("/api/connection/reset-env", { method: "POST", body: "{}" });
      await reload();
      paint();
      return;
    }
  } catch (error) {
    showError(error.message);
  }
}

async function onSubmit(event) {
  const form = event.target;
  if (form.id === "mkdir-form") {
    event.preventDefault();
    const name = form.folder.value.trim();
    const base = browse?.path || "";
    const path = [base, name].filter(Boolean).join("/");
    try {
      await api("/api/remote/mkdir", { method: "POST", body: JSON.stringify({ path }) });
      await showBrowse(path);
    } catch (error) {
      showError(error.message);
    }
    return;
  }
  if (form.id === "webdav-form") {
    event.preventDefault();
    const body = {
      name: form.name.value.trim(),
      url: form.url.value.trim(),
      user: form.user.value.trim(),
      password: form.password.value,
      password_obscured: form.password_obscured.checked,
      vendor: form.vendor.value.trim() || "other",
      original_name: form.original_name.value.trim(),
    };
    try {
      const data = await api("/api/webdav-remotes", { method: "POST", body: JSON.stringify(body) });
      state.connection = data.connection;
      showNote(`WebDAV-Ziel «${data.remote.name}» gespeichert.`);
      paint();
    } catch (error) {
      showError(error.message);
    }
    return;
  }
  if (form.id === "settings-form") {
    event.preventDefault();
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          schedules_paused: form.schedules_paused.checked,
          max_parallel: Number(form.max_parallel.value),
        }),
      });
      await reload();
      showNote("Allgemeine Einstellungen gespeichert.");
      paint();
    } catch (error) {
      showError(error.message);
    }
    return;
  }
  if (form.id === "job-form") {
    event.preventDefault();
    const job = readJob();
    try {
      if (job.id) await api(`/api/jobs/${job.id}`, { method: "PUT", body: JSON.stringify(job) });
      else await api("/api/jobs", { method: "POST", body: JSON.stringify(job) });
      dirty = false;
      await reload();
      go("#/");
    } catch (error) {
      showError(error.message);
    }
  }
}

function onInput(event) {
  if (event.target.closest("#job-form")) {
    dirty = true;
    syncVisibility();
    schedulePreview();
  }
  if (event.target.name === "rclone_remote" && event.target.closest("#webdav-manage")) {
    syncWebdavMeta();
  }
}

document.body.addEventListener("click", (event) => {
  const tab = event.target.closest(".tabs button");
  if (tab) {
    event.preventDefault();
    go(tab.dataset.go);
  }
});
appEl.addEventListener("click", onClick);
appEl.addEventListener("submit", onSubmit);
appEl.addEventListener("input", onInput);
modalEl.addEventListener("click", (event) => {
  if (event.target === modalEl) closeModal();
  else onClick(event);
});
modalEl.addEventListener("submit", onSubmit);
window.addEventListener("hashchange", () => { showError(""); paint(); });

reload().then(paint).catch((error) => showError(error.message));
setInterval(async () => {
  const route = parseRoute().name;
  if (route === "edit" || route === "settings" || route === "log") return;
  try {
    await reload();
    paint();
  } catch (error) {
    showError(error.message);
  }
}, 4000);
