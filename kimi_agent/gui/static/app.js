// Kimi Agent desktop app. Plain JavaScript, no build step and no third-party code.
(function () {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);

  // The key from this window's launch link. Every API call needs it. It is kept in sessionStorage, which (unlike
  // cookies) other web apps on 127.0.0.1 can't read.
  const KEY = (() => {
    const meta = document.querySelector('meta[name="kimi-agent-key"]');
    if (meta && meta.content) {
      sessionStorage.setItem("kimi.key", meta.content);
      meta.remove();
      history.replaceState(null, "", "/");
    }
    return sessionStorage.getItem("kimi.key") || "";
  })();

  const ICONS = {
    plus: '<path d="M12 5v14M5 12h14"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9.5 4v16"/>',
    edit: '<path d="M12 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-6"/><path d="M17.5 3.5a2.1 2.1 0 0 1 3 3L13 14l-4 1 1-4z"/>',
    more: '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
    arrowUp: '<path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/>',
    arrowDown: '<path d="M12 5v14M5.5 12.5L12 19l6.5-6.5"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    folderPlus: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 10.5v5M9.5 13h5"/>',
    shield: '<path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
    hand: '<path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V11M11 10.5v-6a1.5 1.5 0 0 1 3 0V11M14 6.5a1.5 1.5 0 0 1 3 0V12M17 9.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1.6a6 6 0 0 1-4.6-2.2L4.6 15.6a1.6 1.6 0 0 1 2.4-2.1L8 14.5"/>',
    zap: '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>',
    chevronDown: '<path d="M6 9l6 6 6-6"/>',
    chevronRight: '<path d="M9 6l6 6-6 6"/>',
    chevronUp: '<path d="M6 15l6-6 6 6"/>',
    sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    alert: '<path d="M10.3 4.2L2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    fileText: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="M21 16l-5-5-9 9"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
    pencil: '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z"/><path d="M14.5 5.5l3 3"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
    retry: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    compress: '<path d="M8 4l4 4 4-4M8 20l4-4 4 4M4 12h16"/>',
    power: '<path d="M12 3v9"/><path d="M6.3 7.5a8 8 0 1 0 11.4 0"/>',
    cursor: '<path d="M5 3l14 7-6.2 1.8L11 18z"/><path d="M13 12l6 6"/>',
    keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M7.5 14h9"/>',
    code: '<path d="M8 8l-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"/>',
    sparkle: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    home: '<path d="M4 11l8-7 8 7v8a1 1 0 0 1-1 1h-4v-6h-6v6H5a1 1 0 0 1-1-1z"/>',
    drive: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M5 13l2.5-8h9L19 13M7 16.5h.01"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6L6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
    monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M16 7l3 3M14 9l2 2"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    bot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9.5 16.5h5"/>',
    ban: '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
    square: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    dot: '<circle cx="12" cy="12" r="2.5" fill="currentColor"/>',
    bookmark: '<path d="M6 3h12v18l-6-4-6 4z"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    clipboard: '<rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4V3h6v1M9 10h6M9 14h6"/>',
    question: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 0 1 4.9.7c0 1.7-2.4 2.3-2.4 3.8M12 17h.01"/>',
    plug: '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
  };

  function svg(name) {
    return `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.dot}</svg>`;
  }

  const iconTemplate = document.createElement("template");
  function ico(name) {
    iconTemplate.innerHTML = svg(name);
    return iconTemplate.content.firstChild;
  }

  function setIcon(node, name) {
    const old = node.querySelector(":scope > svg.i");
    if (old) old.replaceWith(ico(name));
    else node.prepend(ico(name));
  }

  /** Build an element. `text` sets textContent; `html` is only used for server-sanitized markup. */
  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key in attrs) {
        const value = attrs[key];
        if (value == null || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "html") node.innerHTML = value;
        else if (key === "icon") node.append(ico(value));
        else if (key === "value") node.value = value;
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? "" : String(value));
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      node.append(kid instanceof Node ? kid : String(kid));
    }
    return node;
  }

  async function api(path, body) {
    const options = { headers: { "X-Kimi-Agent": KEY }, credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, options);
    } catch (e) {
      throw new Error("Kimi Agent is not responding.");
    }
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      if (res.status === 401) showSignedOut();
      const err = new Error((data && data.error) || `Request failed (${res.status}).`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /** Like api() but shows failures as a toast and resolves to null. */
  async function call(path, body) {
    try {
      return await api(path, body);
    } catch (e) {
      toast(e.message, true);
      return null;
    }
  }

  function toast(text, error) {
    const node = h("div", { class: "toast" + (error ? " error" : ""), role: error ? "alert" : "status", text });
    $("toasts").append(node);
    setTimeout(() => node.remove(), error ? 6500 : 2600);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      const area = h("textarea", { style: "position:fixed;top:-100px;opacity:0" });
      area.value = text;
      document.body.append(area);
      area.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (e2) {
        ok = false;
      }
      area.remove();
      return ok;
    }
  }

  const fmtSize = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);
  const fmtSecs = (s) => {
    s = Math.max(0, s || 0);
    if (s < 10) return `${s < 1 ? s.toFixed(1) : Math.floor(s)}s`;
    if (s < 60) return `${Math.floor(s)}s`;
    return `${Math.floor(s / 60)}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
  };
  const basename = (p) => {
    const parts = String(p || "").split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : String(p || "");
  };
  const shortModel = (m) => String(m || "").split("/").pop();
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  const isTyping = (t) => t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);

  // ------------------------------------------------------------------ labels
  // [while running, when finished, icon]
  const TOOLS = {
    read_file: ["Reading", "Read", "file"],
    write_file: ["Writing", "Wrote", "pencil"],
    edit_file: ["Editing", "Edited", "pencil"],
    list_directory: ["Listing", "Listed", "folder"],
    find_files: ["Finding files", "Found files", "search"],
    search_files: ["Searching", "Searched", "search"],
    delete_path: ["Deleting", "Deleted", "trash"],
    run_command: ["Running", "Ran", "terminal"],
    start_process: ["Starting", "Started", "terminal"],
    process_output: ["Checking output", "Checked output", "activity"],
    process_input: ["Sending input", "Sent input", "keyboard"],
    stop_process: ["Stopping process", "Stopped process", "square"],
    list_processes: ["Listing processes", "Listed processes", "activity"],
    run_python: ["Running Python", "Ran Python", "code"],
    web_fetch: ["Reading", "Read", "globe"],
    web_search: ["Searching the web", "Searched the web", "globe"],
    $web_search: ["Searching the web", "Searched the web", "globe"],
    remember: ["Remembering", "Remembered", "bookmark"],
    forget: ["Forgetting", "Forgot", "bookmark"],
    delegate_task: ["Working on a sub-task", "Sub-task", "bot"],
    ask_user: ["Asking", "Asked", "question"],
    update_plan: ["Planning", "Updated plan", "list"],
    screenshot: ["Looking at the screen", "Looked at the screen", "monitor"],
    mouse_click: ["Clicking", "Clicked", "cursor"],
    mouse_move: ["Moving the mouse", "Moved the mouse", "cursor"],
    mouse_drag: ["Dragging", "Dragged", "cursor"],
    scroll: ["Scrolling", "Scrolled", "cursor"],
    type_text: ["Typing", "Typed", "keyboard"],
    press_keys: ["Pressing", "Pressed", "keyboard"],
    clipboard_read: ["Reading the clipboard", "Read the clipboard", "clipboard"],
    clipboard_write: ["Copying", "Copied", "clipboard"],
    open_target: ["Opening", "Opened", "external"],
  };
  const CATEGORY_ICON = { read: "file", write: "pencil", execute: "terminal", network: "globe", gui: "cursor", external: "plug", internal: "sparkle" };
  const APPROVAL_VERB = {
    run_command: "run this command", start_process: "start this program", process_input: "send this input",
    write_file: "write this file", edit_file: "edit this file", delete_path: "delete this",
    run_python: "run this Python code", web_fetch: "open this web page", web_search: "search the web",
    open_target: "open this", type_text: "type this text", press_keys: "press these keys", mouse_click: "click here",
    mouse_drag: "drag the mouse", scroll: "scroll", mouse_move: "move the mouse", clipboard_read: "read your clipboard",
    clipboard_write: "change your clipboard", delegate_task: "start a sub-agent",
  };
  const CATEGORY_VERB = {
    read: "read this", write: "make this change", execute: "run this", network: "go online",
    gui: "control your computer", external: "use this connected tool", internal: "do this",
  };
  const RISK_LABEL = { safe: "Low risk", dangerous: "Risky", catastrophic: "Destructive" };
  const PLACE_ICON = { Desktop: "monitor", Documents: "file", Downloads: "download" };
  const STATUS_ICON = { pending: "dot", approval: "hand", done: "check", error: "x", denied: "ban", stopped: "square" };
  const STATUS_TEXT = {
    pending: "Waiting", approval: "Needs your approval", running: "Running", done: "Done", error: "Failed",
    denied: "Denied", stopped: "Stopped",
  };
  const ACTIVE = new Set(["pending", "approval", "running"]);
  const NOTICE_ICON = { info: "info", success: "check", warn: "alert", error: "alert", stopped: "square" };
  const APPROVAL = {
    ask: { label: "Ask first", icon: "hand", desc: "Ask before every command, file change or computer action." },
    auto: { label: "Auto", icon: "shield", desc: "Run safe commands and edit files in this folder; ask before anything else." },
    yolo: { label: "Full access", icon: "zap", desc: "Do everything without asking, except destructive commands." },
  };
  const PROVIDER_NAMES = {
    kimi: "Kimi (Moonshot AI)", "kimi-cn": "Kimi China (Moonshot AI)", "kimi-code": "Kimi Code subscription",
    openrouter: "OpenRouter", huggingface: "Hugging Face", groq: "Groq", together: "Together AI",
    fireworks: "Fireworks AI", deepseek: "DeepSeek", ollama: "Ollama (on this computer)", vllm: "vLLM server",
    sglang: "SGLang server", lmstudio: "LM Studio (on this computer)", llamacpp: "llama.cpp server",
    custom: "Other OpenAI-compatible server",
  };
  const EFFORTS = [["", "Auto"], ["low", "Low"], ["medium", "Medium"], ["high", "High"], ["max", "Max"]];
  const STARTERS = [
    ["folder", "Organize a folder", "Look through the files in this folder and sort them into sensible subfolders. Show me the plan before moving anything."],
    ["code", "Write a script", "Write a Python script that |"],
    ["globe", "Research a topic", "Research the latest news about | and give me a short summary with links to your sources."],
    ["fileText", "Summarize a document", "Summarize the attached document in a few bullet points.", "attach"],
    ["monitor", "Check my computer", "How much free disk space and memory does my computer have? Find the biggest folders in my home folder and suggest what I could clean up. Don't delete anything."],
  ];

  function toolInfo(item) {
    const known = TOOLS[item.name];
    if (known) return known;
    const pretty = String(item.name || "tool").replace(/^mcp__/, "").replace(/__/g, " › ").replace(/_/g, " ");
    return [pretty, pretty, CATEGORY_ICON[item.category] || "plug"];
  }

  // ------------------------------------------------------------------ state
  const S = {
    state: {}, items: new Map(), runningTools: new Set(), sessions: [], files: [], modals: [],
    running: false, empty: null, stick: true, scrollQueued: false, runStart: 0, status: "",
    closed: false, setupShown: false, modelCache: {}, loaded: false,
  };
  const el = {};
  for (const id of ["thread", "scroller", "input", "send", "files", "tail", "tail-text", "tail-time", "jump", "dock",
    "welcome-slot", "welcome", "title", "recents", "folder-btn", "folder-name", "approval-btn", "approval-name",
    "model-btn", "model-name", "menu-btn", "ctx", "banner", "conn", "turn-actions", "copy-last", "retry-last",
    "composer", "file-input", "attach", "drop", "layer", "greeting", "chips", "scrim"]) {
    el[id.replace(/-(\w)/g, (_, c) => c.toUpperCase())] = $(id);
  }
  const mainEl = document.querySelector(".main");

  // ------------------------------------------------------------------ transcript
  const RENDER = {
    user: renderUser, reasoning: renderReasoning, assistant: renderAssistant, tool: renderTool,
    prompt: renderPrompt, plan: renderPlan, notice: renderNotice,
  };

  function containerFor(item) {
    const parent = item.parent && S.items.get(item.parent);
    return (parent && parent.children) || el.thread;
  }

  function upsert(item) {
    const render = RENDER[item.type];
    if (!render || !item.id) return;
    let rec = S.items.get(item.id);
    if (item.type === "reasoning" && "delta" in item) {
      item = Object.assign({}, item, { text: (rec ? rec.item.text || "" : "") + (item.delta || "") });
      delete item.delta;
    }
    if (!rec) {
      rec = { item, el: h("div", { class: "item " + item.type }), ui: {}, children: null };
      rec.el.dataset.id = item.id;
      S.items.set(item.id, rec);
      containerFor(item).append(rec.el);
    } else {
      rec.item = item;
      if (item.type === "plan") containerFor(item).append(rec.el); // the newest plan moves to the end
    }
    render(rec);
    if (item.type === "user") markLastUser();
    setEmpty(false);
    updateTurnActions();
    pinScroll();
  }

  function resetThread(ev) {
    S.items.clear();
    S.runningTools.clear();
    el.thread.replaceChildren();
    S.stick = true;
    applyState(ev.state || {});
    setEmpty(!ev.count && !S.running);
    if (!S.loaded) {
      S.loaded = true;
      document.body.classList.remove("loading");
      if (!S.state.needs_setup) el.input.focus();
    }
    pinScroll();
  }

  function markLastUser() {
    const users = el.thread.querySelectorAll(":scope > .user");
    users.forEach((node, i) => node.classList.toggle("last-user", i === users.length - 1));
  }

  function renderUser(rec) {
    const it = rec.item;
    const files = (it.files || []).map((f) => (f.image
      ? h("img", { class: "thumb", src: f.image, alt: f.name || "image", title: f.name || "" })
      : h("span", { class: "file-pill", icon: "file", title: f.name || "" }, h("span", { text: f.name || "file" }))));
    rec.el.replaceChildren(h("div", { class: "user-col" },
      files.length ? h("div", { class: "user-files" }, files) : null,
      it.text ? h("div", { class: "bubble", text: it.text }) : null,
      h("div", { class: "msg-actions" },
        it.text ? h("button", {
          class: "icon-btn small", icon: "copy", title: "Copy",
          onclick: () => copyText(it.text).then((ok) => ok && toast("Copied")),
        }) : null,
        h("button", { class: "icon-btn small edit-btn", icon: "pencil", title: "Edit and resend", onclick: editLast }))));
  }

  function thoughtLabel(it) {
    if (!it.done) return "Thinking…";
    if (it.seconds == null) return "Thought process";
    return it.seconds < 2 ? "Thought for a moment" : `Thought for ${fmtSecs(it.seconds)}`;
  }

  function renderReasoning(rec) {
    const it = rec.item, ui = rec.ui;
    if (!ui.head) {
      ui.label = h("span", { class: "think-label" });
      ui.preview = h("span", { class: "think-preview" });
      ui.body = h("div", { class: "think-body" });
      ui.head = h("button", {
        class: "think-head", "aria-expanded": "false",
        onclick: () => {
          const open = rec.el.classList.toggle("open");
          ui.head.setAttribute("aria-expanded", String(open));
          if (open) ui.body.textContent = rec.item.text || "";
        },
      }, ico("sparkle"), ui.label, ui.preview, h("span", { class: "chev", icon: "chevronRight" }));
      rec.el.append(ui.head, ui.body);
    }
    rec.el.classList.toggle("live", !it.done);
    ui.label.textContent = thoughtLabel(it);
    if (!it.done) {
      const lines = (it.text || "").trim().split("\n");
      ui.preview.textContent = lines[lines.length - 1].slice(-160);
    }
    if (rec.el.classList.contains("open")) ui.body.textContent = it.text || "";
  }

  function renderAssistant(rec) {
    const it = rec.item, ui = rec.ui;
    if (!ui.md) {
      ui.md = h("div", { class: "md" });
      rec.el.append(ui.md);
    }
    if (ui.html !== it.html) {
      ui.md.innerHTML = it.html || "";
      ui.html = it.html;
    }
    rec.el.classList.toggle("streaming", !!it.streaming);
  }

  function renderTool(rec) {
    const it = rec.item, ui = rec.ui;
    const [doing, done, iconName] = toolInfo(it);
    if (!ui.head) {
      ui.label = h("span", { class: "tool-label" });
      ui.summary = h("span", { class: "tool-summary" });
      ui.meta = h("span", { class: "tool-meta" });
      ui.status = h("span", { class: "tool-status" });
      ui.head = h("button", { class: "tool-head", "aria-expanded": "false", onclick: () => toggleTool(rec) },
        h("span", { class: "tool-icon", icon: iconName }), ui.label, ui.summary, ui.meta, ui.status,
        h("span", { class: "chev", icon: "chevronRight" }));
      ui.body = h("div", { class: "tool-body" });
      ui.shots = h("div", { class: "tool-shots" });
      rec.children = h("div", { class: "children" });
      rec.el.append(ui.head, ui.body, ui.shots, rec.children);
    }
    const open = rec.el.classList.contains("open");
    rec.el.className = `item tool status-${it.status}${open ? " open" : ""}`;
    rec.el.hidden = !!it.hidden;
    ui.label.textContent = ACTIVE.has(it.status) ? doing : done;
    ui.summary.textContent = it.summary || "";
    ui.head.title = `${it.name}${it.summary ? ": " + it.summary : ""}`;
    if (ui.statusKey !== it.status) {
      ui.statusKey = it.status;
      ui.status.replaceChildren(it.status === "running" ? h("span", { class: "spin" }) : ico(STATUS_ICON[it.status] || "dot"));
      ui.status.title = STATUS_TEXT[it.status] || "";
    }
    if (it.status === "running") S.runningTools.add(rec);
    else S.runningTools.delete(rec);
    updateToolTime(rec);
    const images = it.images || [];
    const key = images.join("|");
    if (ui.imagesKey !== key) {
      ui.imagesKey = key;
      ui.shots.replaceChildren(...images.map((src) => h("img", { class: "thumb", src, alt: "Screenshot" })));
      ui.shots.hidden = !images.length;
    }
    if (open) renderToolBody(rec);
  }

  function updateToolTime(rec) {
    const it = rec.item;
    let text = "";
    if (it.status === "running" && it.started) text = fmtSecs(Date.now() / 1000 - it.started);
    else if (typeof it.elapsed === "number" && it.elapsed >= 0.5 && !ACTIVE.has(it.status)) text = fmtSecs(it.elapsed);
    if (rec.ui.meta.textContent !== text) rec.ui.meta.textContent = text;
  }

  function toggleTool(rec) {
    const open = rec.el.classList.toggle("open");
    rec.ui.head.setAttribute("aria-expanded", String(open));
    if (open) renderToolBody(rec);
  }

  function renderToolBody(rec) {
    const it = rec.item;
    const key = [it.status, it.output, it.args, it.command].join("\u0000");
    if (rec.ui.bodyKey === key) return;
    rec.ui.bodyKey = key;
    const parts = [];
    if (it.command) {
      parts.push(h("div", { class: "sec-label", text: "Command" }), h("pre", { class: "out term", text: it.command }));
    } else if (it.args && it.args.trim() !== "{}") {
      parts.push(h("div", { class: "sec-label", text: "Input" }), h("pre", { class: "out", text: it.args }));
    }
    if (it.output) {
      const failed = it.status === "error" || it.status === "denied";
      parts.push(h("div", { class: "sec-label", text: failed ? "Result" : "Output" }),
        h("pre", { class: "out" + (failed ? " err" : ""), text: it.output }));
    } else {
      const note = it.status === "approval" ? "Waiting for your approval…" : ACTIVE.has(it.status) ? "Running…"
        : it.status === "stopped" ? "Stopped before it finished." : "No output.";
      parts.push(h("div", { class: "out-empty", text: note }));
    }
    rec.ui.body.replaceChildren(...parts);
  }

  async function respond(rec, answer, card) {
    const controls = card.querySelectorAll("button, textarea, input");
    controls.forEach((c) => { c.disabled = true; });
    const ok = await call("/api/respond", { id: rec.item.id, answer });
    if (!ok) controls.forEach((c) => { c.disabled = false; });
  }

  function renderPrompt(rec) {
    const it = rec.item;
    const key = JSON.stringify(it.resolved === undefined ? null : it.resolved);
    if (rec.ui.key === key) return;
    rec.ui.key = key;
    rec.el.classList.toggle("done", !!it.resolved);
    const build = it.kind === "approval" ? (it.resolved ? approvalDone : approvalCard)
      : it.kind === "question" ? questionCard : confirmCard;
    rec.el.replaceChildren(build(rec));
  }

  function approvalCard(rec) {
    const it = rec.item;
    const verb = APPROVAL_VERB[it.name] || CATEGORY_VERB[it.category] || "use this tool";
    const always = String(it.always || "").replace(/`/g, "");
    const feedback = h("textarea", {
      class: "feedback", rows: "1", "aria-label": "Instructions instead",
      placeholder: "Or tell Kimi Agent what to do instead…",
    });
    const denyBtn = h("button", { class: "btn", text: "Deny" });
    const card = h("div", { class: "card waiting" },
      h("div", { class: "card-head" },
        h("span", { class: "card-icon", icon: toolInfo(it)[2] }),
        h("div", { class: "card-title", text: `Allow Kimi Agent to ${verb}?` }),
        RISK_LABEL[it.risk] ? h("span", { class: `risk risk-${it.risk}`, text: RISK_LABEL[it.risk] }) : null),
      it.reason ? h("div", { class: "card-reason", text: capitalize(it.reason) + "." }) : null,
      h("div", { html: it.preview || "" }),
      feedback);
    const answer = (choice) => respond(rec, { answer: choice, feedback: choice === "no" ? feedback.value.trim() : "" }, card);
    feedback.addEventListener("input", () => {
      denyBtn.textContent = feedback.value.trim() ? "Deny and send note" : "Deny";
    });
    feedback.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && feedback.value.trim()) {
        e.preventDefault();
        answer("no");
      }
    });
    denyBtn.addEventListener("click", () => answer("no"));
    card.append(h("div", { class: "card-actions" },
      denyBtn,
      always ? h("button", {
        class: "btn", text: "Always allow", title: `Don't ask again in this conversation for ${always}`,
        onclick: () => answer("always"),
      }) : null,
      h("button", { class: "btn primary", text: "Allow once", onclick: () => answer("yes") })));
    return card;
  }

  function approvalDone(rec) {
    const it = rec.item, res = it.resolved || {};
    let iconName = "check", text = "Allowed", extra = "", bad = false;
    if (res.cancelled) {
      iconName = "square"; text = "Cancelled";
    } else if (res.answer === "always") {
      text = "Always allowed"; extra = String(it.always || "").replace(/`/g, "");
    } else if (res.answer !== "yes") {
      iconName = "ban"; text = "Denied"; bad = true;
      extra = res.feedback ? `“${res.feedback}”` : "";
    }
    return h("div", { class: "resolved-line" + (bad ? " bad" : ""), title: extra },
      ico(iconName), h("span", { text }), extra ? h("span", { class: "rl-extra", text: extra }) : null);
  }

  function questionCard(rec) {
    const it = rec.item, res = it.resolved;
    const card = h("div", { class: "card" + (res ? "" : " waiting") },
      h("div", { class: "card-head" },
        h("span", { class: "card-icon", icon: "question" }),
        h("div", { class: "card-title", text: "Kimi Agent has a question" })),
      h("div", { class: "md", html: it.question || "" }));
    if (res) {
      card.append(h("div", { class: "answered" },
        h("b", { text: res.cancelled ? "Skipped" : "You answered" }), res.cancelled ? "" : String(res.answer ?? "")));
      return card;
    }
    const choices = it.choices || [];
    const input = h("input", { class: "input", placeholder: choices.length ? "Or type a different answer…" : "Type your answer…" });
    const submit = (value) => value && respond(rec, { answer: value }, card);
    if (choices.length) {
      card.append(h("div", { class: "choices" }, choices.map((c, i) => h("button", { class: "choice", onclick: () => submit(c) },
        h("span", { class: "choice-n", text: String(i + 1) }), h("span", { text: c })))));
    }
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        submit(input.value.trim());
      }
    });
    card.append(h("div", { class: "answer-row" }, input,
      h("button", { class: "btn primary", text: "Send", onclick: () => submit(input.value.trim()) })));
    if (!choices.length) setTimeout(() => { if (!isTyping(document.activeElement)) input.focus(); }, 50);
    return card;
  }

  function confirmCard(rec) {
    const it = rec.item, res = it.resolved;
    const card = h("div", { class: "card" + (res ? "" : " waiting") },
      h("div", { class: "card-head" },
        h("span", { class: "card-icon", icon: "question" }),
        h("div", { class: "card-title", text: it.question || "Continue?" })));
    if (res) {
      card.append(h("div", { class: "answered" }, h("b", { text: "You answered" }),
        res.cancelled ? "Skipped" : res.answer ? "Yes" : "No"));
      return card;
    }
    card.append(h("div", { class: "card-actions" },
      h("button", { class: "btn", text: "No", onclick: () => respond(rec, { answer: false }, card) }),
      h("button", { class: "btn primary", text: "Yes", onclick: () => respond(rec, { answer: true }, card) })));
    return card;
  }

  function renderPlan(rec) {
    const it = rec.item;
    const steps = (it.steps || []).map((s) => ({ ...s, status: s.status === "completed" ? "done" : s.status }));
    const done = steps.filter((s) => s.status === "done").length;
    rec.el.replaceChildren(h("div", { class: "card" },
      h("div", { class: "plan-head" }, ico("list"), h("span", { text: "Plan" }),
        h("span", { class: "plan-count", text: steps.length ? `${done} of ${steps.length} done` : "" })),
      it.explanation ? h("div", { class: "plan-expl", text: it.explanation }) : null,
      h("ol", { class: "steps" }, steps.map((s) => h("li", { class: "step " + (s.status || "pending") },
        h("span", { class: "step-mark" }, s.status === "done" ? ico("check") : null),
        h("span", { class: "step-text", text: s.step }))))));
  }

  function renderNotice(rec) {
    const it = rec.item, level = it.level || "info";
    rec.el.className = "item notice level-" + level;
    rec.el.replaceChildren(h("span", { class: "n-icon", icon: NOTICE_ICON[level] || "info" }),
      h("span", { class: "n-text", text: it.text || "" }));
  }

  // ------------------------------------------------------------------ app state
  function folderLabel(path, home) {
    if (!path) return "Folder";
    return path === home ? "Home" : basename(path);
  }

  function applyState(st) {
    const prev = S.state;
    S.state = st || {};
    S.running = !!S.state.running;
    S.status = S.state.status || "";
    document.body.classList.toggle("running", S.running);
    if (S.running && (!prev.running || !S.runStart)) S.runStart = Date.now();
    el.title.textContent = S.state.title || "New chat";
    document.title = (S.state.attention ? "● " : "") + (S.state.title ? `${S.state.title} – Kimi Agent` : "Kimi Agent");
    el.folderName.textContent = folderLabel(S.state.workdir, S.state.home);
    el.folderBtn.title = `Working folder: ${S.state.workdir || ""}`;
    const mode = APPROVAL[S.state.approval] || APPROVAL.auto;
    el.approvalName.textContent = mode.label;
    setIcon(el.approvalBtn, mode.icon);
    el.approvalBtn.classList.toggle("danger", S.state.approval === "yolo");
    el.approvalBtn.title = `Permissions: ${mode.desc}`;
    el.modelName.textContent = shortModel(S.state.model) || "Choose a model";
    el.modelBtn.title = `${PROVIDER_NAMES[S.state.provider] || S.state.provider || ""} · ${S.state.model || "no model"}`;
    renderCtx(S.state.context_tokens, S.state.context_window);
    if (el.version) el.version.textContent = S.state.version ? `v${S.state.version}` : "";
    if (S.state.theme && window.applyTheme && S.state.theme !== prev.theme) window.applyTheme(S.state.theme);
    renderBanner();
    updateComposer();
    updateTail();
    updateTurnActions();
    highlightSession();
    if (S.empty && !S.running && !el.thread.childElementCount) setEmpty(true);
    if (S.state.ready && S.state.needs_setup && !S.setupShown && !S.state.start_error) {
      S.setupShown = true;
      openSetup();
    }
  }

  function renderBanner() {
    const st = S.state;
    let text = "", error = false;
    if (st.start_error) {
      text = `Kimi Agent could not start: ${st.start_error}`;
      error = true;
    } else if (st.config_error) {
      text = `There is a problem with your settings file, so defaults are used: ${st.config_error}`;
    }
    el.banner.hidden = !text;
    if (!text) return;
    el.banner.classList.toggle("error", error);
    el.banner.replaceChildren(text, " ", h("button", { text: "Open settings", onclick: openSettings }));
  }

  function renderCtx(tokens, windowSize) {
    const pct = tokens && windowSize ? Math.min(1, tokens / windowSize) : 0;
    // Like Claude, stay out of the way until the conversation is getting long.
    if (pct < 0.5) {
      el.ctx.hidden = true;
      return;
    }
    const c = 2 * Math.PI * 7;
    el.ctx.hidden = false;
    el.ctx.innerHTML = `<svg viewBox="0 0 18 18" aria-hidden="true"><circle class="track" cx="9" cy="9" r="7"/>` +
      `<circle class="fill" cx="9" cy="9" r="7" stroke-dasharray="${(pct * c).toFixed(2)} ${c.toFixed(2)}"/></svg>`;
    el.ctx.classList.toggle("high", pct > 0.75);
    el.ctx.title = `Context used: ${tokens.toLocaleString()} of ${windowSize.toLocaleString()} tokens ` +
      `(${Math.round(pct * 100)}%). Older messages are summarized automatically when it fills up.`;
  }

  function setEmpty(empty) {
    if (empty === S.empty) return;
    S.empty = empty;
    document.body.classList.toggle("empty", empty);
    const focused = document.activeElement === el.input;
    if (empty) el.welcomeSlot.append(el.dock);
    else mainEl.append(el.dock);
    if (focused) el.input.focus();
    el.jump.hidden = true;
    if (empty) greet();
    updateComposer();
  }

  function updateTail() {
    el.tail.hidden = !S.running;
    if (!S.running) return;
    const waiting = !!S.state.attention;
    el.tail.classList.toggle("waiting", waiting);
    el.tailText.textContent = waiting ? "Waiting for you…" : S.status || "Working…";
    tick();
  }

  function tick() {
    if (S.running) el.tailTime.textContent = fmtSecs((Date.now() - S.runStart) / 1000);
    for (const rec of S.runningTools) updateToolTime(rec);
  }

  function lastAssistantText() {
    const nodes = el.thread.children;
    const parts = [];
    for (let i = nodes.length - 1; i >= 0; i--) {
      const node = nodes[i];
      if (node.classList.contains("user")) break;
      if (node.classList.contains("assistant")) {
        const rec = S.items.get(node.dataset.id);
        if (rec && rec.item.text) parts.unshift(rec.item.text);
      }
    }
    return parts.join("\n\n");
  }

  function updateTurnActions() {
    const show = !S.running && !!S.state.can_undo && el.thread.childElementCount > 0;
    el.turnActions.hidden = !show;
    if (show) el.copyLast.hidden = !lastAssistantText();
  }

  // ------------------------------------------------------------------ scrolling
  function pinScroll() {
    if (!S.stick || S.scrollQueued) return;
    S.scrollQueued = true;
    requestAnimationFrame(() => {
      S.scrollQueued = false;
      if (S.stick) el.scroller.scrollTop = el.scroller.scrollHeight;
    });
  }

  el.scroller.addEventListener("scroll", () => {
    const gap = el.scroller.scrollHeight - el.scroller.scrollTop - el.scroller.clientHeight;
    S.stick = gap < 90;
    el.jump.hidden = S.stick || !!S.empty;
  }, { passive: true });
  el.thread.addEventListener("load", (e) => { if (e.target.tagName === "IMG") pinScroll(); }, true);
  el.jump.addEventListener("click", () => {
    S.stick = true;
    el.scroller.scrollTo({ top: el.scroller.scrollHeight, behavior: "smooth" });
  });

  // ------------------------------------------------------------------ composer
  let fileSeq = 0;

  function autosize() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight + 2, Math.round(window.innerHeight * 0.42)) + "px";
  }

  function updateComposer() {
    const st = S.state;
    if (S.running) {
      if (!el.send.classList.contains("stop")) {
        el.send.classList.add("stop");
        setIcon(el.send, "stop");
      }
      el.send.disabled = false;
      el.send.title = "Stop (Esc)";
      return;
    }
    if (el.send.classList.contains("stop")) {
      el.send.classList.remove("stop");
      setIcon(el.send, "arrowUp");
    }
    const uploading = S.files.some((f) => f.uploading);
    const hasContent = !!el.input.value.trim() || S.files.some((f) => f.path);
    el.send.disabled = !st.ready || !!st.start_error || uploading || !hasContent;
    el.send.title = uploading ? "Waiting for files to upload…" : "Send (Enter)";
    el.input.placeholder = !st.ready && !st.start_error ? "Starting Kimi Agent…"
      : S.empty ? "How can I help you today?" : "Reply to Kimi Agent…";
  }

  async function submit() {
    if (S.running) {
      stopRun();
      return;
    }
    updateComposer();
    if (el.send.disabled) return;
    if (S.state.needs_setup) {
      openSetup();
      return;
    }
    const text = el.input.value;
    const files = S.files.filter((f) => f.path);
    el.send.disabled = true;
    S.stick = true;
    const ok = await call("/api/send", { text, attachments: files.map((f) => ({ path: f.path, name: f.name })) });
    if (ok) {
      el.input.value = "";
      clearFiles();
      saveDraft();
      autosize();
    }
    updateComposer();
  }

  function stopRun() {
    call("/api/stop", {});
  }

  function saveDraft() {
    try {
      sessionStorage.setItem("kimi.draft", el.input.value);
    } catch (e) {
      /* storage may be unavailable */
    }
  }

  function clearFiles() {
    for (const f of S.files) if (f.local) URL.revokeObjectURL(f.local);
    S.files = [];
    renderFiles();
  }

  function removeFile(f) {
    if (f.local) URL.revokeObjectURL(f.local);
    S.files = S.files.filter((x) => x !== f);
    renderFiles();
    updateComposer();
  }

  function addFiles(list) {
    for (const file of Array.from(list || [])) {
      if (S.files.length >= 20) {
        toast("You can attach up to 20 files to a message.", true);
        break;
      }
      if (file.size > 50 * 1024 * 1024) {
        toast(`${file.name} is larger than 50 MB.`, true);
        continue;
      }
      const isImage = /^image\/(png|jpe?g|gif|webp|bmp)$/.test(file.type);
      const f = {
        key: ++fileSeq, name: file.name || "pasted-image.png", size: file.size, uploading: true, path: "", image: null,
        local: isImage ? URL.createObjectURL(file) : null,
      };
      S.files.push(f);
      upload(f, file);
    }
    renderFiles();
    updateComposer();
  }

  async function upload(f, file) {
    try {
      const res = await fetch("/api/upload?name=" + encodeURIComponent(f.name), {
        method: "POST", body: file, credentials: "same-origin",
        headers: { "X-Kimi-Agent": KEY, "Content-Type": "application/octet-stream" },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `upload failed (${res.status})`);
      Object.assign(f, { path: data.path, image: data.image, uploading: false });
    } catch (e) {
      toast(`Could not attach ${f.name}: ${e.message}`, true);
      if (f.local) URL.revokeObjectURL(f.local);
      S.files = S.files.filter((x) => x !== f);
    }
    renderFiles();
    updateComposer();
  }

  function renderFiles() {
    el.files.replaceChildren(...S.files.map((f) => {
      const src = f.local || f.image;
      const chip = src
        ? h("div", { class: "file-chip img-only" + (f.uploading ? " uploading" : ""), title: f.name }, h("img", { src, alt: f.name }))
        : h("div", { class: "file-chip" + (f.uploading ? " uploading" : ""), title: f.name },
          h("span", { class: "fi" }, f.uploading ? h("span", { class: "spin" }) : ico("file")),
          h("span", { class: "fc-text" }, h("span", { class: "fc-name", text: f.name }),
            h("span", { class: "fc-size", text: f.uploading ? "Uploading…" : f.size ? fmtSize(f.size) : "" })));
      chip.append(h("button", { class: "fc-x", type: "button", icon: "x", title: "Remove", "aria-label": `Remove ${f.name}`, onclick: () => removeFile(f) }));
      return chip;
    }));
  }

  async function editLast() {
    const result = await call("/api/undo", {});
    if (!result) return;
    el.input.value = result.text || "";
    clearFiles();
    S.files = (result.files || []).map((f) => ({ key: ++fileSeq, name: f.name, path: f.path, image: f.image, size: 0 }));
    renderFiles();
    autosize();
    updateComposer();
    el.input.focus();
    el.input.setSelectionRange(el.input.value.length, el.input.value.length);
    const restored = result.restored || [];
    if (restored.length) toast(`Undid the changes to ${plural(restored.length, "file")}.`);
  }

  // ------------------------------------------------------------------ connection
  let source = null, downSince = 0;

  function handle(ev) {
    switch (ev.type) {
      case "reset": resetThread(ev); break;
      case "state": applyState(ev.state || {}); break;
      case "status":
        S.status = ev.status || "";
        updateTail();
        break;
      case "sessions": loadSessionsSoon(); break;
      case "shutdown": showClosed(true); break;
      default: upsert(ev);
    }
  }

  function connect() {
    if (S.closed) return;
    source = new EventSource("/api/events?key=" + encodeURIComponent(KEY));
    source.onopen = () => setConnected(true);
    source.onmessage = (e) => {
      let ev;
      try {
        ev = JSON.parse(e.data);
      } catch (err) {
        return;
      }
      try {
        handle(ev);
      } catch (err) {
        console.error("Kimi Agent: could not handle event", ev, err);
      }
    };
    source.onerror = () => {
      setConnected(false);
      if (source && source.readyState === EventSource.CLOSED) {
        source = null;
        checkSignedIn();
      }
    };
  }

  async function checkSignedIn() {
    try {
      const res = await fetch("/api/state", { headers: { "X-Kimi-Agent": KEY }, cache: "no-store" });
      if (res.status === 401) {
        showSignedOut();
        return;
      }
    } catch (e) {
      /* not running: keep trying */
    }
    setTimeout(connect, 2000);
  }

  function setConnected(ok) {
    if (ok) {
      downSince = 0;
      el.conn.hidden = true;
      if (S.closedModal && !S.closed) {
        S.closedModal.close();
        S.closedModal = null;
      }
      return;
    }
    if (S.closed) return;
    if (!downSince) downSince = Date.now();
    el.conn.hidden = false;
  }

  setInterval(() => {
    if (downSince && !S.closed && !S.closedModal && Date.now() - downSince > 8000) {
      S.closedModal = modal({
        title: "Kimi Agent isn't responding", closable: false,
        body: [h("p", { text: "The app may have been closed. Open Kimi Agent again from its shortcut, or wait while this window tries to reconnect." }),
          h("div", { class: "closed-note" }, h("span", { class: "spin" }), "Reconnecting…")],
      });
    }
  }, 2000);

  function showClosed(quit) {
    if (S.closed) return;
    S.closed = true;
    if (source) source.close();
    source = null;
    if (S.closedModal) S.closedModal.close();
    closeAllModals();
    modal({
      title: quit ? "Kimi Agent has quit" : "Kimi Agent was closed", closable: false,
      body: h("p", { text: "You can close this window. To continue, open Kimi Agent again from its shortcut." }),
    });
    if (quit) setTimeout(() => window.close(), 400);
  }

  function showSignedOut() {
    if (S.closed) return;
    S.closed = true;
    if (source) source.close();
    source = null;
    closeAllModals();
    modal({
      title: "This window is signed out", closable: false,
      body: h("p", { text: "Kimi Agent was restarted, or this window wasn't opened by it. Open Kimi Agent again from its shortcut (or run kimi-agent --gui) to continue." }),
    });
  }

  // ------------------------------------------------------------------ sidebar
  let sessionsTimer = 0, sessionsKey = "";

  function loadSessionsSoon() {
    clearTimeout(sessionsTimer);
    sessionsTimer = setTimeout(loadSessions, 120);
  }

  async function loadSessions() {
    try {
      const data = await api("/api/sessions");
      S.sessions = (data && data.sessions) || [];
    } catch (e) {
      return;
    }
    renderSessions();
  }

  function sessionTooltip(s) {
    const when = s.updated ? new Date(s.updated) : null;
    return [s.title || "Untitled", s.cwd ? `Folder: ${s.cwd}` : "",
      when && !isNaN(when) ? `Last used: ${when.toLocaleString()}` : ""].filter(Boolean).join("\n");
  }

  function renderSessions() {
    const cur = S.state.session_id || "";
    const hasThread = el.thread.childElementCount > 0;
    sessionsKey = [cur, S.state.title, hasThread].join("\u0000");
    const list = S.sessions.slice();
    if (cur && hasThread && !list.some((s) => s.id === cur)) list.unshift({ id: cur, title: S.state.title || "New chat" });
    if (!list.length) {
      el.recents.replaceChildren(h("div", { class: "recents-empty", text: "Your conversations will appear here." }));
      return;
    }
    el.recents.replaceChildren(...list.map((s) => {
      const title = s.id === cur && S.state.title ? S.state.title : s.title || "Untitled";
      return h("div", { class: "recent" + (s.id === cur ? " active" : ""), "data-id": s.id },
        h("button", { class: "r-title", text: title, title: sessionTooltip(Object.assign({}, s, { title })), onclick: () => openSession(s.id) }),
        h("div", { class: "r-actions" },
          h("button", { class: "icon-btn", icon: "pencil", title: "Rename", "aria-label": `Rename ${title}`, onclick: () => renameSession(s.id, title) }),
          h("button", { class: "icon-btn", icon: "trash", title: "Delete", "aria-label": `Delete ${title}`, onclick: () => deleteSession(s.id, title) })));
    }));
  }

  function highlightSession() {
    const key = [S.state.session_id || "", S.state.title, el.thread.childElementCount > 0].join("\u0000");
    if (key !== sessionsKey) {
      renderSessions();
      return;
    }
    for (const row of el.recents.children) row.classList.toggle("active", row.dataset.id === S.state.session_id);
  }

  const isNarrow = () => window.matchMedia("(max-width: 860px)").matches;

  function toggleSidebar(open) {
    if (isNarrow()) {
      document.body.classList.toggle("sb-open", open !== undefined ? open : !document.body.classList.contains("sb-open"));
      return;
    }
    const collapsed = open !== undefined ? !open : !document.body.classList.contains("sb-collapsed");
    document.body.classList.toggle("sb-collapsed", collapsed);
    try {
      localStorage.setItem("kimi.sidebar", collapsed ? "collapsed" : "open");
    } catch (e) {
      /* storage may be unavailable */
    }
  }

  function closeSidebarOverlay() {
    document.body.classList.remove("sb-open");
  }

  // ------------------------------------------------------------------ conversation actions
  function busy(what) {
    if (!S.running) return false;
    toast(`Stop the current task before ${what}.`, true);
    return true;
  }

  async function newChat() {
    closeSidebarOverlay();
    if (S.empty && !el.thread.childElementCount) {
      el.input.focus();
      return;
    }
    if (busy("starting a new chat")) return;
    if (await call("/api/new", {})) el.input.focus();
  }

  async function openSession(id) {
    closeSidebarOverlay();
    if (id === S.state.session_id || busy("switching conversations")) return;
    await call("/api/open", { id });
  }

  async function renameSession(id, title) {
    const value = await promptDialog({ title: "Rename conversation", value: title || "", confirm: "Save" });
    if (value && value !== title) await call("/api/rename", { id, title: value });
  }

  function renameCurrent() {
    if (S.state.session_id && el.thread.childElementCount) renameSession(S.state.session_id, S.state.title);
  }

  async function deleteSession(id, title) {
    if (id === S.state.session_id && busy("deleting this conversation")) return;
    const ok = await confirmDialog({
      title: "Delete conversation?", text: `“${title || "Untitled"}” will be permanently deleted.`,
      confirm: "Delete", danger: true,
    });
    if (ok && (await call("/api/delete", { id }))) toast("Conversation deleted");
  }

  async function compactChat() {
    const focus = await promptDialog({
      title: "Compact conversation", confirm: "Compact", optional: true,
      text: "Kimi Agent will summarize the conversation so far, freeing up room for more messages.",
      placeholder: "What should the summary focus on? (optional)",
    });
    if (focus === null) return;
    S.stick = true;
    await call("/api/compact", { focus });
  }

  async function exportChat() {
    let res;
    try {
      res = await fetch("/api/export?id=current", { headers: { "X-Kimi-Agent": KEY }, cache: "no-store" });
    } catch (e) {
      toast("Kimi Agent is not responding.", true);
      return;
    }
    if (!res.ok) {
      if (res.status === 401) showSignedOut();
      else toast(((await res.json().catch(() => null)) || {}).error || `Export failed (${res.status}).`, true);
      return;
    }
    const match = /filename\*=UTF-8''([^;]+)/i.exec(res.headers.get("Content-Disposition") || "");
    const url = URL.createObjectURL(await res.blob());
    const a = h("a", { href: url, download: match ? decodeURIComponent(match[1]) : "conversation.md" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  async function retryLast() {
    S.stick = true;
    await call("/api/retry", {});
  }

  async function quitApp() {
    if (S.running) {
      const ok = await confirmDialog({
        title: "Quit Kimi Agent?", text: "A task is still running. Quitting stops it.", confirm: "Quit", danger: true,
      });
      if (!ok) return;
    }
    try {
      await api("/api/shutdown", {});
    } catch (e) {
      /* already gone */
    }
    showClosed(true);
  }

  async function setApproval(mode) {
    if (mode === S.state.approval) return mode;
    if (mode === "yolo") {
      const ok = await confirmDialog({
        title: "Give Kimi Agent full access?", confirm: "Give full access", danger: true,
        text: "Kimi Agent will run commands, change files anywhere and use your computer without asking first. " +
          "It still asks before destructive commands. Only use this for tasks you trust.",
      });
      if (!ok) return null;
    }
    const res = await call("/api/settings", { approval: mode });
    if (!res) return null;
    toast(`Permissions: ${APPROVAL[mode].label}`);
    return mode;
  }

  async function saveConnection(changes, message) {
    const res = await call("/api/settings", changes);
    if (res) {
      if ("provider" in changes || "base_url" in changes || "api_key" in changes) S.modelCache = {};
      if (message) toast(message);
    }
    return res;
  }

  // ------------------------------------------------------------------ popovers
  let popover = null;

  function closePopover() {
    if (!popover) return;
    const p = popover;
    popover = null;
    p.node.remove();
    document.removeEventListener("mousedown", p.outside, true);
    p.anchor.setAttribute("aria-expanded", "false");
  }

  /** Open a menu next to `anchor`; clicking the same anchor again closes it (returns null). */
  function openPopover(anchor, content, align) {
    const same = popover && popover.anchor === anchor;
    closePopover();
    if (same) return null;
    const node = h("div", { class: "popover", role: "menu" }, content);
    const outside = (e) => {
      if (!node.contains(e.target) && !anchor.contains(e.target)) closePopover();
    };
    node.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const items = Array.from(node.querySelectorAll(".menu-item:not(:disabled)"));
      if (!items.length) return;
      e.preventDefault();
      let i = items.indexOf(document.activeElement);
      i = e.key === "ArrowDown" ? (i + 1) % items.length : i <= 0 ? items.length - 1 : i - 1;
      items[i].focus();
    });
    el.layer.append(node);
    document.addEventListener("mousedown", outside, true);
    popover = { node, anchor, outside, align };
    anchor.setAttribute("aria-expanded", "true");
    placePopover();
    return node;
  }

  function placePopover() {
    if (!popover) return;
    const { node, anchor, align } = popover;
    const r = anchor.getBoundingClientRect();
    const w = node.offsetWidth, ht = node.offsetHeight, vw = window.innerWidth, vh = window.innerHeight;
    const left = Math.max(8, Math.min(align === "right" ? r.right - w : r.left, vw - w - 8));
    const above = r.top > vh / 2;
    let top = above ? r.top - ht - 6 : r.bottom + 6;
    if (top < 8) top = 8;
    if (top + ht > vh - 8) top = Math.max(8, vh - ht - 8);
    node.style.left = left + "px";
    node.style.top = top + "px";
  }

  function menuItem(icon, label, opts) {
    opts = opts || {};
    return h("button", {
      class: "menu-item" + (opts.danger ? " danger" : ""), type: "button", role: "menuitem", disabled: !!opts.disabled,
      title: opts.title,
      onclick: () => {
        closePopover();
        if (opts.action) opts.action();
      },
    }, icon ? ico(icon) : null,
    h("span", { class: "mi-body" }, label, opts.desc ? h("span", { class: "mi-desc", text: opts.desc }) : null),
    opts.checked ? h("span", { class: "mi-check" }, ico("check")) : null);
  }

  function segmented(options, value, onchange) {
    const seg = h("div", { class: "seg", role: "radiogroup" });
    let current = value;
    const buttons = options.map(([v, label]) => h("button", {
      type: "button", class: v === value ? "on" : "", role: "radio", "aria-checked": String(v === value), text: label,
      onclick: () => {
        if (v === current) return;
        current = v;
        buttons.forEach((b, i) => {
          const on = options[i][0] === v;
          b.classList.toggle("on", on);
          b.setAttribute("aria-checked", String(on));
        });
        onchange(v);
      },
    }));
    seg.append(...buttons);
    return seg;
  }

  function openApprovalMenu() {
    openPopover(el.approvalBtn, [
      h("div", { class: "pop-title", text: "Permissions" }),
      ["ask", "auto", "yolo"].map((mode) => menuItem(APPROVAL[mode].icon, APPROVAL[mode].label, {
        desc: APPROVAL[mode].desc, checked: mode === S.state.approval, action: () => setApproval(mode),
      })),
    ]);
  }

  function openMainMenu() {
    const has = el.thread.childElementCount > 0;
    const idle = !S.running && !!S.state.ready;
    openPopover(el.menuBtn, [
      menuItem("pencil", "Rename", { disabled: !has, action: renameCurrent }),
      menuItem("undo", "Undo last message", {
        disabled: !idle || !S.state.can_undo, action: editLast,
        desc: "Remove it, revert its file changes and put it back in the message box",
      }),
      menuItem("compress", "Compact conversation", {
        disabled: !idle || !has, action: compactChat, desc: "Summarize older messages to free up room",
      }),
      menuItem("download", "Export as Markdown", { disabled: !has || !S.state.ready, action: exportChat }),
      menuItem("trash", "Delete conversation", {
        danger: true, disabled: !has || S.running, action: () => deleteSession(S.state.session_id, S.state.title),
      }),
      h("div", { class: "menu-sep" }),
      menuItem("sliders", "Settings", { action: openSettings }),
      menuItem("power", "Quit Kimi Agent", { action: quitApp }),
    ], "right");
  }

  async function openModelMenu() {
    const st = S.state;
    const list = h("div", { class: "model-list" });
    const filter = h("input", {
      class: "pop-input", placeholder: "Search or enter a model name", spellcheck: "false", "aria-label": "Model",
    });
    const effortName = (v) => (EFFORTS.find((e) => e[0] === v) || ["", v])[1];
    const node = openPopover(el.modelBtn, [
      h("div", { class: "pop-title", text: PROVIDER_NAMES[st.provider] || st.provider || "Model" }),
      filter, list,
      h("div", { class: "menu-sep" }),
      h("div", { class: "pop-title", text: "Thinking effort", title: "How hard models that support it think before answering" }),
      h("div", { class: "pop-row" }, segmented(EFFORTS, st.effort || "",
        (v) => saveConnection({ reasoning_effort: v }, `Thinking effort: ${effortName(v)}`))),
      h("div", { class: "menu-sep" }),
      menuItem("sliders", "Provider and API key…", { action: openSettings }),
    ], "right");
    if (!node) return;
    const key = st.provider || "";
    let models = S.modelCache[key] || null, error = "";
    const pick = (name) => {
      closePopover();
      if (name && name !== st.model) saveConnection({ model: name }, `Model: ${shortModel(name)}`);
    };
    const matching = (q) => (models || []).filter((m) => !q || m.toLowerCase().includes(q.toLowerCase()));
    const render = () => {
      const q = filter.value.trim();
      let shown = matching(q);
      if (st.model && !(models || []).includes(st.model) && (!q || st.model.toLowerCase().includes(q.toLowerCase()))) {
        shown = [st.model, ...shown];
      }
      const items = [];
      if (q && !shown.includes(q)) items.push(menuItem("plus", `Use “${q}”`, { action: () => pick(q) }));
      for (const m of shown.slice(0, 200)) items.push(menuItem(null, m, { checked: m === st.model, action: () => pick(m) }));
      if (!models) items.push(h("div", { class: "pop-note" }, h("span", { class: "spin" }), " Loading models…"));
      else if (error && !q) items.push(h("div", { class: "pop-note", text: `Couldn't list the models: ${error}` }));
      else if (!shown.length && !q) items.push(h("div", { class: "pop-note", text: "No models found. Type a model name above." }));
      list.replaceChildren(...items);
      placePopover();
    };
    filter.addEventListener("input", render);
    filter.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.isComposing) return;
      e.preventDefault();
      const q = filter.value.trim();
      if (!q) return;
      const found = matching(q);
      pick(found.length === 1 ? found[0] : q);
    });
    render();
    setTimeout(() => filter.focus(), 0);
    if (!models) {
      const data = await call("/api/models");
      models = (data && data.models) || [];
      error = data ? data.error || "" : "the request failed";
      if (data && !data.error) S.modelCache[key] = models;
      if (popover && popover.node === node) render();
    }
  }

  // ------------------------------------------------------------------ modals
  let fieldSeq = 0;

  function modal(opts) {
    const closable = opts.closable !== false;
    const back = h("div", { class: "modal-back" });
    const box = h("div", {
      class: "modal" + (opts.wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": opts.title || "Dialog",
    });
    const m = {
      kind: opts.kind || "", closable, closed: false, box,
      close() {
        if (m.closed) return;
        m.closed = true;
        back.remove();
        S.modals = S.modals.filter((x) => x !== m);
        if (opts.onclose) opts.onclose();
        if (!S.modals.length && restore && document.contains(restore)) restore.focus();
      },
    };
    const restore = document.activeElement;
    box.append(
      h("div", { class: "modal-head" }, h("h2", { text: opts.title || "" }),
        closable ? h("button", { class: "icon-btn small", type: "button", icon: "x", title: "Close", onclick: () => m.close() }) : null),
      h("div", { class: "modal-body" }, opts.body));
    if (opts.foot) box.append(h("div", { class: "modal-foot" }, opts.foot));
    back.append(box);
    if (closable) back.addEventListener("mousedown", (e) => { if (e.target === back) m.close(); });
    box.addEventListener("keydown", (e) => {
      if (e.key !== "Tab") return;
      const nodes = Array.from(box.querySelectorAll("button, input, select, textarea, a[href]"))
        .filter((n) => !n.disabled && n.offsetParent !== null);
      if (!nodes.length) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });
    closePopover();
    el.layer.append(back);
    S.modals.push(m);
    setTimeout(() => {
      if (box.contains(document.activeElement)) return;
      const target = box.querySelector("[data-autofocus]") || box.querySelector(".modal-body input, .modal-body textarea") ||
        box.querySelector(".modal-foot .btn.primary, .modal-foot .btn.solid");
      if (target) target.focus();
    }, 0);
    return m;
  }

  function closeAllModals() {
    closePopover();
    for (const m of S.modals.slice()) m.close();
  }

  function confirmDialog(opts) {
    return new Promise((resolve) => {
      let result = false;
      const m = modal({
        title: opts.title, body: opts.text ? h("p", { text: opts.text }) : null, onclose: () => resolve(result),
        foot: [h("button", { class: "btn", type: "button", text: opts.cancel || "Cancel", onclick: () => m.close() }),
          h("button", {
            class: "btn " + (opts.danger ? "danger solid" : "primary"), type: "button", text: opts.confirm || "OK",
            "data-autofocus": "", onclick: () => { result = true; m.close(); },
          })],
      });
    });
  }

  function promptDialog(opts) {
    return new Promise((resolve) => {
      let result = null;
      const input = h("input", { class: "input", value: opts.value || "", placeholder: opts.placeholder || "", spellcheck: "false" });
      const done = () => {
        const value = input.value.trim();
        if (!value && !opts.optional) {
          input.focus();
          return;
        }
        result = value;
        m.close();
      };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          done();
        }
      });
      const m = modal({
        title: opts.title, body: [opts.text ? h("p", { text: opts.text }) : null, input], onclose: () => resolve(result),
        foot: [h("button", { class: "btn", type: "button", text: "Cancel", onclick: () => m.close() }),
          h("button", { class: "btn primary", type: "button", text: opts.confirm || "OK", onclick: done })],
      });
      setTimeout(() => input.select(), 0);
    });
  }

  function field(label, control, hint) {
    control.id = control.id || `f${++fieldSeq}`;
    return h("div", { class: "field" }, h("label", { for: control.id, text: label }), control,
      hint ? h("div", { class: "hint" }, hint) : null);
  }

  function switchRow(title, desc, checked, onchange, disabled) {
    const input = h("input", { type: "checkbox", role: "switch" });
    input.checked = !!checked;
    input.disabled = !!disabled;
    input.addEventListener("change", async () => {
      input.disabled = true;
      const ok = await onchange(input.checked);
      if (!ok) input.checked = !input.checked;
      input.disabled = !!disabled;
    });
    return h("label", { class: "row" },
      h("span", { class: "row-text" }, h("span", { class: "row-title", text: title }), h("span", { class: "row-desc", text: desc })),
      h("span", { class: "switch" }, input, h("span")));
  }

  function keyLink(url) {
    return url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: "Get a key" }) : null;
  }

  // ------------------------------------------------------------------ settings
  async function openSettings() {
    closeSidebarOverlay();
    closePopover();
    if (S.modals.some((m) => m.kind === "settings")) return;
    let data;
    try {
      data = await api("/api/settings");
    } catch (e) {
      toast(e.message, true);
      return;
    }
    modal({ title: "Settings", kind: "settings", wide: true, body: settingsBody(data) });
  }

  function settingsBody(data) {
    const find = (name) => (data.providers || []).find((p) => p.name === name) || {};
    const error = h("div", { class: "form-error", hidden: true });
    const providerSel = h("select", { class: "input" },
      (data.providers || []).map((p) => h("option", { value: p.name, text: PROVIDER_NAMES[p.name] || p.name })));
    providerSel.value = data.provider;
    const providerHint = h("span");
    const keyInput = h("input", { class: "input", type: "password", autocomplete: "off", spellcheck: "false" });
    const keyHint = h("span");
    const keyField = field("API key", keyInput, keyHint);
    const modelInput = h("input", { class: "input", value: data.model || "", spellcheck: "false" });
    const urlInput = h("input", { class: "input", value: data.base_url || "", spellcheck: "false" });
    let effort = data.reasoning_effort || "";
    const saveBtn = h("button", { class: "btn primary", type: "button", text: "Save" });

    const sync = (switched) => {
      const p = find(providerSel.value);
      providerHint.textContent = p.description || "";
      keyField.hidden = !p.needs_key;
      keyInput.value = "";
      const hint = providerSel.value === data.provider && data.key_hint ? ` (${data.key_hint})` : "";
      keyInput.placeholder = p.has_key ? `Saved${hint}. Paste a new key to replace it.` : `Paste your ${p.key_env || "API key"}`;
      keyHint.replaceChildren(`Stored only on this computer, in ${data.paths.env}. `, keyLink(p.key_url) || "");
      if (switched) {
        modelInput.value = p.model || "";
        urlInput.value = p.base_url || "";
      }
      modelInput.placeholder = p.model || "";
      urlInput.placeholder = p.base_url || "";
    };
    providerSel.addEventListener("change", () => sync(true));
    sync(false);

    saveBtn.addEventListener("click", async () => {
      error.hidden = true;
      saveBtn.disabled = true;
      const body = {
        provider: providerSel.value, model: modelInput.value.trim(), base_url: urlInput.value.trim(), reasoning_effort: effort,
      };
      if (keyInput.value.trim()) body.api_key = keyInput.value.trim();
      try {
        Object.assign(data, await api("/api/settings", body));
        S.modelCache = {};
        sync(false);
        toast("Settings saved");
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
      } finally {
        saveBtn.disabled = false;
      }
    });

    const permissions = ["ask", "auto", "yolo"].map((mode) => h("button", {
      class: "option" + (data.approval === mode ? " on" : ""), type: "button", "data-mode": mode,
      onclick: async () => {
        const now = await setApproval(mode);
        if (now) permissions.forEach((b) => b.classList.toggle("on", b.dataset.mode === now));
      },
    }, ico(APPROVAL[mode].icon), h("span", {}, h("b", { text: APPROVAL[mode].label }), h("small", { text: APPROVAL[mode].desc }))));

    const toggle = (name) => async (value) => !!(await saveConnection({ [name]: value }));
    const computer = data.computer || {};
    const pathLine = (label, path) => h("div", { class: "path-line" }, h("span", { text: `${label}: ${path}` }),
      h("button", {
        class: "icon-btn small", type: "button", icon: "copy", title: "Copy path",
        onclick: () => copyText(path).then((ok) => ok && toast("Copied")),
      }));
    const shortcut = (keys, what) => h("div", { class: "row" }, h("span", { class: "row-title", text: what }),
      h("span", { class: "row-desc", text: keys }));
    const mod = /Mac/.test(navigator.platform) ? "⌘" : "Ctrl";

    return [
      data.config_error ? h("div", { class: "form-error", text: `Your config file has a problem, so defaults are used: ${data.config_error}` }) : null,
      h("div", { class: "section" }, h("h3", { text: "Model provider" }),
        field("Provider", providerSel, providerHint), keyField,
        field("Model", modelInput, "The model Kimi Agent uses. You can also pick one from the model menu in the message box."),
        field("Server address", urlInput, "Only change this if you run your own server or proxy."),
        h("div", { class: "field" }, h("span", { class: "field-label", text: "Thinking effort" }),
          segmented(EFFORTS, effort, (v) => { effort = v; }),
          h("div", { class: "hint", text: "How long models that support it think before answering. Auto lets the model decide." })),
        error, h("div", { class: "row" }, h("span"), saveBtn)),
      h("div", { class: "section" }, h("h3", { text: "Permissions" }),
        h("p", { class: "hint", text: "What Kimi Agent may do without asking you first." }), h("div", { class: "options" }, permissions)),
      h("div", { class: "section" }, h("h3", { text: "Capabilities" }),
        switchRow("Web access", "Search the web and read web pages.", data.web, toggle("web")),
        switchRow("Computer use", computer.available ? "See your screen and use the mouse and keyboard (always asks first unless you give full access)."
          : `Not available on this computer: ${computer.reason || "missing support"}.`,
        data.computer_use && computer.available, toggle("computer_use"), !computer.available)),
      h("div", { class: "section" }, h("h3", { text: "Appearance" }),
        h("div", { class: "row" }, h("span", { class: "row-title", text: "Theme" }),
          segmented([["system", "System"], ["light", "Light"], ["dark", "Dark"]], data.theme || "system", (v) => {
            if (window.applyTheme) window.applyTheme(v);
            call("/api/settings", { theme: v });
          }))),
      h("div", { class: "section" }, h("h3", { text: "Keyboard shortcuts" }),
        shortcut("Enter", "Send message"), shortcut("Shift + Enter", "New line"), shortcut("Esc", "Stop Kimi Agent"),
        shortcut(`${mod} + Shift + O`, "New chat"), shortcut(`${mod} + B`, "Show or hide the sidebar"),
        shortcut(`${mod} + ,`, "Settings")),
      h("div", { class: "section" }, h("h3", { text: "About" }),
        h("p", { class: "hint" }, `Kimi Agent ${data.version || ""} · `,
          h("a", { href: "https://github.com/Trexfly3/kimi-agent", target: "_blank", rel: "noopener noreferrer", text: "Project page" })),
        pathLine("Data", data.paths.data), pathLine("Settings file", data.paths.config),
        pathLine("API keys", data.paths.env), pathLine("Logs", data.paths.logs),
        h("div", { class: "row" }, h("span"), h("button", { class: "btn danger", type: "button", icon: "power", onclick: quitApp }, "Quit Kimi Agent"))),
    ];
  }

  // ------------------------------------------------------------------ first-run setup
  async function openSetup() {
    if (S.modals.some((m) => m.kind === "setup" || m.kind === "settings")) return;
    let data;
    try {
      data = await api("/api/settings");
    } catch (e) {
      return;
    }
    const featured = ["kimi", "openrouter", "kimi-code", "ollama"]
      .map((n) => (data.providers || []).find((p) => p.name === n)).filter(Boolean);
    if (!featured.length) {
      openSettings();
      return;
    }
    let chosen = featured.find((p) => p.name === data.provider) || featured[0];
    const keyInput = h("input", { class: "input", type: "password", autocomplete: "off", spellcheck: "false" });
    const keyHint = h("span");
    const keyField = field("API key", keyInput, keyHint);
    const error = h("div", { class: "form-error", hidden: true });
    const go = h("button", { class: "btn accent", type: "button", text: "Continue" });
    const options = featured.map((p) => h("button", { class: "option", type: "button", "data-name": p.name, onclick: () => choose(p) },
      ico(p.needs_key ? "key" : "monitor"),
      h("span", {}, h("b", { text: PROVIDER_NAMES[p.name] || p.name }), h("small", { text: p.description }))));
    const choose = (p) => {
      chosen = p;
      options.forEach((o) => o.classList.toggle("on", o.dataset.name === p.name));
      keyField.hidden = !p.needs_key;
      keyInput.placeholder = p.has_key ? "Already saved. Paste a new key to replace it." : `Paste your ${p.key_env || "API key"}`;
      keyHint.replaceChildren("Your key is stored only on this computer. ", keyLink(p.key_url) || "");
      error.hidden = true;
    };
    const m = modal({
      title: "Welcome to Kimi Agent", kind: "setup",
      body: [
        h("img", { class: "logo-lg", src: "/static/icon.svg", alt: "" }),
        h("p", { text: "Kimi Agent gets things done on your computer: it can work with your files, run programs and search the web. " +
          "First, choose the AI service it should use." }),
        h("div", { class: "options" }, options), h("div", { style: "height:12px" }), keyField, error,
        h("p", { class: "hint" }, "Using something else? ", h("a", {
          href: "#", text: "See all providers",
          onclick: (e) => {
            e.preventDefault();
            m.close();
            openSettings();
          },
        })),
      ],
      foot: [h("button", { class: "btn ghost", type: "button", text: "Skip for now", onclick: () => m.close() }), go],
    });
    choose(chosen);
    const save = async () => {
      const key = keyInput.value.trim();
      if (chosen.needs_key && !key && !chosen.has_key) {
        error.textContent = "Paste your API key to continue.";
        error.hidden = false;
        keyInput.focus();
        return;
      }
      go.disabled = true;
      try {
        await api("/api/settings", key ? { provider: chosen.name, api_key: key } : { provider: chosen.name });
        S.modelCache = {};
        m.close();
        toast("You're all set. Ask Kimi Agent anything.");
        el.input.focus();
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
      } finally {
        go.disabled = false;
      }
    };
    go.addEventListener("click", save);
    keyInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        save();
      }
    });
  }

  // ------------------------------------------------------------------ folder picker
  function openFolderPicker() {
    if (busy("changing folders")) return;
    let current = null;
    const pathInput = h("input", { class: "input", spellcheck: "false", "aria-label": "Folder path" });
    const upBtn = h("button", { class: "btn", type: "button", icon: "arrowUp", title: "Up one level", onclick: () => current && current.parent && go(current.parent) });
    const places = h("div", { class: "places" });
    const list = h("div", { class: "dir-list" });
    const error = h("div", { class: "form-error", hidden: true });
    const nameInput = h("input", { class: "input", placeholder: "New folder name", spellcheck: "false" });
    const mkdirRow = h("div", { class: "mkdir-row", hidden: true }, nameInput,
      h("button", { class: "btn", type: "button", text: "Create", onclick: () => makeFolder() }));

    const place = (icon, label, path) => h("button", { class: "btn sm", type: "button", icon, title: path, onclick: () => go(path) }, label);

    async function go(path) {
      error.hidden = true;
      let data;
      try {
        data = await api("/api/browse" + (path ? "?path=" + encodeURIComponent(path) : ""));
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
        return;
      }
      current = data;
      pathInput.value = data.path;
      upBtn.disabled = !data.parent;
      if (data.error) {
        error.textContent = `Can't open this folder: ${data.error}`;
        error.hidden = false;
      }
      list.replaceChildren(...(data.dirs.length
        ? data.dirs.map((d) => h("button", { class: "dir", type: "button", title: d.path, onclick: () => go(d.path) }, ico("folder"), h("span", { text: d.name })))
        : [h("div", { class: "dir-empty", text: data.error ? "" : "No folders inside this one." })]));
      list.scrollTop = 0;
      const known = data.places || [];
      const skip = new Set([data.home, ...known.map((k) => k.path)]);
      const recent = (S.state.recent_folders || []).filter((p) => !skip.has(p)).slice(0, 4);
      places.replaceChildren(place("home", "Home", data.home),
        ...known.map((k) => place(PLACE_ICON[k.name] || "folder", k.name, k.path)),
        ...recent.map((p) => place("clock", basename(p) || p, p)),
        ...(data.roots || []).map((r) => place("drive", r.replace(/\\$/, ""), r)));
    }

    async function makeFolder() {
      const name = nameInput.value.trim();
      if (!name || !current) return;
      const res = await call("/api/mkdir", { parent: current.path, name });
      if (!res) return;
      nameInput.value = "";
      mkdirRow.hidden = true;
      go(res.path);
    }

    pathInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        go(pathInput.value.trim());
      }
    });
    nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        makeFolder();
      }
    });
    const useBtn = h("button", {
      class: "btn primary", type: "button", text: "Use this folder",
      onclick: async () => {
        if (!current) return;
        useBtn.disabled = true;
        const ok = await call("/api/workdir", { path: current.path });
        useBtn.disabled = false;
        if (!ok) return;
        m.close();
        toast(`Working in ${basename(current.path) || current.path}`);
        el.input.focus();
      },
    });
    const m = modal({
      title: "Choose a working folder", kind: "folder", wide: true,
      body: [h("p", { class: "hint", text: "Kimi Agent works in this folder: it reads and changes files here without asking (in Auto mode) and asks before touching anything else." }),
        h("div", { class: "pathbar" }, pathInput, upBtn), places, error, list, mkdirRow],
      foot: [h("button", {
        class: "btn ghost", type: "button", icon: "folderPlus",
        onclick: () => {
          mkdirRow.hidden = !mkdirRow.hidden;
          if (!mkdirRow.hidden) nameInput.focus();
        },
      }, "New folder"), h("span", { class: "spacer" }),
      h("button", { class: "btn", type: "button", text: "Cancel", onclick: () => m.close() }), useBtn],
    });
    go(S.state.workdir || "");
  }

  // ------------------------------------------------------------------ lightbox, drag and drop
  function openLightbox(src, alt) {
    closeLightbox();
    S.lightbox = h("div", { class: "lightbox", role: "dialog", "aria-label": alt || "Image", onclick: closeLightbox },
      h("img", { src, alt: alt || "" }));
    document.body.append(S.lightbox);
  }

  function closeLightbox() {
    if (!S.lightbox) return false;
    S.lightbox.remove();
    S.lightbox = null;
    return true;
  }

  let dragDepth = 0;
  const dragHasFiles = (e) => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  window.addEventListener("dragenter", (e) => {
    if (!dragHasFiles(e) || S.closed) return;
    e.preventDefault();
    dragDepth += 1;
    el.drop.hidden = false;
  });
  window.addEventListener("dragover", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = S.closed ? "none" : "copy";
  });
  window.addEventListener("dragleave", (e) => {
    if (!dragHasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) el.drop.hidden = true;
  });
  window.addEventListener("drop", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    el.drop.hidden = true;
    if (S.closed || S.modals.length) return;
    addFiles(e.dataTransfer.files);
    el.input.focus();
  });

  // ------------------------------------------------------------------ keyboard
  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (e.key === "Escape") {
      if (closeLightbox()) return;
      if (popover) {
        const anchor = popover.anchor;
        closePopover();
        anchor.focus();
        return;
      }
      const top = S.modals[S.modals.length - 1];
      if (top) {
        if (top.closable) top.close();
        return;
      }
      if (document.body.classList.contains("sb-open")) {
        closeSidebarOverlay();
        return;
      }
      if (isTyping(e.target) && e.target !== el.input) {
        e.target.blur();
        return;
      }
      if (S.running) {
        e.preventDefault();
        stopRun();
      }
      return;
    }
    if (S.closed) return;
    const key = e.key.toLowerCase();
    if (mod && e.shiftKey && key === "o") {
      e.preventDefault();
      newChat();
    } else if (mod && !e.shiftKey && key === "b") {
      e.preventDefault();
      toggleSidebar();
    } else if (mod && e.key === ",") {
      e.preventDefault();
      openSettings();
    } else if (!mod && !e.altKey && e.target === document.body && !S.modals.length && !popover) {
      if (e.key === "/") {
        e.preventDefault();
        el.input.focus();
      } else if (e.key.length === 1) {
        el.input.focus(); // typing anywhere goes to the message box
      }
    }
  });

  // ------------------------------------------------------------------ start
  function greet() {
    const hour = new Date().getHours();
    el.greeting.textContent = hour < 5 ? "Hello, night owl" : hour < 12 ? "Good morning"
      : hour < 18 ? "Good afternoon" : "Good evening";
  }

  function useStarter(text, attach) {
    const at = text.indexOf("|");
    el.input.value = text.replace("|", "");
    autosize();
    updateComposer();
    saveDraft();
    el.input.focus();
    const pos = at >= 0 ? at : el.input.value.length;
    el.input.setSelectionRange(pos, pos);
    if (attach) el.fileInput.click();
  }

  function init() {
    for (const node of Array.from(document.querySelectorAll("[data-icon]"))) {
      const name = node.dataset.icon;
      node.removeAttribute("data-icon");
      if (node.tagName === "SPAN" && !node.className) node.replaceWith(ico(name));
      else node.prepend(ico(name));
    }
    try {
      if (localStorage.getItem("kimi.sidebar") === "collapsed") document.body.classList.add("sb-collapsed");
      el.input.value = sessionStorage.getItem("kimi.draft") || "";
    } catch (e) {
      /* storage may be unavailable */
    }
    el.version = h("span", { class: "sb-version" });
    $("open-settings").append(el.version);
    greet();
    el.chips.replaceChildren(...STARTERS.map(([icon, label, text, attach]) => h("button", {
      class: "chip", type: "button", onclick: () => useStarter(text, attach),
    }, ico(icon), label)));

    $("sb-close").addEventListener("click", () => toggleSidebar(false));
    $("sb-open").addEventListener("click", () => toggleSidebar(true));
    $("new-chat").addEventListener("click", newChat);
    $("new-chat-top").addEventListener("click", newChat);
    $("open-settings").addEventListener("click", openSettings);
    el.scrim.addEventListener("click", closeSidebarOverlay);
    el.title.addEventListener("click", renameCurrent);
    el.menuBtn.addEventListener("click", openMainMenu);
    el.folderBtn.addEventListener("click", openFolderPicker);
    el.approvalBtn.addEventListener("click", openApprovalMenu);
    el.modelBtn.addEventListener("click", openModelMenu);
    el.copyLast.addEventListener("click", () => copyText(lastAssistantText()).then((ok) => ok && toast("Copied")));
    el.retryLast.addEventListener("click", retryLast);
    for (const button of [el.menuBtn, el.approvalBtn, el.modelBtn]) {
      button.setAttribute("aria-haspopup", "menu");
      button.setAttribute("aria-expanded", "false");
    }

    el.composer.addEventListener("submit", (e) => {
      e.preventDefault();
      submit();
    });
    el.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        if (!S.running) submit();
      }
    });
    el.input.addEventListener("input", () => {
      autosize();
      updateComposer();
      saveDraft();
    });
    el.input.addEventListener("paste", (e) => {
      const data = e.clipboardData;
      const files = data ? Array.from(data.files || []) : [];
      if (files.length && !data.getData("text/plain")) {
        e.preventDefault();
        addFiles(files);
      }
    });
    el.attach.addEventListener("click", () => el.fileInput.click());
    el.fileInput.addEventListener("change", () => {
      addFiles(el.fileInput.files);
      el.fileInput.value = "";
      el.input.focus();
    });

    el.thread.addEventListener("click", (e) => {
      const copyBtn = e.target.closest(".copy-btn");
      if (copyBtn) {
        const pre = copyBtn.closest(".code-block") && copyBtn.closest(".code-block").querySelector("pre");
        if (!pre) return;
        copyText(pre.textContent).then((ok) => {
          if (!ok) return;
          const label = copyBtn.querySelector("span");
          copyBtn.classList.add("copied");
          if (label) label.textContent = "Copied";
          setTimeout(() => {
            copyBtn.classList.remove("copied");
            if (label) label.textContent = "Copy";
          }, 1600);
        });
        return;
      }
      const img = e.target.closest("img.thumb, .md img");
      if (img) openLightbox(img.src, img.alt);
    });
    el.files.addEventListener("click", (e) => {
      const img = e.target.closest(".file-chip img");
      if (img) openLightbox(img.src, img.alt);
    });
    window.addEventListener("resize", () => {
      closePopover();
      autosize();
      if (!isNarrow()) closeSidebarOverlay();
    });

    autosize();
    updateComposer();
    connect();
    loadSessions();
    setInterval(tick, 1000);
  }

  init();
})();
