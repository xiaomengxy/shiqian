(() => {
  const app = document.querySelector("[data-notes-app]");
  const trashList = document.querySelector("[data-trash-list]");
  const md = window.markdownit ? window.markdownit({ html: false, linkify: true, breaks: true }) : null;
  const collapsedKey = "shiqian.notes.collapsedDirectories";

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
  }

  function openContextMenu(x, y, items) {
    closeContextMenu();
    contextMenu = document.createElement("div");
    contextMenu.className = "app-context-menu";
    contextMenu.setAttribute("role", "menu");
    contextMenu.innerHTML = items
      .map(
        (item, index) => `
          <button type="button" class="${item.danger ? "danger-item" : ""}" data-menu-index="${index}" role="menuitem">
            ${escapeHtml(item.label)}
          </button>
        `
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
      if (item?.action) await item.action();
    });
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
        openContextMenu(event.clientX, event.clientY, [
          { label: "编辑笔记", action: () => renderEditor(note) },
          { label: "移到回收站", danger: true, action: () => deleteSelected() },
        ]);
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

  if (trashList) {
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
    if (contextMenu && !event.target.closest(".app-context-menu")) closeContextMenu();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeContextMenu();
      if (cancelDialog) cancelDialog();
    }
  });
})();
