(function () {
  "use strict";

  var script = document.currentScript;
  var assetBase = script ? new URL(".", script.src) : new URL("./assets/", window.location.href);
  var logoUrl = new URL("logos/tencent-docs.png", assetBase).href;
  var observer = null;
  var delegatedClickInstalled = false;
  var hashOpenInstalled = false;
  var tencentDocsOpenLocked = false;
  var selectedTencentFiles = new Map();
  var memoryGraphCache = null;

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function $all(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function textOf(node) {
    return String(node && node.textContent || "").trim();
  }

  function create(tag, attrs) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === undefined || value === null) return;
      if (key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else if (key.indexOf("on") === 0 && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, String(value));
    });
    for (var i = 2; i < arguments.length; i += 1) {
      var child = arguments[i];
      if (child === undefined || child === null) continue;
      if (Array.isArray(child)) child.forEach(function (item) { append(node, item); });
      else append(node, child);
    }
    return node;
  }

  function append(parent, child) {
    parent.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }

  function logo(className) {
    return create("img", { src: logoUrl, alt: "", className: className || "" });
  }

  function qrImageUrl(url) {
    return "https://api.qrserver.com/v1/create-qr-code/?size=184x184&margin=12&data=" + encodeURIComponent(url);
  }

  function fetchJson(path, options) {
    options = options || {};
    var headers = Object.assign({ "Accept": "application/json" }, options.headers || {});
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    return fetch(path, Object.assign({ credentials: "same-origin", headers: headers }, options)).then(function (response) {
      return response.text().then(function (text) {
        var payload = text ? JSON.parse(text) : {};
        if (!response.ok) {
          throw new Error(payload.message || response.statusText || "Request failed");
        }
        return payload;
      });
    });
  }

  function showToast(message) {
    var old = $(".ecorex-v029-toast");
    if (old) old.remove();
    var toast = create("div", { className: "toast ecorex-v029-toast", text: message });
    document.body.appendChild(toast);
    window.setTimeout(function () { if (toast.isConnected) toast.remove(); }, 2800);
  }

  function closeModal() {
    var old = $(".ecorex-v029-modal-backdrop");
    if (old) old.remove();
  }

  function modal(title, subtitle, body) {
    closeModal();
    var backdrop = create("div", { className: "ecorex-v029-modal-backdrop", onclick: function (event) {
      if (event.target === backdrop) closeModal();
    }});
    var panel = create("section", { className: "ecorex-v029-modal", role: "dialog", "aria-modal": "true" },
      create("header", {},
        create("div", { className: "ecorex-v029-modal-title" },
          create("span", { className: "ecorex-v029-modal-logo" }, logo()),
          create("div", {}, create("h2", { text: title }), create("p", { text: subtitle || "" }))
        ),
        create("button", { type: "button", className: "ecorex-v029-modal-close", "aria-label": "关闭", text: "×", onclick: closeModal })
      ),
      create("div", { className: "ecorex-v029-modal-body" }, body)
    );
    panel.addEventListener("click", function (event) { event.stopPropagation(); });
    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);
  }

  function enhanceTencentLogos() {
    $all(".connection-logo.is-tencent-docs").forEach(function (node) {
      if (node.dataset.tencentDocsLogo === "1") return;
      node.dataset.tencentDocsLogo = "1";
      node.textContent = "";
      node.appendChild(logo());
    });
  }

  function findAttachButton() {
    return $all("button").find(function (button) {
      var label = [
        textOf(button),
        button.getAttribute("title") || "",
        button.getAttribute("aria-label") || ""
      ].join(" ");
      return /添加本地文件|本地文件|choose files|add local/i.test(label);
    });
  }

  function findNativeTencentDocsButton() {
    return $all("button, a").find(function (node) {
      if (node.classList.contains("ecorex-tencent-docs-composer-button")) return false;
      if (node.closest(".ecorex-v029-modal")) return false;
      var label = [
        textOf(node),
        node.getAttribute("title") || "",
        node.getAttribute("aria-label") || ""
      ].join(" ");
      return /添加腾讯文档|腾讯文档|tencent docs/i.test(label);
    });
  }

  function makeTencentDocsEntry(node) {
    node.classList.add("ecorex-tencent-docs-composer-button");
    node.dataset.tencentDocsButton = "official-logo";
    node.setAttribute("title", "腾讯文档");
    node.setAttribute("aria-label", "腾讯文档");
    node.setAttribute("role", "button");
    node.setAttribute("onclick", "window.EcoreXV029OpenTencentDocs && window.EcoreXV029OpenTencentDocs(event)");
    if (node.tagName === "A") node.setAttribute("href", "#ecorex-tencent-docs");
    if (node.tagName === "BUTTON") {
      node.setAttribute("type", "button");
      node.disabled = false;
      node.removeAttribute("disabled");
    }
    node.textContent = "";
    node.appendChild(logo());
    return node;
  }

  function enhanceComposer() {
    var nativeButton = findNativeTencentDocsButton();
    if (nativeButton) {
      makeTencentDocsEntry(nativeButton);
      return;
    }
    if ($(".ecorex-tencent-docs-composer-button")) return;
    var attach = findAttachButton();
    if (!attach || !attach.parentElement) return;
    var button = makeTencentDocsEntry(create("a", {
      href: "#ecorex-tencent-docs",
      className: "ecorex-tencent-docs-composer-button",
      title: "腾讯文档",
      "aria-label": "腾讯文档",
      role: "button",
      onclick: "window.EcoreXV029OpenTencentDocs && window.EcoreXV029OpenTencentDocs(event)"
    }));
    attach.insertAdjacentElement("afterend", button);
  }

  function openTencentDocs() {
    modal("腾讯文档", "连接状态", create("div", { className: "ecorex-v029-status", text: "正在检查连接" }));
    fetchJson("/api/tencent-docs/status?start=1")
      .then(function (payload) {
        var capability = payload.capability || {};
        if (capability.configured) renderTencentPicker(capability);
        else renderTencentAuth(capability);
      })
      .catch(function (error) {
        renderTencentAuth({ message: error.message || String(error) });
      });
  }

  function clearTencentDocsHash() {
    if (window.location.hash !== "#ecorex-tencent-docs") return;
    try {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    } catch (error) {
      window.location.hash = "";
    }
  }

  function requestTencentDocsOpen(event) {
    if (event && event.preventDefault) event.preventDefault();
    if (event && event.stopPropagation) event.stopPropagation();
    clearTencentDocsHash();
    if (tencentDocsOpenLocked) return;
    tencentDocsOpenLocked = true;
    window.setTimeout(function () { tencentDocsOpenLocked = false; }, 450);
    openTencentDocs();
  }

  function handleTencentDocsHash() {
    if (window.location.hash === "#ecorex-tencent-docs") requestTencentDocsOpen();
  }

  function renderTencentAuth(capability) {
    var authUrl = capability.authUrl || "https://docs.qq.com/open/auth/mcp.html";
    var status = create("div", { className: "ecorex-v029-status", text: capability.message || capability.setupHint || "扫码授权后会自动检查连接状态；如浏览器没有返回连接，可展开高级方式手动粘贴凭据。" });
    var tokenInput = create("input", { type: "password", placeholder: "MCP Token", autocomplete: "off" });
    var body = create("div", {},
      create("div", { className: "ecorex-v029-qr-auth" },
        create("a", { href: authUrl, target: "_blank", rel: "noopener", className: "ecorex-v029-qr-box", onclick: function () {
          startTencentDocsAuthPolling();
        }},
          create("img", { src: qrImageUrl(authUrl), alt: "扫码授权腾讯文档" }),
          create("span", { text: "微信 / QQ 扫码授权" })
        ),
        create("div", {},
          create("strong", { text: "腾讯文档扫码授权" }),
          create("p", { text: "用手机扫码或点击下方按钮打开官方 MCP 授权页。授权完成后，EcoreX 会自动检查连接状态。" }),
          create("div", { className: "ecorex-v029-actions" },
            create("button", { type: "button", className: "is-primary", onclick: function () {
              window.open(authUrl, "_blank", "noopener,noreferrer");
              startTencentDocsAuthPolling();
            }}, logo(), "打开官方授权页"),
            create("button", { type: "button", onclick: openTencentDocs }, "检查连接")
          )
        )
      ),
      status,
      create("details", { className: "ecorex-v029-token-fallback" },
        create("summary", { text: "高级方式：使用 MCP token" }),
        create("form", { className: "ecorex-v029-token-form", onsubmit: function (event) {
          event.preventDefault();
          var token = tokenInput.value.trim();
          if (!token) {
            showToast("请在高级方式中填写腾讯文档 MCP token");
            return;
          }
          fetchJson("/api/tencent-docs/connect", {
            method: "POST",
            body: JSON.stringify({ token: token })
          }).then(function () {
            showToast("腾讯文档已连接");
            openTencentDocs();
          }).catch(function (error) {
            showToast(error.message || "连接失败");
          });
        }},
          tokenInput,
          create("button", { type: "submit" }, "连接")
        )
      )
    );
    modal("腾讯文档", "扫码授权", body);
  }

  var tencentDocsAuthPollTimer = null;

  function startTencentDocsAuthPolling() {
    if (tencentDocsAuthPollTimer) window.clearInterval(tencentDocsAuthPollTimer);
    var attempts = 0;
    tencentDocsAuthPollTimer = window.setInterval(function () {
      attempts += 1;
      fetchJson("/api/tencent-docs/status?start=1").then(function (payload) {
        var capability = payload.capability || {};
        if (capability.configured) {
          window.clearInterval(tencentDocsAuthPollTimer);
          tencentDocsAuthPollTimer = null;
          showToast("腾讯文档已连接");
          renderTencentPicker(capability);
        } else if (attempts >= 30) {
          window.clearInterval(tencentDocsAuthPollTimer);
          tencentDocsAuthPollTimer = null;
        }
      }).catch(function () {
        if (attempts >= 30 && tencentDocsAuthPollTimer) {
          window.clearInterval(tencentDocsAuthPollTimer);
          tencentDocsAuthPollTimer = null;
        }
      });
    }, 3000);
  }

  function renderTencentPicker(capability) {
    var list = create("div", { className: "ecorex-v029-file-list" },
      create("div", { className: "ecorex-v029-status", text: "正在读取文档" })
    );
    var searchInput = create("input", { type: "search", placeholder: "搜索腾讯文档" });
    var footer = create("div", { className: "ecorex-v029-picker-footer" },
      create("button", { type: "button", className: "is-primary", onclick: function () {
        addSelectedTencentDocsToComposer();
      }}, "加入会话"),
      create("button", { type: "button", onclick: closeModal }, "关闭")
    );
    var body = create("div", {},
      create("div", { className: "ecorex-v029-status", text: capability.connected ? "已连接" : "已配置" }),
      create("div", { className: "ecorex-v029-tabs" },
        create("button", { type: "button", className: "is-primary", onclick: function () { loadTencentFiles("recent", searchInput.value, list); }}, "最近"),
        create("button", { type: "button", onclick: function () { loadTencentFiles("mine", searchInput.value, list); }}, "我的")
      ),
      create("form", { className: "ecorex-v029-search", onsubmit: function (event) {
        event.preventDefault();
        loadTencentFiles("search", searchInput.value, list);
      }},
        searchInput,
        create("button", { type: "submit" }, "搜索")
      ),
      list,
      footer
    );
    modal("腾讯文档", "选择文档", body);
    loadTencentFiles("recent", "", list);
  }

  function fileKey(file) {
    return String(file && (file.key || file.file_id || file.node_id || file.url || file.title) || "");
  }

  function loadTencentFiles(tab, query, list) {
    list.textContent = "";
    list.appendChild(create("div", { className: "ecorex-v029-status", text: "正在读取文档" }));
    var url = "/api/tencent-docs/files?tab=" + encodeURIComponent(tab || "recent") + "&q=" + encodeURIComponent(query || "") + "&limit=30";
    fetchJson(url).then(function (payload) {
      var files = Array.isArray(payload.files) ? payload.files : [];
      list.textContent = "";
      if (!files.length) {
        list.appendChild(create("div", { className: "ecorex-v029-status", text: payload.message || "暂无文档" }));
        return;
      }
      files.forEach(function (file) {
        var key = fileKey(file);
        var checkbox = create("input", { type: "checkbox" });
        checkbox.checked = selectedTencentFiles.has(key);
        checkbox.addEventListener("change", function () {
          if (checkbox.checked) selectedTencentFiles.set(key, file);
          else selectedTencentFiles.delete(key);
        });
        list.appendChild(create("label", { className: "ecorex-v029-file-row" },
          checkbox,
          create("span", {},
            create("strong", { text: file.title || file.file_name || "腾讯文档" }),
            create("span", { text: [file.doc_type, file.owner, file.updated_at].filter(Boolean).join(" · ") || file.url || key })
          )
        ));
      });
    }).catch(function (error) {
      list.textContent = "";
      list.appendChild(create("div", { className: "ecorex-v029-status", text: error.message || "读取失败" }));
    });
  }

  function addSelectedTencentDocsToComposer() {
    var files = Array.from(selectedTencentFiles.values());
    if (!files.length) {
      showToast("请选择腾讯文档");
      return;
    }
    var textarea = $(".composer textarea") || $("textarea[placeholder*='发送']") || $("textarea");
    if (!textarea) {
      showToast("未找到输入框");
      return;
    }
    var lines = files.map(function (file) {
      var title = file.title || file.file_name || "腾讯文档";
      return "@腾讯文档 " + title + (file.url ? " " + file.url : "");
    }).join("\n");
    var nextValue = textarea.value ? textarea.value + "\n" + lines : lines;
    var setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    setter.call(textarea, nextValue);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.focus();
    closeModal();
    showToast("已加入会话");
  }

  function enhanceMemoryPanel() {
    var section = $all(".settings-section").find(function (item) {
      var head = $(".settings-section-head strong", item);
      return textOf(head) === "记忆";
    });
    if (!section || $(".ecorex-memory-tabs", section)) return;
    var originalLists = $all(".settings-list, .knowledge-graph-grid, .memory-list", section);
    var graph = create("div", { className: "memory-starry-page", hidden: "hidden" },
      create("div", { className: "memory-starry-head" },
        create("div", {}, create("h3", { text: "知识图谱" }), create("p", { text: "Knowledge Graph" })),
        create("div", { className: "memory-starry-actions" }, create("button", { type: "button", onclick: function () {
          memoryGraphCache = null;
          loadMemoryGraph(graph);
        }}, "刷新"))
      ),
      create("div", { className: "memory-starry-stats" }),
      create("div", { className: "memory-starry-legend" }),
      create("div", { className: "memory-starry-body" },
        create("div", { className: "memory-starry-canvas" }),
        create("div", { className: "memory-starry-detail", text: "选择一个节点" })
      )
    );
    var filesTab = create("button", { type: "button", className: "is-active", onclick: function () {
      filesTab.classList.add("is-active");
      graphTab.classList.remove("is-active");
      graph.hidden = true;
      originalLists.forEach(function (item) { item.hidden = false; });
    }}, "记忆文件");
    var graphTab = create("button", { type: "button", onclick: function () {
      graphTab.classList.add("is-active");
      filesTab.classList.remove("is-active");
      originalLists.forEach(function (item) { item.hidden = true; });
      graph.hidden = false;
      loadMemoryGraph(graph);
    }}, "知识图谱");
    var tabs = create("div", { className: "ecorex-memory-tabs" }, filesTab, graphTab);
    var head = $(".settings-section-head", section);
    if (head) head.insertAdjacentElement("afterend", tabs);
    section.appendChild(graph);
  }

  function loadMemoryGraph(container) {
    var canvas = $(".memory-starry-canvas", container);
    var stats = $(".memory-starry-stats", container);
    if (!canvas) return;
    canvas.textContent = "";
    canvas.appendChild(create("div", { className: "memory-starry-empty", text: "正在读取图谱" }));
    var load = memoryGraphCache ? Promise.resolve(memoryGraphCache) : fetchJson("/api/knowledge/graph").then(function (payload) {
      memoryGraphCache = payload;
      return payload;
    });
    load.then(function (payload) {
      renderMemoryGraph(container, payload || {});
      var nodes = Array.isArray(payload.nodes) ? payload.nodes : [];
      var links = Array.isArray(payload.links) ? payload.links : [];
      if (stats) {
        var categories = new Set(nodes.map(nodeCategory).filter(Boolean));
        stats.textContent = "";
        stats.appendChild(create("span", { text: "节点 " + nodes.length }));
        stats.appendChild(create("span", { text: "关联 " + links.length }));
        stats.appendChild(create("span", { text: "分类 " + categories.size }));
      }
    }).catch(function (error) {
      canvas.textContent = "";
      canvas.appendChild(create("div", { className: "memory-starry-empty", text: error.message || "图谱不可用" }));
    });
  }

  function nodeId(node) {
    return String(node && (node.id || node.path || node.title || node.name) || "");
  }

  function nodeCategory(node) {
    return String(node && (node.category || node.type || node.source || node.provider) || "记忆").trim() || "记忆";
  }

  function hashText(value) {
    var hash = 0;
    String(value || "").split("").forEach(function (char) {
      hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
    });
    return Math.abs(hash);
  }

  var memoryPalette = ["#60a5fa", "#34d399", "#fbbf24", "#f472b6", "#a78bfa", "#22d3ee", "#fb7185", "#c084fc"];

  function categoryColor(category) {
    return memoryPalette[hashText(category) % memoryPalette.length];
  }

  function renderMemoryGraph(container, payload) {
    var canvas = $(".memory-starry-canvas", container);
    var detail = $(".memory-starry-detail", container);
    var legend = $(".memory-starry-legend", container);
    var nodes = (Array.isArray(payload.nodes) ? payload.nodes : []).slice(0, 180);
    var links = (Array.isArray(payload.links) ? payload.links : []).slice(0, 260);
    canvas.textContent = "";
    if (!nodes.length) {
      canvas.appendChild(create("div", { className: "memory-starry-empty", text: "暂无图谱数据" }));
      return;
    }
    var width = Math.max(680, canvas.clientWidth || 680);
    var height = Math.max(340, canvas.clientHeight || 340);
    var cx = width / 2;
    var cy = height / 2;
    var radius = Math.min(width, height) * 0.38;
    var byId = new Map();
    var degree = new Map();
    links.forEach(function (link) {
      var sourceId = String(link.source || link.from || "");
      var targetId = String(link.target || link.to || "");
      degree.set(sourceId, (degree.get(sourceId) || 0) + 1);
      degree.set(targetId, (degree.get(targetId) || 0) + 1);
    });
    nodes.sort(function (a, b) {
      return (degree.get(nodeId(b)) || 0) - (degree.get(nodeId(a)) || 0);
    });
    nodes.forEach(function (node, index) {
      var category = nodeCategory(node);
      var angle = index * 2.399963 + (hashText(category) % 37) / 21;
      var spread = Math.sqrt((index + 1) / Math.max(1, nodes.length));
      var ring = radius * (0.12 + spread * 0.88);
      var jitter = ((hashText(nodeId(node)) % 100) / 100 - 0.5) * 28;
      node.__x = cx + Math.cos(angle) * (ring + jitter);
      node.__y = cy + Math.sin(angle) * (ring - jitter);
      byId.set(nodeId(node), node);
    });
    if (legend) {
      var categories = Array.from(new Set(nodes.map(nodeCategory))).slice(0, 10);
      legend.textContent = "";
      categories.forEach(function (category) {
        var item = create("span", {}, create("i", { style: "background:" + categoryColor(category) }), category);
        legend.appendChild(item);
      });
    }
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    var stars = document.createElementNS("http://www.w3.org/2000/svg", "g");
    stars.setAttribute("class", "memory-starry-stars");
    for (var i = 0; i < 72; i += 1) {
      var star = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      star.setAttribute("cx", String((hashText("x" + i) % width)));
      star.setAttribute("cy", String((hashText("y" + i) % height)));
      star.setAttribute("r", String(0.6 + (i % 3) * 0.35));
      stars.appendChild(star);
    }
    svg.appendChild(stars);
    links.forEach(function (link) {
      var source = byId.get(String(link.source || link.from || ""));
      var target = byId.get(String(link.target || link.to || ""));
      if (!source || !target) return;
      var line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("class", "memory-starry-link");
      line.setAttribute("x1", source.__x);
      line.setAttribute("y1", source.__y);
      line.setAttribute("x2", target.__x);
      line.setAttribute("y2", target.__y);
      svg.appendChild(line);
    });
    nodes.forEach(function (node, index) {
      var group = document.createElementNS("http://www.w3.org/2000/svg", "g");
      group.setAttribute("class", "memory-starry-node");
      group.setAttribute("transform", "translate(" + node.__x + " " + node.__y + ")");
      group.addEventListener("click", function () {
        $all(".memory-starry-node", svg).forEach(function (item) { item.classList.remove("is-selected"); });
        group.classList.add("is-selected");
        renderMemoryDetail(detail, node, links, byId);
      });
      var circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      var size = 5 + Math.min(11, Number(node.weight || node.size || (degree.get(nodeId(node)) || 0) || index % 6));
      circle.setAttribute("r", String(size));
      circle.setAttribute("fill", categoryColor(nodeCategory(node)));
      var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", String(size + 5));
      label.setAttribute("y", "4");
      label.textContent = String(node.title || node.name || node.id || "记忆").slice(0, 18);
      group.appendChild(circle);
      group.appendChild(label);
      svg.appendChild(group);
    });
    canvas.appendChild(svg);
    var firstNode = $(".memory-starry-node", svg);
    if (firstNode) firstNode.classList.add("is-selected");
    renderMemoryDetail(detail, nodes[0], links, byId);
  }

  function renderMemoryDetail(detail, node, links, byId) {
    if (!detail || !node) return;
    var id = nodeId(node);
    var related = links.filter(function (link) {
      return String(link.source || link.from || "") === id || String(link.target || link.to || "") === id;
    }).slice(0, 8).map(function (link) {
      var other = String(link.source || link.from || "") === id ? String(link.target || link.to || "") : String(link.source || link.from || "");
      var item = byId.get(other);
      return item && (item.title || item.name || item.id);
    }).filter(Boolean);
    detail.textContent = "";
    detail.appendChild(create("header", {},
      create("span", { className: "memory-starry-detail-dot", style: "background:" + categoryColor(nodeCategory(node)) }),
      create("div", {},
        create("strong", { text: node.title || node.name || node.id || "记忆节点" }),
        create("span", { text: [nodeCategory(node), node.path].filter(Boolean).join(" · ") || id })
      )
    ));
    detail.appendChild(create("div", { className: "memory-starry-meta" },
      create("span", { text: "ID " + id.slice(0, 28) }),
      create("span", { text: "关联 " + related.length })
    ));
    if (node.excerpt || node.summary) detail.appendChild(create("p", { text: String(node.excerpt || node.summary).slice(0, 320) }));
    detail.appendChild(create("div", { className: "memory-starry-related" },
      create("strong", { text: "关联节点" }),
      related.length
        ? related.map(function (title) { return create("span", { text: title }); })
        : create("span", { text: "暂无关联节点" })
    ));
  }

  function enhance() {
    enhanceTencentLogos();
  }

  function start() {
    enhance();
    if (!observer) {
      observer = new MutationObserver(function () { window.requestAnimationFrame(enhance); });
      observer.observe(document.getElementById("root") || document.body, { childList: true, subtree: true });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
  window.EcoreXV029OpenTencentDocs = null;
})();
