(() => {
  const partialMap = {
    "/bookmarks": { partial: "/bookmarks/partials", target: "#bookmark-browser" },
    "/trash": { partial: "/trash/partials", target: "#trash-lists" },
    "/jobs": { partial: "/jobs/partials", target: "#jobs-list" },
  };
  let autoRefreshTimer = null;
  let liveFilterTimer = null;
  let liveFilterController = null;
  let draggedDirectoryId = null;
  let draggedBookmarkId = null;

  function asUrl(value) {
    return new URL(value, window.location.origin);
  }

  function partialUrlFrom(urlLike) {
    const url = asUrl(urlLike);
    const mapped = partialMap[url.pathname];
    if (!mapped) return null;
    return `${mapped.partial}${url.search}`;
  }

  function targetForUrl(urlLike) {
    const url = asUrl(urlLike);
    return partialMap[url.pathname]?.target || null;
  }

  function captureFocus(target) {
    const active = document.activeElement;
    if (!active || !target.contains(active) || !active.name) return null;
    return {
      name: active.name,
      selectionStart: active.selectionStart,
      selectionEnd: active.selectionEnd,
    };
  }

  function restoreFocus(state) {
    if (!state) return;
    const form = document.querySelector("[data-filter-form]");
    const element = form?.elements?.[state.name];
    if (!element || typeof element.focus !== "function") return;
    element.focus();
    if (
      typeof element.setSelectionRange === "function" &&
      Number.isInteger(state.selectionStart) &&
      Number.isInteger(state.selectionEnd)
    ) {
      element.setSelectionRange(state.selectionStart, state.selectionEnd);
    }
  }

  async function replaceFragment(targetSelector, partialUrl, nextUrl, options = {}) {
    const target = document.querySelector(targetSelector);
    if (!target || !partialUrl) return;
    const focusState = options.preserveFocus ? captureFocus(target) : null;
    target.classList.add("is-loading");
    try {
      const response = await fetch(partialUrl, {
        headers: { "X-Requested-With": "fetch" },
        signal: options.signal,
      });
      if (!response.ok) throw new Error(await response.text());
      const html = await response.text();
      target.outerHTML = html;
      setupAutoRefresh();
      restoreFocus(focusState);
      if (nextUrl) {
        const historyState = { target: targetSelector };
        if (options.history === "replace") {
          window.history.replaceState(historyState, "", nextUrl);
        } else {
          window.history.pushState(historyState, "", nextUrl);
        }
      }
    } finally {
      if (target.isConnected) {
        target.classList.remove("is-loading");
      }
    }
  }

  function abortLiveFilter() {
    if (liveFilterController) {
      liveFilterController.abort();
      liveFilterController = null;
    }
  }

  async function runLiveFilter(form) {
    abortLiveFilter();
    const controller = new AbortController();
    liveFilterController = controller;
    try {
      await handleFilterForm(form, {
        history: "replace",
        preserveFocus: true,
        signal: controller.signal,
      });
    } catch (error) {
      if (error.name !== "AbortError") {
        toast(messageFromError(error));
      }
    } finally {
      if (liveFilterController === controller) {
        liveFilterController = null;
      }
    }
  }

  function scheduleLiveFilter(form, delay = 300) {
    window.clearTimeout(liveFilterTimer);
    abortLiveFilter();
    liveFilterTimer = window.setTimeout(() => {
      if (form.isConnected) {
        runLiveFilter(form);
      } else {
        const nextForm = document.querySelector("[data-filter-form]");
        if (nextForm) runLiveFilter(nextForm);
      }
    }, delay);
  }

  function toast(message) {
    let node = document.querySelector(".toast");
    if (!node) {
      node = document.createElement("div");
      node.className = "toast";
      document.body.appendChild(node);
    }
    node.textContent = message;
    node.classList.add("show");
    window.clearTimeout(node.dataset.timer);
    node.dataset.timer = window.setTimeout(() => node.classList.remove("show"), 1600);
  }

  function messageFromError(error) {
    if (!error) return "操作失败";
    return String(error.message || error).slice(0, 160);
  }

  function setBusy(form, busy) {
    form.querySelectorAll("button, input, select, textarea").forEach((element) => {
      if (busy) {
        element.dataset.wasDisabled = element.disabled ? "1" : "0";
        element.disabled = true;
      } else if (element.dataset.wasDisabled !== "1") {
        element.disabled = false;
      }
    });
  }

  async function handlePartialLink(link) {
    const href = link.getAttribute("href");
    const partialUrl = partialUrlFrom(href);
    const target = link.dataset.target || targetForUrl(href);
    if (!partialUrl || !target) return false;
    await replaceFragment(target, partialUrl, href);
    return true;
  }

  function filterUrlFromForm(form) {
    const params = new URLSearchParams();
    new FormData(form).forEach((value, key) => {
      if (String(value).trim()) params.set(key, value);
    });
    const path = form.getAttribute("action") || window.location.pathname;
    return `${path}${params.toString() ? `?${params.toString()}` : ""}`;
  }

  async function handleFilterForm(form, options = {}) {
    const url = filterUrlFromForm(form);
    await replaceFragment(form.dataset.target || targetForUrl(url), partialUrlFrom(url), url, options);
  }

  async function handleAsyncForm(form) {
    const confirmed = form.dataset.confirm ? window.confirm(form.dataset.confirm) : true;
    if (!confirmed) return;
    setBusy(form, true);
    try {
      const response = await fetch(form.action, {
        method: (form.method || "POST").toUpperCase(),
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
      });
      if (!response.ok) throw new Error(await response.text());
      const finalUrl = response.url || window.location.href;
      const target = form.dataset.target || targetForUrl(finalUrl);
      await replaceFragment(target, partialUrlFrom(finalUrl), finalUrl);
      toast("已更新");
    } finally {
      setBusy(form, false);
    }
  }

  async function handleApiForm(form) {
    const confirmed = form.dataset.confirm ? window.confirm(form.dataset.confirm) : true;
    if (!confirmed) return;
    setBusy(form, true);
    try {
      const response = await fetch(form.dataset.apiAction, {
        method: form.dataset.apiMethod || "POST",
        headers: { "X-Requested-With": "fetch" },
      });
      if (!response.ok) throw new Error(await response.text());
      const target = form.dataset.refreshFragment;
      if (target) {
        const partialUrl = partialUrlFrom(window.location.href) || partialUrlFrom(target === "#jobs-list" ? "/jobs" : "/trash");
        await replaceFragment(target, partialUrl, null);
      } else {
        form.closest("[data-card]")?.remove();
      }
      toast("已完成");
    } finally {
      setBusy(form, false);
    }
  }

  async function handleDirectoryForm(form) {
    setBusy(form, true);
    try {
      const payload = {};
      new FormData(form).forEach((value, key) => {
        const text = String(value).trim();
        if (key === "parent_id") {
          payload[key] = text ? Number(text) : null;
        } else {
          payload[key] = text;
        }
      });
      const response = await fetch(form.dataset.directoryAction, {
        method: form.dataset.directoryMethod || "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      await refreshBookmarksForDirectory(data.path);
      toast("目录已更新");
    } finally {
      setBusy(form, false);
    }
  }

  function directoryImpactMessage(preview, title = "删除目录") {
    const paths = (preview.paths || []).join("、");
    return `${title}：${paths}\n\n将删除 ${preview.directory_count || 0} 个目录（含 ${preview.children_count || 0} 个子目录），${preview.bookmark_count || 0} 条收藏会移到未分类。\n\n收藏内容、标签和关键词不会被删除。确认继续？`;
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "fetch",
        ...(options.headers || {}),
      },
      ...options,
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  function toggleDirectoryInline(button) {
    const branch = button.closest("[data-directory-branch]");
    if (!branch) return;
    const mode = button.dataset.directoryToggle;
    Array.from(branch.children)
      .filter((child) => child.matches?.("[data-directory-inline]"))
      .forEach((form) => {
        const shouldOpen = form.dataset.directoryInline === mode && form.hidden;
        form.hidden = !shouldOpen;
        if (shouldOpen) form.querySelector("input")?.focus();
      });
  }

  function toggleRootDirectoryForm(button) {
    const zone = button.closest("[data-directory-drop]");
    const form = zone?.querySelector("[data-root-directory-form]");
    if (!form) return;
    form.hidden = !form.hidden;
    if (!form.hidden) form.querySelector("input")?.focus();
  }

  function bookmarkBrowserFrom(node) {
    return node?.closest?.("#bookmark-browser") || document.querySelector("#bookmark-browser");
  }

  function isDirectoryManageMode(node) {
    return Boolean(bookmarkBrowserFrom(node)?.classList.contains("directory-manage-mode"));
  }

  function setDirectoryManageMode(browser, active) {
    if (!browser) return;
    browser.classList.toggle("directory-manage-mode", active);
    if (active) return;
    browser.classList.remove("directory-bulk-mode");
    browser.querySelectorAll("[data-directory-inline], [data-root-directory-form]").forEach((form) => {
      form.hidden = true;
    });
    browser.querySelectorAll("[data-directory-select]").forEach((input) => {
      input.checked = false;
    });
    browser.querySelectorAll("[data-directory-bulk-toggle]").forEach((button) => {
      button.textContent = "批量";
    });
    browser.querySelectorAll("[data-directory-bulk-delete]").forEach((button) => {
      button.hidden = true;
    });
  }

  async function deleteDirectory(button) {
    const id = button.dataset.directoryId;
    const preview = await fetchJson(`/api/directories/${id}/delete-preview`);
    if (!window.confirm(directoryImpactMessage(preview))) return;
    const result = await fetchJson(`/api/directories/${id}`, { method: "DELETE" });
    await refreshBookmarksForDirectory(result.redirect_directory || "__none__");
    toast("目录已删除，收藏已移到未分类");
  }

  function toggleDirectoryBulkMode(button) {
    const browser = document.querySelector("#bookmark-browser");
    if (!browser) return;
    const active = !browser.classList.contains("directory-bulk-mode");
    browser.classList.toggle("directory-bulk-mode", active);
    button.textContent = active ? "退出批量" : "批量";
    const deleteButton = browser.querySelector("[data-directory-bulk-delete]");
    if (deleteButton) deleteButton.hidden = !active;
    if (!active) {
      browser.querySelectorAll("[data-directory-select]").forEach((input) => {
        input.checked = false;
      });
    }
  }

  async function bulkDeleteDirectories(button) {
    const browser = button.closest("#bookmark-browser") || document;
    const ids = Array.from(browser.querySelectorAll("[data-directory-select]:checked")).map((input) =>
      Number(input.value)
    );
    if (!ids.length) {
      toast("先选择要删除的目录");
      return;
    }
    const preview = await fetchJson("/api/directories/bulk-delete", {
      method: "POST",
      body: JSON.stringify({ ids, preview: true }),
    });
    if (!window.confirm(directoryImpactMessage(preview, "批量删除目录"))) return;
    const result = await fetchJson("/api/directories/bulk-delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    await refreshBookmarksForDirectory(result.redirect_directory || "__none__");
    toast("目录已批量删除，收藏已移到未分类");
  }

  async function moveDraggedDirectory(parentId) {
    if (!draggedDirectoryId) return;
    if (String(parentId || "") === String(draggedDirectoryId)) return;
    const response = await fetch(`/api/directories/${draggedDirectoryId}/move`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "fetch",
      },
      body: JSON.stringify({ parent_id: parentId ? Number(parentId) : null }),
    });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    await refreshBookmarksForDirectory(data.path);
    toast("目录已移动");
  }

  async function moveDraggedBookmark(directoryId) {
    if (!draggedBookmarkId) return;
    const response = await fetch(`/api/bookmarks/${draggedBookmarkId}/move`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "fetch",
      },
      body: JSON.stringify({ directory_id: directoryId ? Number(directoryId) : null }),
    });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    await replaceFragment("#bookmark-browser", partialUrlFrom(window.location.href), window.location.href, {
      history: "replace",
    });
    toast(`已移动到 ${data.directory || "未分类"}`);
  }

  async function refreshBookmarksForDirectory(path) {
    const nextUrl = `/bookmarks?directory=${encodeURIComponent(path)}`;
    await replaceFragment("#bookmark-browser", partialUrlFrom(nextUrl), nextUrl);
  }

  async function copyLink(button) {
    const url = button.dataset.url;
    try {
      await navigator.clipboard.writeText(url);
      toast("链接已复制");
    } catch {
      window.prompt("复制链接", url);
    }
  }

  async function rewriteSummary(button) {
    const box = button.closest("[data-summary-tools]");
    const summary = document.querySelector('textarea[name="summary"]');
    if (!box || !summary) return;
    const status = box.querySelector("[data-summary-status]");
    button.disabled = true;
    if (status) status.textContent = "正在重写...";
    try {
      const response = await fetch(`/api/jobs/${box.dataset.jobId}/summary/rewrite`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({
          length: box.querySelector("[data-summary-length]")?.value || "normal",
          style: box.querySelector("[data-summary-style]")?.value || "note",
          current_summary: summary.value,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      summary.value = data.summary || summary.value;
      if (status) status.textContent = data.error ? "已用本地规则改写" : "已重写";
      toast("简介已重写");
    } finally {
      button.disabled = false;
    }
  }

  function applySummaryPreset(button) {
    const box = button.closest("[data-summary-tools]");
    if (!box) return;
    const length = box.querySelector("[data-summary-length]");
    const style = box.querySelector("[data-summary-style]");
    if (length && button.dataset.length) length.value = button.dataset.length;
    if (style && button.dataset.style) style.value = button.dataset.style;
    box.querySelector("[data-summary-rewrite]")?.click();
  }

  function appendToken(button) {
    const input = document.querySelector(`[name="${button.dataset.appendToken}"]`);
    if (!input) return;
    const nextToken = (button.dataset.token || "").trim();
    if (!nextToken) return;
    const tokens = input.value
      .split(",")
      .map((token) => token.trim())
      .filter(Boolean);
    if (!tokens.includes(nextToken)) {
      tokens.push(nextToken);
    }
    input.value = tokens.join(", ");
    input.focus();
  }

  function fillNamedInput(button) {
    const input = document.querySelector(`[name="${button.dataset.fillInput}"]`);
    if (!input) return;
    input.value = button.dataset.value || "";
    input.focus();
  }

  document.addEventListener("click", async (event) => {
    const summaryPreset = event.target.closest("[data-summary-preset]");
    if (summaryPreset) {
      event.preventDefault();
      applySummaryPreset(summaryPreset);
      return;
    }

    const rewriteButton = event.target.closest("[data-summary-rewrite]");
    if (rewriteButton) {
      event.preventDefault();
      try {
        await rewriteSummary(rewriteButton);
      } catch (error) {
        toast(messageFromError(error));
      }
      return;
    }

    const tokenButton = event.target.closest("[data-append-token]");
    if (tokenButton) {
      event.preventDefault();
      appendToken(tokenButton);
      return;
    }

    const fillButton = event.target.closest("[data-fill-input]");
    if (fillButton) {
      event.preventDefault();
      fillNamedInput(fillButton);
      return;
    }

    const copyButton = event.target.closest(".copy-link");
    if (copyButton) {
      event.preventDefault();
      await copyLink(copyButton);
      return;
    }

    const manageToggle = event.target.closest("[data-directory-manage-toggle]");
    if (manageToggle) {
      event.preventDefault();
      setDirectoryManageMode(bookmarkBrowserFrom(manageToggle), true);
      return;
    }

    const manageDone = event.target.closest("[data-directory-manage-done]");
    if (manageDone) {
      event.preventDefault();
      setDirectoryManageMode(bookmarkBrowserFrom(manageDone), false);
      return;
    }

    const directoryToggle = event.target.closest("[data-directory-toggle]");
    if (directoryToggle) {
      event.preventDefault();
      if (!isDirectoryManageMode(directoryToggle)) return;
      toggleDirectoryInline(directoryToggle);
      return;
    }

    const directoryCancel = event.target.closest("[data-directory-cancel]");
    if (directoryCancel) {
      event.preventDefault();
      directoryCancel.closest("[data-directory-inline]").hidden = true;
      return;
    }

    const rootToggle = event.target.closest("[data-root-directory-toggle]");
    if (rootToggle) {
      event.preventDefault();
      if (!isDirectoryManageMode(rootToggle)) return;
      toggleRootDirectoryForm(rootToggle);
      return;
    }

    const rootCancel = event.target.closest("[data-root-directory-cancel]");
    if (rootCancel) {
      event.preventDefault();
      rootCancel.closest("[data-root-directory-form]").hidden = true;
      return;
    }

    const directoryDelete = event.target.closest("[data-directory-delete]");
    if (directoryDelete) {
      event.preventDefault();
      if (!isDirectoryManageMode(directoryDelete)) return;
      try {
        await deleteDirectory(directoryDelete);
      } catch (error) {
        toast(messageFromError(error));
      }
      return;
    }

    const bulkToggle = event.target.closest("[data-directory-bulk-toggle]");
    if (bulkToggle) {
      event.preventDefault();
      if (!isDirectoryManageMode(bulkToggle)) return;
      toggleDirectoryBulkMode(bulkToggle);
      return;
    }

    const bulkDelete = event.target.closest("[data-directory-bulk-delete]");
    if (bulkDelete) {
      event.preventDefault();
      if (!isDirectoryManageMode(bulkDelete)) return;
      try {
        await bulkDeleteDirectories(bulkDelete);
      } catch (error) {
        toast(messageFromError(error));
      }
      return;
    }

    const link = event.target.closest("[data-partial-link]");
    if (!link) return;
    event.preventDefault();
    try {
      await handlePartialLink(link);
    } catch (error) {
      toast(messageFromError(error));
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.matches("[data-filter-form], [data-async-form], [data-api-action], [data-directory-form]")) return;
    event.preventDefault();
    try {
      if (form.matches("[data-filter-form]")) {
        await handleFilterForm(form);
      } else if (form.matches("[data-directory-form]")) {
        await handleDirectoryForm(form);
      } else if (form.dataset.apiAction) {
        await handleApiForm(form);
      } else {
        await handleAsyncForm(form);
      }
    } catch (error) {
      toast(messageFromError(error));
    }
  });

  document.addEventListener("dragstart", (event) => {
    const card = event.target.closest("[data-bookmark-draggable]");
    if (card && !event.target.closest("a, button, input, select, textarea, summary, form")) {
      draggedBookmarkId = card.dataset.bookmarkId;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggedBookmarkId);
      card.classList.add("is-dragging");
      document.querySelector(".library-sidebar")?.classList.add("is-bookmark-drop-mode");
      return;
    }

    const node = event.target.closest("[data-directory-draggable]");
    if (!node) return;
    if (!isDirectoryManageMode(node)) {
      event.preventDefault();
      return;
    }
    draggedDirectoryId = node.dataset.directoryId;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", draggedDirectoryId);
    node.classList.add("is-dragging");
  });

  document.addEventListener("dragend", (event) => {
    event.target.closest("[data-directory-draggable]")?.classList.remove("is-dragging");
    event.target.closest("[data-bookmark-draggable]")?.classList.remove("is-dragging");
    document
      .querySelectorAll(".is-drop-target, .is-bookmark-drop-target")
      .forEach((node) => node.classList.remove("is-drop-target", "is-bookmark-drop-target"));
    document.querySelector(".library-sidebar")?.classList.remove("is-bookmark-drop-mode");
    draggedDirectoryId = null;
    draggedBookmarkId = null;
  });

  document.addEventListener("dragover", (event) => {
    if (draggedBookmarkId) {
      const bookmarkTarget = event.target.closest("[data-bookmark-drop]");
      if (!bookmarkTarget) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      bookmarkTarget.classList.add("is-bookmark-drop-target");
      return;
    }

    const target = event.target.closest("[data-directory-drop]");
    if (!target || !draggedDirectoryId) return;
    if (String(target.dataset.parentId || "") === String(draggedDirectoryId)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    target.classList.add("is-drop-target");
  });

  document.addEventListener("dragleave", (event) => {
    const bookmarkTarget = event.target.closest("[data-bookmark-drop]");
    if (bookmarkTarget && !bookmarkTarget.contains(event.relatedTarget)) {
      bookmarkTarget.classList.remove("is-bookmark-drop-target");
    }
    const target = event.target.closest("[data-directory-drop]");
    if (target && !target.contains(event.relatedTarget)) {
      target.classList.remove("is-drop-target");
    }
  });

  document.addEventListener("drop", async (event) => {
    if (draggedBookmarkId) {
      const bookmarkTarget = event.target.closest("[data-bookmark-drop]");
      if (!bookmarkTarget) return;
      event.preventDefault();
      bookmarkTarget.classList.remove("is-bookmark-drop-target");
      try {
        await moveDraggedBookmark(bookmarkTarget.dataset.bookmarkDirectoryId || null);
      } catch (error) {
        toast(messageFromError(error));
      } finally {
        draggedBookmarkId = null;
        document.querySelector(".library-sidebar")?.classList.remove("is-bookmark-drop-mode");
        document
          .querySelectorAll(".is-bookmark-drop-target")
          .forEach((node) => node.classList.remove("is-bookmark-drop-target"));
      }
      return;
    }

    const target = event.target.closest("[data-directory-drop]");
    if (!target || !draggedDirectoryId) return;
    event.preventDefault();
    target.classList.remove("is-drop-target");
    try {
      await moveDraggedDirectory(target.dataset.parentId || null);
    } catch (error) {
      toast(messageFromError(error));
    } finally {
      draggedDirectoryId = null;
      document.querySelectorAll(".is-drop-target").forEach((node) => node.classList.remove("is-drop-target"));
    }
  });

  document.addEventListener("input", (event) => {
    const control = event.target;
    if (!(control instanceof HTMLInputElement)) return;
    const form = control.closest("[data-filter-form]");
    if (!form || control.name !== "query") return;
    scheduleLiveFilter(form, 300);
  });

  document.addEventListener("change", (event) => {
    const control = event.target;
    if (!(control instanceof HTMLSelectElement)) return;
    const form = control.closest("[data-filter-form]");
    if (!form) return;
    scheduleLiveFilter(form, 0);
  });

  window.addEventListener("popstate", async () => {
    const target = targetForUrl(window.location.href);
    const partialUrl = partialUrlFrom(window.location.href);
    if (!target || !document.querySelector(target)) return;
    try {
      await replaceFragment(target, partialUrl, null);
    } catch (error) {
      toast(messageFromError(error));
    }
  });

  function setupAutoRefresh() {
    if (autoRefreshTimer) {
      window.clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
    const node = document.querySelector("[data-auto-refresh]");
    if (!node) return;
    const seconds = Number(node.dataset.autoRefresh || 0);
    if (!seconds) return;
    const selector = `#${node.id}`;
    autoRefreshTimer = window.setInterval(async () => {
      if (!document.querySelector(selector)) {
        setupAutoRefresh();
        return;
      }
      try {
        await replaceFragment(selector, partialUrlFrom(window.location.href), null);
      } catch (error) {
        toast(messageFromError(error));
      }
    }, seconds * 1000);
  }

  setupAutoRefresh();
})();
