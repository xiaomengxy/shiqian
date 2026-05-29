(() => {
  const partialMap = {
    "/bookmarks": { partial: "/bookmarks/partials", target: "#bookmark-browser" },
    "/directories": { partial: "/directories/partials", target: "#directory-workspace" },
    "/trash": { partial: "/trash/partials", target: "#trash-lists" },
    "/jobs": { partial: "/jobs/partials", target: "#jobs-list" },
  };
  let autoRefreshTimer = null;

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

  async function replaceFragment(targetSelector, partialUrl, pushUrl) {
    const target = document.querySelector(targetSelector);
    if (!target || !partialUrl) return;
    target.classList.add("is-loading");
    const response = await fetch(partialUrl, { headers: { "X-Requested-With": "fetch" } });
    if (!response.ok) throw new Error(await response.text());
    const html = await response.text();
    target.outerHTML = html;
    setupAutoRefresh();
    if (pushUrl) {
      window.history.pushState({ target: targetSelector }, "", pushUrl);
    }
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

  async function handleFilterForm(form) {
    const params = new URLSearchParams();
    new FormData(form).forEach((value, key) => {
      if (String(value).trim()) params.set(key, value);
    });
    const path = form.getAttribute("action") || window.location.pathname;
    const url = `${path}${params.toString() ? `?${params.toString()}` : ""}`;
    await replaceFragment(form.dataset.target || targetForUrl(url), partialUrlFrom(url), url);
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

  document.addEventListener("click", async (event) => {
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

    const copyButton = event.target.closest(".copy-link");
    if (copyButton) {
      event.preventDefault();
      await copyLink(copyButton);
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
    if (!form.matches("[data-filter-form], [data-async-form], [data-api-action]")) return;
    event.preventDefault();
    try {
      if (form.matches("[data-filter-form]")) {
        await handleFilterForm(form);
      } else if (form.dataset.apiAction) {
        await handleApiForm(form);
      } else {
        await handleAsyncForm(form);
      }
    } catch (error) {
      toast(messageFromError(error));
    }
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
