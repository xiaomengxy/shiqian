(() => {
  const app = document.querySelector("[data-notes-app]");
  const trashList = document.querySelector("[data-trash-list]");
  const trashDeleteToggle = document.querySelector("[data-trash-delete-toggle]");
  const learningApp = document.querySelector("[data-learning-app]");
  const md = window.markdownit ? window.markdownit({ html: false, linkify: true, breaks: true }) : null;
  const collapsedKey = "shiqian.notes.collapsedDirectories";
  const showDeleteEntrypointsKey = "shiqian.notes.showDeleteEntrypoints";
  const learningStateKey = "shiqian.learning.state";

  function parseJson(id, fallback) {
    const node = document.querySelector(`#${id}`);
    if (!node) return fallback;
    try {
      return JSON.parse(node.textContent || "");
    } catch {
      return fallback;
    }
  }

  let notes = parseJson("notes-data", []);
  let tree = parseJson("tree-data", { root_notes: [], directories: [], total_count: notes.length, visible_count: notes.length });
  let directories = parseJson("directories-data", []);
  let selectedId = notes[0]?.id || null;
  let selectedDirectoryId = notes[0]?.directory_id || "";
  let editingId = null;
  let dragged = null;
  let pointerDrag = null;
  let activeDropTarget = null;
  let suppressClickUntil = 0;
  let contextMenu = null;
  let dialog = null;
  let cancelDialog = null;
  let showDeleteEntrypoints = storedBoolean(showDeleteEntrypointsKey, false);
  let biliState = {
    loggedIn: false,
    user: null,
    folders: [],
    selectedFolder: null,
    videos: [],
    overwriteExisting: false,
    importLog: null,
    importJobId: null,
    importRunning: false,
    pollTimer: null,
    jobPollTimer: null,
  };

  function collapsedDirectories() {
    try {
      return new Set(JSON.parse(localStorage.getItem(collapsedKey) || "[]").map(String));
    } catch {
      return new Set();
    }
  }

  function saveCollapsedDirectories(values) {
    try {
      localStorage.setItem(collapsedKey, JSON.stringify(Array.from(values)));
    } catch {
      // Local storage can be unavailable in restricted browser contexts.
    }
  }

  function storedBoolean(key, fallback) {
    try {
      const value = localStorage.getItem(key);
      if (value === null) return fallback;
      return value === "true";
    } catch {
      return fallback;
    }
  }

  function saveBoolean(key, value) {
    try {
      localStorage.setItem(key, String(Boolean(value)));
    } catch {
      // Local storage can be unavailable in restricted browser contexts.
    }
  }

  function byId(id) {
    return notes.find((note) => Number(note.id) === Number(id)) || null;
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function renderMarkdown(text) {
    if (md) return md.render(text || "");
    return `<p>${escapeHtml(text || "").replace(/\n/g, "<br>")}</p>`;
  }

  function sourceTypeFor(url) {
    return String(url || "").trim() ? "url" : "manual";
  }

  function noteMeta(note) {
    return note.source_url || note.updated_at_display || "未分类";
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  function learningNode(selector) {
    return learningApp?.querySelector(selector) || null;
  }

  function restoreLearningState() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(learningStateKey) || "null");
      if (!saved || typeof saved !== "object") return;
      biliState = {
        ...biliState,
        loggedIn: Boolean(saved.loggedIn),
        user: saved.user || null,
        folders: Array.isArray(saved.folders) ? saved.folders : [],
        selectedFolder: saved.selectedFolder || null,
        videos: Array.isArray(saved.videos) ? saved.videos : [],
        overwriteExisting: Boolean(saved.overwriteExisting),
        importLog: saved.importLog || null,
        importJobId: saved.importJobId || null,
        importRunning: Boolean(saved.importRunning),
        pollTimer: null,
        jobPollTimer: null,
      };
    } catch {
      // Session storage can be unavailable or manually edited.
    }
  }

  function persistLearningState() {
    if (!learningApp) return;
    try {
      sessionStorage.setItem(
        learningStateKey,
        JSON.stringify({
          loggedIn: biliState.loggedIn,
          user: biliState.user,
          folders: biliState.folders,
          selectedFolder: biliState.selectedFolder,
          videos: biliState.videos,
          overwriteExisting: biliState.overwriteExisting,
          importLog: biliState.importLog,
          importJobId: biliState.importJobId,
          importRunning: biliState.importRunning,
        })
      );
    } catch {
      // Session storage can be unavailable in restricted browser contexts.
    }
  }

  function clearLearningState() {
    try {
      sessionStorage.removeItem(learningStateKey);
    } catch {
      // Session storage can be unavailable in restricted browser contexts.
    }
  }

  function renderBiliLogFromState() {
    const node = learningNode("[data-bili-import-log]");
    if (!node || !biliState.importLog) return;
    node.hidden = false;
    node.className = `bili-import-log ${biliState.importLog.tone || ""}`;
    node.innerHTML = biliState.importLog.message || "";
  }

  function renderBiliLogin() {
    const node = learningNode("[data-bili-login-state]");
    if (!node) return;
    if (biliState.loggedIn) {
      node.innerHTML = `
        <div class="bili-user">
          ${biliState.user?.face ? `<img src="${escapeHtml(biliState.user.face)}" alt="">` : `<span class="bili-avatar-fallback">B</span>`}
          <span>
            <strong>${escapeHtml(biliState.user?.uname || "Bilibili 用户")}</strong>
            <small>已登录，可以读取收藏夹。</small>
          </span>
        </div>
        <button class="secondary" type="button" data-bili-logout>退出登录</button>
      `;
      return;
    }
    node.innerHTML = `
      <p class="muted-text">扫码登录后，本地保存会话 Cookie，用于读取你的收藏夹。</p>
      <button type="button" data-bili-start-login>扫码登录</button>
    `;
  }

  function renderBiliFolders() {
    const node = learningNode("[data-bili-folder-list]");
    if (!node) return;
    if (!biliState.folders.length) {
      node.innerHTML = `<p class="muted-text">${biliState.loggedIn ? "点击读取收藏夹。" : "登录后读取收藏夹。"}</p>`;
      return;
    }
    node.innerHTML = biliState.folders
      .map(
        (folder) => `
          <button class="bili-folder ${biliState.selectedFolder?.media_id === folder.media_id ? "active" : ""}" type="button" data-bili-folder="${folder.media_id}">
            <span>${escapeHtml(folder.title)}</span>
            <small>${folder.media_count || 0}</small>
          </button>
        `
      )
      .join("");
  }

  function renderBiliVideos() {
    const title = learningNode("[data-bili-current-folder]");
    if (title) {
      const folderTitle = biliState.selectedFolder?.title || "选择收藏夹";
      title.textContent = biliState.videos.length ? `${folderTitle} (${biliState.videos.length})` : folderTitle;
    }
    const list = learningNode("[data-bili-video-list]");
    if (!list) return;
    if (!biliState.videos.length) {
      list.innerHTML = `<section class="empty-panel compact"><h2>还没有视频</h2><p>选择一个收藏夹后读取视频。</p></section>`;
      return;
    }
    list.innerHTML = biliState.videos
      .map(
        (video, index) => `
          <label class="bili-video">
            <input type="checkbox" data-bili-video-check="${index}" checked>
            <span>
              <strong>${escapeHtml(video.title || video.bvid)}</strong>
              <small>${escapeHtml([video.owner_name, video.needs_regeneration ? "需要重新生成" : video.note_id ? "已生成笔记" : "", biliContentStatus(video)].filter(Boolean).join(" · ") || video.bvid)}</small>
            </span>
          </label>
        `
      )
      .join("");
  }

  function biliContentStatus(item) {
    if (!item?.content_source) return "";
    if (item.content_trust === "stale") return "旧版需重生";
    if (item.content_trust === "untrusted") return "字幕疑似跑题";
    if (item.content_trust === "weak") return "内容较弱";
    if (item.content_source === "subtitle") return "字幕可信";
    if (item.content_source === "ai_summary") return "AI摘要可信";
    if (item.content_source === "basic_info") return "仅基础信息";
    if (String(item.content_source).startsWith("untrusted_")) return "字幕疑似跑题";
    return item.content_source;
  }

  function renderBiliOverwriteToggle() {
    const button = learningNode("[data-bili-overwrite-toggle]");
    if (!button) return;
    button.setAttribute("aria-pressed", biliState.overwriteExisting ? "true" : "false");
    button.title = biliState.overwriteExisting ? "会重新解析并覆盖已存在的 Bilibili 笔记" : "默认跳过已生成笔记的视频";
  }

  function renderBiliImportControls() {
    const importButton = learningNode("[data-bili-import-selected]");
    const cancelButton = learningNode("[data-bili-cancel-import]");
    if (importButton) {
      importButton.disabled = Boolean(biliState.importRunning);
      importButton.textContent = biliState.importRunning ? "生成中" : "生成笔记";
    }
    if (cancelButton) {
      cancelButton.hidden = !biliState.importRunning;
      cancelButton.disabled = false;
    }
  }

  function setBiliLog(message, tone = "") {
    const node = learningNode("[data-bili-import-log]");
    if (!node) return;
    biliState.importLog = { message, tone };
    node.hidden = false;
    node.className = `bili-import-log ${tone}`;
    node.innerHTML = message;
    persistLearningState();
  }

  function biliProgressMarkup(label, current, total, detail = "") {
    const numericTotal = Number(total) || 0;
    const percent = numericTotal ? Math.max(3, Math.min(100, Math.round((current / numericTotal) * 100))) : 18;
    const count = numericTotal ? `${Math.min(current, numericTotal)} / ${numericTotal}` : `${current}`;
    return `
      <div class="bili-progress">
        <div class="bili-progress-head">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(count)}</strong>
        </div>
        <div class="bili-progress-track"><span style="width:${percent}%"></span></div>
        ${detail ? `<p>${escapeHtml(detail)}</p>` : ""}
      </div>
    `;
  }

  async function loadBiliSession() {
    const data = await requestJson("/api/bilibili/session");
    biliState.loggedIn = Boolean(data.logged_in);
    biliState.user = data.user || null;
    renderBiliLogin();
    renderBiliFolders();
    persistLearningState();
  }

  async function startBiliLogin() {
    const node = learningNode("[data-bili-login-state]");
    if (!node) return;
    clearInterval(biliState.pollTimer);
    node.innerHTML = `<p class="muted-text">正在生成二维码...</p>`;
    const data = await requestJson("/api/bilibili/qrcode");
    node.innerHTML = `
      <div class="bili-qr">
        <img src="${escapeHtml(data.qrcode_image_base64)}" alt="Bilibili 登录二维码">
        <p data-bili-qr-status>请用 Bilibili 扫码确认登录。</p>
      </div>
    `;
    biliState.pollTimer = setInterval(() => pollBiliLogin(data.qrcode_key), 1800);
  }

  async function pollBiliLogin(key) {
    const status = learningNode("[data-bili-qr-status]");
    try {
      const data = await requestJson(`/api/bilibili/qrcode/poll/${encodeURIComponent(key)}`);
      if (status) status.textContent = data.message || data.status;
      if (data.status === "confirmed") {
        clearInterval(biliState.pollTimer);
        biliState.loggedIn = true;
        biliState.user = data.user || null;
        renderBiliLogin();
        persistLearningState();
        await loadBiliFolders();
      } else if (data.status === "expired") {
        clearInterval(biliState.pollTimer);
      }
    } catch (error) {
      clearInterval(biliState.pollTimer);
      if (status) status.textContent = String(error.message || error).slice(0, 140);
    }
  }

  async function loadBiliFolders() {
    if (!biliState.loggedIn) await loadBiliSession();
    if (!biliState.loggedIn) {
      await modalMessage("需要登录", "请先扫码登录 Bilibili。");
      return;
    }
    const node = learningNode("[data-bili-folder-list]");
    if (node) node.innerHTML = `<p class="muted-text">正在读取收藏夹...</p>`;
    const data = await requestJson("/api/bilibili/favorites");
    biliState.folders = data.folders || [];
    renderBiliFolders();
    persistLearningState();
  }

  async function loadBiliVideos(mediaId) {
    const folder = biliState.folders.find((item) => String(item.media_id) === String(mediaId));
    biliState.selectedFolder = folder || { media_id: mediaId, title: String(mediaId) };
    biliState.videos = [];
    biliState.importLog = null;
    persistLearningState();
    renderBiliFolders();
    const list = learningNode("[data-bili-video-list]");
    const expectedTotal = Number(biliState.selectedFolder.media_count) || 0;
    if (list) list.innerHTML = biliProgressMarkup("正在读取收藏夹视频", 0, expectedTotal, "逐页读取中...");
    let page = 1;
    let hasMore = true;
    while (hasMore) {
      const data = await requestJson(`/api/bilibili/favorites/${encodeURIComponent(mediaId)}/videos?page=${page}`);
      biliState.videos = [...biliState.videos, ...(data.videos || [])];
      hasMore = Boolean(data.has_more);
      if (list) {
        const total = expectedTotal || biliState.videos.length + (hasMore ? 20 : 0);
        list.innerHTML = biliProgressMarkup("正在读取收藏夹视频", biliState.videos.length, total, hasMore ? `已读取第 ${page} 页` : "读取完成");
      }
      page += 1;
    }
    renderBiliVideos();
    persistLearningState();
  }

  async function importSelectedBiliVideos() {
    if (!biliState.selectedFolder) {
      await modalMessage("未选择收藏夹", "请先选择一个收藏夹。");
      return;
    }
    const selected = Array.from(learningApp.querySelectorAll("[data-bili-video-check]:checked")).map((input) => biliState.videos[Number(input.dataset.biliVideoCheck)]);
    if (!selected.length) {
      await modalMessage("未选择视频", "请至少选择一个视频。");
      return;
    }
    const skippedExisting = biliState.overwriteExisting ? [] : selected.filter((video) => video?.note_id && !video?.needs_regeneration);
    const toImport = biliState.overwriteExisting ? selected : selected.filter((video) => !video?.note_id || video?.needs_regeneration);
    if (!toImport.length) {
      const rows = skippedExisting.map((video) => `<li><strong>已跳过</strong><span>${escapeHtml(video.title || video.bvid || "")}</span><small>已生成的视频默认跳过</small></li>`);
      setBiliLog(`${biliProgressMarkup("没有需要生成的笔记", selected.length, selected.length, "已生成的视频默认跳过。打开“重新生成”后可覆盖更新。")}<ol>${rows.join("")}</ol>`, "done");
      renderBiliVideos();
      persistLearningState();
      return;
    }
    setBiliLog(biliProgressMarkup("正在解析并生成笔记", 0, toImport.length, skippedExisting.length ? `已跳过 ${skippedExisting.length} 个已生成视频，后台任务准备开始...` : "后台任务准备开始..."));
    const data = await requestJson("/api/bilibili/import-jobs", {
      method: "POST",
      body: JSON.stringify({
        media_id: biliState.selectedFolder.media_id,
        folder_title: biliState.selectedFolder.title,
        videos: toImport,
        overwrite: biliState.overwriteExisting,
      }),
    });
    applyBiliImportJob(data.job, { skippedCount: skippedExisting.length });
    startBiliJobPolling();
  }

  function applyBiliImportJob(job, options = {}) {
    if (!job) return;
    const running = ["queued", "running", "canceling"].includes(job.status);
    biliState.importJobId = job.id || biliState.importJobId;
    biliState.importRunning = running;
    const done = Number(job.done) || 0;
    const total = Number(job.total) || 0;
    const statusLabel = job.status === "done" ? "解析完成" : job.status === "failed" ? "解析失败" : job.status === "canceled" ? "已中断" : "正在解析并生成笔记";
    const detail = job.message || job.current_title || "";
    const rows = (job.items || []).map((item) => {
      const label = item.status === "imported" ? "已生成" : item.status === "skipped" ? "已跳过" : "失败";
      return `<li><strong>${escapeHtml(label)}</strong><span>${escapeHtml(item.title || item.bvid || "")}</span><small>${escapeHtml(item.reason || item.error || item.summary_error || item.trust_reason || biliContentStatus(item) || "")}</small></li>`;
    });
    const skipped = options.skippedCount ? `已跳过 ${options.skippedCount} 个已生成视频。` : "";
    const tone = job.status === "done" ? "done" : job.status === "failed" || job.status === "canceled" ? "warn" : "";
    setBiliLog(`${biliProgressMarkup(statusLabel, done, total, [skipped, detail].filter(Boolean).join(" "))}${rows.length ? `<ol>${rows.join("")}</ol>` : ""}`, tone);
    for (const item of job.items || []) {
      const stored = biliState.videos.find((candidate) => candidate.bvid === item.bvid);
      if (stored && item.status === "imported") {
        stored.note_id = item.note_id;
        stored.title = item.title || stored.title;
        stored.content_source = item.content_source;
        stored.content_trust = item.content_trust;
        stored.trust_reason = item.trust_reason;
        stored.needs_regeneration = false;
      }
    }
    renderBiliImportControls();
    renderBiliVideos();
    persistLearningState();
    if (!running) stopBiliJobPolling();
  }

  async function loadCurrentBiliImportJob() {
    try {
      const data = await requestJson("/api/bilibili/import-jobs/current");
      if (data.job) {
        applyBiliImportJob(data.job);
        if (biliState.importRunning) startBiliJobPolling();
      } else {
        biliState.importRunning = false;
        renderBiliImportControls();
      }
    } catch {
      renderBiliImportControls();
    }
  }

  async function pollBiliImportJob() {
    if (!biliState.importJobId) {
      stopBiliJobPolling();
      return;
    }
    try {
      const data = await requestJson(`/api/bilibili/import-jobs/${encodeURIComponent(biliState.importJobId)}`);
      applyBiliImportJob(data.job);
    } catch (error) {
      setBiliLog(biliProgressMarkup("进度读取失败", 0, 0, String(error.message || error).slice(0, 160)), "warn");
      stopBiliJobPolling();
      biliState.importRunning = false;
      renderBiliImportControls();
    }
  }

  function startBiliJobPolling() {
    stopBiliJobPolling();
    if (!biliState.importJobId) return;
    biliState.importRunning = true;
    renderBiliImportControls();
    biliState.jobPollTimer = setInterval(() => {
      pollBiliImportJob();
    }, 1200);
    pollBiliImportJob();
  }

  function stopBiliJobPolling() {
    if (biliState.jobPollTimer) clearInterval(biliState.jobPollTimer);
    biliState.jobPollTimer = null;
  }

  async function cancelBiliImportJob() {
    if (!biliState.importJobId) return;
    const ok = await modalConfirm("中断解析", "会停止后台继续解析。已经生成的笔记会保留，正在处理中的视频可能需要等请求结束后停止。", "中断");
    if (!ok) return;
    const data = await requestJson(`/api/bilibili/import-jobs/${encodeURIComponent(biliState.importJobId)}/cancel`, { method: "POST", body: "{}" });
    applyBiliImportJob(data.job);
  }

  async function reloadNotes(query = app?.querySelector("[data-note-search]")?.value || "") {
    const url = new URL("/api/notes", window.location.origin);
    if (query) url.searchParams.set("query", query);
    const data = await requestJson(url.pathname + url.search);
    notes = data.notes || [];
    tree = data.tree || tree;
    directories = data.directories || data.folders || directories;
    if (!notes.some((note) => note.id === selectedId)) selectedId = notes[0]?.id || null;
    render();
  }

  function noteRow(note, depth) {
    return `
      <div class="tree-note ${note.id === selectedId ? "active" : ""}" role="button" tabindex="0"
        data-note-draggable data-note-id="${note.id}" data-select-note="${note.id}" style="--depth:${depth}">
        <span class="tree-note-text">
          <strong>${escapeHtml(note.title)}</strong>
          <small>${escapeHtml(noteMeta(note))}</small>
        </span>
      </div>
    `;
  }

  function directoryNode(node, depth = 0) {
    const collapsed = collapsedDirectories().has(String(node.id));
    const children = [...(node.notes || []).map((note) => noteRow(note, depth + 1)), ...(node.children || []).map((child) => directoryNode(child, depth + 1))];
    const hasChildren = children.length > 0;
    return `
      <section class="tree-branch ${collapsed ? "collapsed" : ""} ${hasChildren ? "" : "empty"}" data-directory-branch="${node.id}">
        <div class="tree-directory ${String(selectedDirectoryId) === String(node.id) ? "active" : ""}"
          data-directory-draggable data-directory-drop data-directory-id="${node.id}" style="--depth:${depth}">
          <button class="tree-toggle" type="button" data-toggle-directory="${node.id}" aria-label="展开或折叠目录" ${hasChildren ? "" : "disabled"}></button>
          <span class="tree-folder" data-select-directory="${node.id}">
            <span>${escapeHtml(node.name)}</span>
            <small>${node.count}</small>
          </span>
        </div>
        <div class="tree-children">${children.join("")}</div>
      </section>
    `;
  }

  function renderTree() {
    const rootCount = app.querySelector("[data-root-count]");
    if (rootCount) rootCount.textContent = `${tree.root_notes?.length || 0}`;
    const treeNode = app.querySelector("[data-note-tree]");
    if (!treeNode) return;
    const rootNotes = (tree.root_notes || []).map((note) => noteRow(note, 0));
    const branches = (tree.directories || []).map((node) => directoryNode(node, 0));
    const html = [...rootNotes, ...branches].join("");
    treeNode.innerHTML = html || `<div class="tree-empty">没有笔记</div>`;
  }

  function renderReader(note) {
    const empty = app.querySelector("[data-empty-panel]");
    const reader = app.querySelector("[data-reader-panel]");
    const editor = app.querySelector("[data-editor-panel]");
    if (!note) {
      empty.hidden = false;
      reader.hidden = true;
      editor.hidden = true;
      return;
    }
    empty.hidden = true;
    editor.hidden = true;
    reader.hidden = false;
    reader.querySelector("[data-note-title]").textContent = note.title;
    reader.querySelector("[data-note-rendered]").innerHTML = renderMarkdown(note.body_md);
    reader.querySelector("[data-note-updated]").textContent = `更新于 ${note.updated_at_display || ""}`;
    reader.querySelector("[data-note-source-label]").textContent = `${note.folder_path || "未分类"} · ${note.source_type || "manual"}`;
    const source = reader.querySelector("[data-note-source]");
    if (note.source_url) {
      source.hidden = false;
      source.href = note.source_url;
      source.textContent = note.source_url;
    } else {
      source.hidden = true;
      source.removeAttribute("href");
      source.textContent = "";
    }
    const deleteButton = reader.querySelector("[data-delete-note]");
    if (deleteButton) deleteButton.hidden = !showDeleteEntrypoints;
  }

  function renderEditor(note = null) {
    const empty = app.querySelector("[data-empty-panel]");
    const reader = app.querySelector("[data-reader-panel]");
    const editor = app.querySelector("[data-editor-panel]");
    const form = app.querySelector("[data-note-form]");
    editingId = note?.id || null;
    empty.hidden = true;
    reader.hidden = true;
    editor.hidden = false;
    form.elements.title.value = note?.title || "";
    form.elements.source_url.value = note?.source_url || "";
    form.elements.body_md.value = note?.body_md || "";
    form.elements.title.focus();
  }

  function render() {
    if (!app) return;
    renderTree();
    if (editingId !== null) return;
    renderReader(byId(selectedId));
  }

  async function saveCurrent(form) {
    const currentNote = editingId ? byId(editingId) : null;
    const directoryId = currentNote ? currentNote.directory_id : selectedDirectoryId;
    const payload = {
      title: form.elements.title.value,
      body_md: form.elements.body_md.value,
      directory_id: directoryId ? Number(directoryId) : null,
      folder_path: "",
      source_url: form.elements.source_url.value || null,
      source_type: sourceTypeFor(form.elements.source_url.value),
      source_id: null,
      source_meta: {},
    };
    const url = editingId ? `/api/notes/${editingId}` : "/api/notes";
    const method = editingId ? "PUT" : "POST";
    const data = await requestJson(url, { method, body: JSON.stringify(payload) });
    selectedId = data.note.id;
    selectedDirectoryId = data.note.directory_id || "";
    editingId = null;
    await reloadNotes();
  }

  async function deleteSelected() {
    if (!showDeleteEntrypoints) return;
    if (!selectedId) return;
    const confirmed = await modalConfirm("删除笔记", "把这条笔记移入回收站？", "删除");
    if (!confirmed) return;
    await requestJson(`/api/notes/${selectedId}`, { method: "DELETE" });
    await reloadNotes();
  }

  async function createDirectory(parentId = null) {
    const name = await modalInput("新建目录", "目录名称", "");
    if (!name || !name.trim()) return;
    const data = await requestJson("/api/directories", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parentId ? Number(parentId) : null }),
    });
    tree = data.tree || tree;
    directories = data.directories || directories;
    selectedDirectoryId = data.directory?.id || selectedDirectoryId;
    render();
  }

  async function renameDirectory(id, currentName) {
    const name = await modalInput("重命名目录", "新的目录名称", currentName || "");
    if (!name || !name.trim() || name === currentName) return;
    const data = await requestJson(`/api/directories/${id}`, { method: "PUT", body: JSON.stringify({ name }) });
    tree = data.tree || tree;
    directories = data.directories || directories;
    await reloadNotes();
  }

  async function deleteDirectory(id, name) {
    const confirmed = await modalConfirm("删除目录", `删除目录“${name}”？\n\n目录下的笔记会移到未分类，不会被删除。`, "删除");
    if (!confirmed) return;
    const data = await requestJson(`/api/directories/${id}`, { method: "DELETE" });
    tree = data.tree || tree;
    directories = data.directories || directories;
    selectedDirectoryId = "";
    await reloadNotes();
  }

  async function moveDraggedTo(directoryId) {
    if (!dragged) return;
    const targetId = directoryId ? Number(directoryId) : null;
    if (dragged.type === "note") {
      const data = await requestJson(`/api/notes/${dragged.id}/move`, {
        method: "POST",
        body: JSON.stringify({ directory_id: targetId }),
      });
      selectedId = data.note.id;
      selectedDirectoryId = data.note.directory_id || "";
      await reloadNotes();
      return;
    }
    if (dragged.type === "directory") {
      if (String(targetId || "") === String(dragged.id)) return;
      await requestJson(`/api/directories/${dragged.id}/move`, {
        method: "POST",
        body: JSON.stringify({ parent_id: targetId }),
      });
      selectedDirectoryId = dragged.id;
      await reloadNotes();
    }
  }

  function isDirectoryDescendantTarget(sourceId, target) {
    const sourceBranch = app.querySelector(`[data-directory-branch="${CSS.escape(String(sourceId))}"]`);
    const targetBranch = target.closest("[data-directory-branch]");
    return Boolean(sourceBranch && targetBranch && sourceBranch !== targetBranch && sourceBranch.contains(targetBranch));
  }

  function clearDropTarget() {
    activeDropTarget?.classList.remove("is-drop-target");
    activeDropTarget = null;
  }

  function validDropTargetFor(dragState, target) {
    if (!target || !dragState) return false;
    if (dragState.type === "directory" && String(target.dataset.directoryId || "") === String(dragState.id)) return false;
    if (dragState.type === "directory" && isDirectoryDescendantTarget(dragState.id, target)) return false;
    return true;
  }

  function updatePointerDropTarget(event) {
    const element = document.elementFromPoint(event.clientX, event.clientY);
    const target = element?.closest?.("[data-directory-drop]");
    const nextTarget = validDropTargetFor(pointerDrag?.dragged, target) ? target : null;
    if (activeDropTarget === nextTarget) return;
    clearDropTarget();
    activeDropTarget = nextTarget;
    activeDropTarget?.classList.add("is-drop-target");
  }

  function finishPointerDrag() {
    pointerDrag?.node?.classList.remove("is-dragging");
    document.body.classList.remove("is-tree-dragging");
    clearDropTarget();
    pointerDrag = null;
  }

  async function restoreNote(id) {
    await requestJson(`/api/notes/${id}/restore`, { method: "POST" });
    document.querySelector(`[data-trash-note="${CSS.escape(String(id))}"]`)?.remove();
    refreshTrashEmptyState();
  }

  async function purgeNote(id) {
    const confirmed = await modalConfirm("永久删除", "永久删除后无法恢复，确认继续？", "永久删除");
    if (!confirmed) return;
    await requestJson(`/api/notes/${id}/purge`, { method: "DELETE" });
    document.querySelector(`[data-trash-note="${CSS.escape(String(id))}"]`)?.remove();
    refreshTrashEmptyState();
  }

  function closeContextMenu() {
    contextMenu?.remove();
    contextMenu = null;
    document.querySelector("[data-feature-menu]")?.setAttribute("aria-expanded", "false");
  }

  function openContextMenu(x, y, items) {
    closeContextMenu();
    contextMenu = document.createElement("div");
    contextMenu.className = "app-context-menu";
    contextMenu.setAttribute("role", "menu");
    contextMenu.innerHTML = items
      .map(
        (item, index) => {
          if (item.type === "separator") return `<span class="menu-separator" role="separator"></span>`;
          if (item.type === "switch") {
            return `
              <button type="button" class="menu-switch" data-menu-index="${index}" role="menuitemcheckbox" aria-checked="${item.checked ? "true" : "false"}">
                <span>${escapeHtml(item.label)}</span>
                <span class="switch-track" aria-hidden="true"><span></span></span>
              </button>
            `;
          }
          return `
            <button type="button" class="${item.danger ? "danger-item" : ""}" data-menu-index="${index}" role="menuitem">
              ${escapeHtml(item.label)}
            </button>
          `;
        }
      )
      .join("");
    document.body.appendChild(contextMenu);
    const box = contextMenu.getBoundingClientRect();
    const left = Math.min(x, window.innerWidth - box.width - 8);
    const top = Math.min(y, window.innerHeight - box.height - 8);
    contextMenu.style.left = `${Math.max(8, left)}px`;
    contextMenu.style.top = `${Math.max(8, top)}px`;
    contextMenu.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-menu-index]");
      if (!button) return;
      const item = items[Number(button.dataset.menuIndex)];
      closeContextMenu();
      if (item?.href) {
        window.location.href = item.href;
        return;
      }
      if (item?.action) await item.action();
    });
  }

  function openFeatureMenu(button) {
    const box = button.getBoundingClientRect();
    const items = [
      { label: "回收站", href: "/trash" },
      { label: "设置", href: "/settings" },
    ];
    openContextMenu(box.right, box.bottom + 6, items);
    button.setAttribute("aria-expanded", "true");
  }

  function renderDeleteEntryToggle() {
    if (!trashDeleteToggle) return;
    trashDeleteToggle.setAttribute("aria-pressed", showDeleteEntrypoints ? "true" : "false");
  }

  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement("div");
    dialog.className = "app-dialog-backdrop";
    dialog.hidden = true;
    document.body.appendChild(dialog);
    return dialog;
  }

  function closeDialog() {
    if (!dialog) return;
    cancelDialog = null;
    dialog.hidden = true;
    dialog.innerHTML = "";
  }

  function modalInput(title, label, initialValue = "") {
    const node = ensureDialog();
    return new Promise((resolve) => {
      node.innerHTML = `
        <form class="app-dialog" data-dialog-form>
          <h2>${escapeHtml(title)}</h2>
          <label>
            ${escapeHtml(label)}
            <input name="value" value="${escapeHtml(initialValue)}" autocomplete="off">
          </label>
          <div class="dialog-actions">
            <button class="secondary" type="button" data-dialog-cancel>取消</button>
            <button type="submit">保存</button>
          </div>
        </form>
      `;
      node.hidden = false;
      const form = node.querySelector("[data-dialog-form]");
      const input = form.elements.value;
      cancelDialog = () => {
        closeDialog();
        resolve(null);
      };
      input.focus();
      input.select();
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const value = input.value.trim();
        closeDialog();
        resolve(value || null);
      });
      node.querySelector("[data-dialog-cancel]").addEventListener("click", () => cancelDialog?.());
    });
  }

  function modalConfirm(title, message, confirmLabel = "确认") {
    const node = ensureDialog();
    return new Promise((resolve) => {
      node.innerHTML = `
        <section class="app-dialog">
          <h2>${escapeHtml(title)}</h2>
          <p>${escapeHtml(message).replace(/\n/g, "<br>")}</p>
          <div class="dialog-actions">
            <button class="secondary" type="button" data-dialog-cancel>取消</button>
            <button class="danger" type="button" data-dialog-confirm>${escapeHtml(confirmLabel)}</button>
          </div>
        </section>
      `;
      node.hidden = false;
      cancelDialog = () => {
        closeDialog();
        resolve(false);
      };
      node.querySelector("[data-dialog-cancel]").focus();
      node.querySelector("[data-dialog-cancel]").addEventListener("click", () => cancelDialog?.());
      node.querySelector("[data-dialog-confirm]").addEventListener("click", () => {
        closeDialog();
        resolve(true);
      });
    });
  }

  async function modalMessage(title, message) {
    const node = ensureDialog();
    return new Promise((resolve) => {
      node.innerHTML = `
        <section class="app-dialog">
          <h2>${escapeHtml(title)}</h2>
          <p>${escapeHtml(message).replace(/\n/g, "<br>")}</p>
          <div class="dialog-actions">
            <button type="button" data-dialog-confirm>知道了</button>
          </div>
        </section>
      `;
      node.hidden = false;
      cancelDialog = () => {
        closeDialog();
        resolve();
      };
      node.querySelector("[data-dialog-confirm]").focus();
      node.querySelector("[data-dialog-confirm]").addEventListener("click", () => cancelDialog?.());
    });
  }

  function refreshTrashEmptyState() {
    if (!trashList || trashList.querySelector("[data-trash-note]")) return;
    trashList.innerHTML = `<section class="empty-panel compact"><h2>回收站是空的</h2><p>暂时没有被删除的笔记。</p></section>`;
  }

  if (app) {
    render();

    app.addEventListener("contextmenu", (event) => {
      const noteRowNode = event.target.closest("[data-note-draggable]");
      const directoryRow = event.target.closest("[data-directory-draggable]");
      const rootRow = event.target.closest(".tree-root-drop");
      if (!noteRowNode && !directoryRow && !rootRow) return;
      event.preventDefault();
      if (noteRowNode) {
        const note = byId(noteRowNode.dataset.noteId);
        selectedId = Number(noteRowNode.dataset.noteId);
        selectedDirectoryId = note?.directory_id || "";
        render();
        const items = [{ label: "编辑笔记", action: () => renderEditor(note) }];
        if (showDeleteEntrypoints) {
          items.push({ label: "移到回收站", danger: true, action: () => deleteSelected() });
        }
        openContextMenu(event.clientX, event.clientY, items);
        return;
      }
      if (directoryRow) {
        const id = directoryRow.dataset.directoryId;
        const directory = directories.find((item) => String(item.id) === String(id));
        selectedDirectoryId = id || "";
        render();
        openContextMenu(event.clientX, event.clientY, [
          {
            label: "在此新建笔记",
            action: () => {
              selectedId = null;
              selectedDirectoryId = id;
              renderEditor(null);
            },
          },
          { label: "新建子目录", action: () => createDirectory(id) },
          { label: "重命名目录", action: () => renameDirectory(id, directory?.name || "") },
          { label: "删除目录", danger: true, action: () => deleteDirectory(id, directory?.name || "目录") },
        ]);
        return;
      }
      selectedDirectoryId = "";
      render();
      openContextMenu(event.clientX, event.clientY, [
        {
          label: "新建未分类笔记",
          action: () => {
            selectedId = null;
            selectedDirectoryId = "";
            renderEditor(null);
          },
        },
        { label: "新建根目录", action: () => createDirectory(null) },
      ]);
    });

    app.addEventListener("click", async (event) => {
      if (Date.now() < suppressClickUntil) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      closeContextMenu();
      const toggle = event.target.closest("[data-toggle-directory]");
      if (toggle) {
        const values = collapsedDirectories();
        const id = String(toggle.dataset.toggleDirectory);
        if (values.has(id)) values.delete(id);
        else values.add(id);
        saveCollapsedDirectories(values);
        renderTree();
        return;
      }

      const selectDirectory = event.target.closest("[data-select-directory]");
      if (selectDirectory) {
        selectedDirectoryId = selectDirectory.dataset.selectDirectory || "";
        render();
        return;
      }

      const select = event.target.closest("[data-select-note]");
      if (select) {
        const note = byId(select.dataset.selectNote);
        selectedId = Number(select.dataset.selectNote);
        selectedDirectoryId = note?.directory_id || "";
        editingId = null;
        render();
        return;
      }

      if (event.target.closest("[data-new-directory]")) {
        await createDirectory(null);
        return;
      }

      if (event.target.closest("[data-new-note]")) {
        renderEditor(null);
        return;
      }

      if (event.target.closest("[data-edit-note]")) {
        renderEditor(byId(selectedId));
        return;
      }

      if (event.target.closest("[data-cancel-edit]")) {
        editingId = null;
        render();
        return;
      }

      if (event.target.closest("[data-delete-note]")) {
        await deleteSelected();
      }
    });

    app.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      const blocked = event.target.closest("button, input, select, textarea, a");
      if (blocked) return;
      const note = event.target.closest("[data-note-draggable]");
      const directory = event.target.closest("[data-directory-draggable]");
      const node = note || directory;
      if (!node) return;
      pointerDrag = {
        active: false,
        node,
        startX: event.clientX,
        startY: event.clientY,
        pointerId: event.pointerId,
        dragged: note
          ? { type: "note", id: Number(note.dataset.noteId) }
          : { type: "directory", id: Number(directory.dataset.directoryId) },
      };
    });

    app.addEventListener("dragstart", (event) => {
      if (event.target.closest("[data-note-draggable], [data-directory-draggable]")) {
        event.preventDefault();
        return;
      }
      const note = event.target.closest("[data-note-draggable]");
      if (note) {
        const blocked = event.target.closest("input, select, textarea, a");
        if (blocked) {
          event.preventDefault();
          return;
        }
        dragged = { type: "note", id: Number(note.dataset.noteId) };
        note.classList.add("is-dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", String(dragged.id));
        return;
      }
      const directory = event.target.closest("[data-directory-draggable]");
      if (directory) {
        const blocked = event.target.closest("button, input, select, textarea, a");
        if (blocked) {
          event.preventDefault();
          return;
        }
        dragged = { type: "directory", id: Number(directory.dataset.directoryId) };
        directory.classList.add("is-dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", String(dragged.id));
      }
    });

    app.addEventListener("dragend", () => {
      dragged = null;
      app.querySelectorAll(".is-dragging, .is-drop-target").forEach((node) => node.classList.remove("is-dragging", "is-drop-target"));
    });

    app.addEventListener("dragover", (event) => {
      if (!dragged) return;
      const target = event.target.closest("[data-directory-drop]");
      if (!target) return;
      if (!validDropTargetFor(dragged, target)) return;
      event.preventDefault();
      target.classList.add("is-drop-target");
      event.dataTransfer.dropEffect = "move";
    });

    app.addEventListener("dragleave", (event) => {
      const target = event.target.closest("[data-directory-drop]");
      if (target && !target.contains(event.relatedTarget)) target.classList.remove("is-drop-target");
    });

    app.addEventListener("drop", async (event) => {
      const target = event.target.closest("[data-directory-drop]");
      if (!target || !dragged) return;
      if (!validDropTargetFor(dragged, target)) return;
      event.preventDefault();
      target.classList.remove("is-drop-target");
      try {
        await moveDraggedTo(target.dataset.directoryId || null);
      } catch (error) {
        await modalMessage("移动失败", String(error.message || error).slice(0, 180));
      } finally {
        dragged = null;
      }
    });

    app.querySelector("[data-note-form]")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      await saveCurrent(event.currentTarget);
    });

    app.querySelector("[data-note-search]")?.addEventListener("input", (event) => {
      clearTimeout(event.currentTarget._timer);
      event.currentTarget._timer = setTimeout(() => reloadNotes(event.currentTarget.value), 180);
    });

    app.addEventListener("keydown", (event) => {
      const note = event.target.closest("[data-select-note]");
      if (!note || !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      note.click();
    });
  }

  if (learningApp) {
    restoreLearningState();
    renderBiliLogin();
    renderBiliFolders();
    renderBiliVideos();
    renderBiliOverwriteToggle();
    renderBiliImportControls();
    renderBiliLogFromState();
    loadBiliSession().catch((error) => {
      const node = learningNode("[data-bili-login-state]");
      if (node) node.innerHTML = `<p class="muted-text">${escapeHtml(String(error.message || error).slice(0, 160))}</p>`;
    });
    loadCurrentBiliImportJob();
    learningApp.addEventListener("click", async (event) => {
      const start = event.target.closest("[data-bili-start-login]");
      if (start) {
        await startBiliLogin();
        return;
      }
      if (event.target.closest("[data-bili-refresh-session]")) {
        await loadBiliSession();
        return;
      }
      if (event.target.closest("[data-bili-load-folders]")) {
        await loadBiliFolders();
        return;
      }
      if (event.target.closest("[data-bili-logout]")) {
        await requestJson("/api/bilibili/logout", { method: "POST", body: "{}" });
        clearLearningState();
        stopBiliJobPolling();
        biliState = { loggedIn: false, user: null, folders: [], selectedFolder: null, videos: [], overwriteExisting: biliState.overwriteExisting, importLog: null, importJobId: null, importRunning: false, pollTimer: null, jobPollTimer: null };
        renderBiliLogin();
        renderBiliFolders();
        renderBiliVideos();
        renderBiliOverwriteToggle();
        renderBiliImportControls();
        const log = learningNode("[data-bili-import-log]");
        if (log) log.hidden = true;
        return;
      }
      const folder = event.target.closest("[data-bili-folder]");
      if (folder) {
        await loadBiliVideos(folder.dataset.biliFolder);
        return;
      }
      if (event.target.closest("[data-bili-select-all]")) {
        learningApp.querySelectorAll("[data-bili-video-check]").forEach((input) => {
          input.checked = true;
        });
        return;
      }
      if (event.target.closest("[data-bili-overwrite-toggle]")) {
        biliState.overwriteExisting = !biliState.overwriteExisting;
        renderBiliOverwriteToggle();
        persistLearningState();
        return;
      }
      if (event.target.closest("[data-bili-import-selected]")) {
        await importSelectedBiliVideos();
        return;
      }
      if (event.target.closest("[data-bili-cancel-import]")) {
        await cancelBiliImportJob();
      }
    });
  }

  if (trashList) {
    renderDeleteEntryToggle();
    trashList.addEventListener("click", async (event) => {
      closeContextMenu();
      const restore = event.target.closest("[data-restore-note]");
      if (restore) {
        await restoreNote(restore.dataset.restoreNote);
        return;
      }
      const purge = event.target.closest("[data-purge-note]");
      if (purge) await purgeNote(purge.dataset.purgeNote);
    });
  }

  if (trashDeleteToggle) {
    renderDeleteEntryToggle();
    trashDeleteToggle.addEventListener("click", () => {
      showDeleteEntrypoints = !showDeleteEntrypoints;
      saveBoolean(showDeleteEntrypointsKey, showDeleteEntrypoints);
      renderDeleteEntryToggle();
    });
  }

  document.addEventListener("pointermove", (event) => {
    if (!pointerDrag || pointerDrag.pointerId !== event.pointerId) return;
    const dx = Math.abs(event.clientX - pointerDrag.startX);
    const dy = Math.abs(event.clientY - pointerDrag.startY);
    if (!pointerDrag.active && dx + dy < 7) return;
    if (!pointerDrag.active) {
      pointerDrag.active = true;
      pointerDrag.node.classList.add("is-dragging");
      document.body.classList.add("is-tree-dragging");
    }
    event.preventDefault();
    updatePointerDropTarget(event);
  });

  document.addEventListener("pointerup", async (event) => {
    if (!pointerDrag || pointerDrag.pointerId !== event.pointerId) return;
    const wasActive = pointerDrag.active;
    const dropTarget = activeDropTarget;
    const dragState = pointerDrag.dragged;
    if (wasActive) {
      event.preventDefault();
      suppressClickUntil = Date.now() + 250;
    }
    finishPointerDrag();
    if (!wasActive || !dropTarget) return;
    dragged = dragState;
    try {
      await moveDraggedTo(dropTarget.dataset.directoryId || null);
    } catch (error) {
      await modalMessage("移动失败", String(error.message || error).slice(0, 180));
    } finally {
      dragged = null;
    }
  });

  document.addEventListener("pointercancel", finishPointerDrag);

  document.addEventListener("click", (event) => {
    const featureButton = event.target.closest("[data-feature-menu]");
    if (featureButton) {
      event.preventDefault();
      if (contextMenu) {
        closeContextMenu();
      } else {
        openFeatureMenu(featureButton);
      }
      return;
    }
    if (contextMenu && !event.target.closest(".app-context-menu")) closeContextMenu();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeContextMenu();
      if (cancelDialog) cancelDialog();
    }
  });
})();
