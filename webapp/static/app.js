/* WatchNight Frontend PWA Logic */
(() => {
  const state = {
    user: localStorage.getItem("wn_user") || "",
    activeTab: "together", // together | me | search | infuse
    mediaFilter: "both",
    searchType: "movie,show",
    browseCat: "trending_shows",
    moodPrompt: "",
    deckCards: [],
    deckIndex: 0,
    deckSource: "",
    config: null,
    syncStats: null,
    seederItems: [],
    infusePollTimer: null,
    searchDebounce: null,
  };

  const $ = (id) => document.getElementById(id);

  function esc(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  let toastTimer = null;
  function showToast(msg, isMatch = false) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast" + (isMatch ? " match" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), isMatch ? 4500 : 2800);
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
  }

  function deckModeForTab() {
    return state.activeTab === "me" ? state.user : "together";
  }

  function setActiveUser(user, persist = true) {
    state.user = user === "mark" ? "mark" : "shelly";
    if (persist) localStorage.setItem("wn_user", state.user);

    $("switch-shelly").classList.toggle("active", state.user === "shelly");
    $("switch-mark").classList.toggle("active", state.user === "mark");
    $("nav-me-label").textContent = state.user === "shelly" ? "For Shelly" : "For Mark";
    $("taste-heading").textContent =
      state.user === "shelly" ? "Shelly's Taste & Imports" : "Mark's Taste & Imports";
    $("taste-user-label").textContent = state.user === "shelly" ? "Shelly" : "Mark";
    $("profile-overlay").classList.add("hidden");

    refreshBootstrap();
    if (state.activeTab === "together" || state.activeTab === "me") {
      updateDeckHeaders();
      loadDeck();
    } else if (state.activeTab === "search") {
      runSearchOrBrowse();
    } else if (state.activeTab === "infuse") {
      loadInfuseAndTaste();
    }
  }

  function updateDeckHeaders() {
    if (state.activeTab === "me") {
      const name = state.user === "shelly" ? "Shelly" : "Mark";
      $("deck-heading").textContent = `Picked for ${name}`;
      $("deck-subheading").textContent = "Personalized picks — tap Want or Send to Infuse anytime";
    } else {
      $("deck-heading").textContent = "For Us Tonight";
      $("deck-subheading").textContent = "Both tap 👍 to auto-match & send to Infuse";
    }
  }

  async function refreshBootstrap() {
    try {
      const data = await api(`/api/bootstrap?user=${encodeURIComponent(state.user)}`);
      state.config = data.config;
      state.syncStats = data.sync_stats;

      const badge = $("engine-badge");
      if (data.config.has_api_key) {
        badge.textContent = `✨ ${data.config.ai_provider.toUpperCase()}`;
        $("settings-dot").classList.add("hidden");
      } else {
        badge.textContent = "Trakt Picks";
        $("settings-dot").classList.remove("hidden");
      }

      const nudgeDismissed = sessionStorage.getItem("wn_nudge_dismissed") === "1";
      $("shelly-nudge").classList.toggle("hidden", !(data.show_shelly_nudge && !nudgeDismissed));

      const activeBadge = $("nav-infuse-badge");
      if (data.active_infuse_count > 0) {
        activeBadge.textContent = String(data.active_infuse_count);
        activeBadge.classList.remove("hidden");
      } else {
        activeBadge.classList.add("hidden");
      }
    } catch (err) {
      console.error("Bootstrap error:", err);
    }
  }

  /* ---------- Deck Tab ---------- */

  async function loadDeck() {
    const stage = $("deck-stage");
    stage.innerHTML = `<div class="empty-card"><p class="muted">Loading picks...</p></div>`;
    try {
      const mode = deckModeForTab();
      const data = await api(
        `/api/deck?mode=${encodeURIComponent(mode)}&user=${encodeURIComponent(state.user)}&media_type=${encodeURIComponent(state.mediaFilter)}`
      );
      state.deckCards = data.cards || [];
      state.deckIndex = 0;
      state.deckSource = data.source || "";
      const warn = $("deck-warning");
      if (data.warning) {
        warn.textContent = data.warning;
        warn.classList.remove("hidden");
      } else {
        warn.classList.add("hidden");
      }
      renderCurrentCard();
    } catch (err) {
      stage.innerHTML = `<div class="empty-card"><p class="muted">Could not load picks: ${esc(err.message)}</p></div>`;
    }
  }

  async function generateNewPicks() {
    const btn = $("ask-ai-btn");
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "Thinking...";
    $("deck-stage").innerHTML = `<div class="empty-card"><p class="muted">Curating fresh picks for ${state.activeTab === "me" ? esc(state.user) : "Mark & Shelly"}...</p></div>`;

    try {
      const mode = deckModeForTab();
      const mood = $("mood-input").value.trim();
      const data = await api("/api/recommendations/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode,
          user: state.user,
          mood,
          media_type: state.mediaFilter,
        }),
      });
      state.deckCards = data.cards || [];
      state.deckIndex = 0;
      state.deckSource = data.source || "";
      const warn = $("deck-warning");
      if (data.warning) {
        warn.textContent = data.warning;
        warn.classList.remove("hidden");
      } else {
        warn.classList.add("hidden");
      }
      renderCurrentCard();
    } catch (err) {
      showToast(`Failed to generate: ${err.message}`);
      loadDeck();
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }

  function renderPosterHtml(item, cls = "") {
    if (item.poster_proxy) {
      return `<img src="${esc(item.poster_proxy)}" alt="${esc(item.title)}" loading="lazy" class="${cls}" />`;
    }
    return `<div class="poster-fallback">${esc(item.title)}</div>`;
  }

  function renderInfuseButtonOrBadge(item, compact = false) {
    if (item.in_infuse || item.infuse_status === "ready") {
      return `<span class="status-pill status-ready">✓ Ready in Infuse</span>`;
    }
    if (item.infuse_status === "queued" || item.infuse_status === "searching") {
      return `<span class="status-pill status-${esc(item.infuse_status)}">⏳ Finding stream...</span>`;
    }
    const label = compact ? "⚡ Infuse" : "⚡ Send to Infuse";
    return `<button class="btn ${compact ? "btn-sm" : "btn-full"} btn-secondary js-send-infuse" data-id="${item.trakt_id}" data-type="${esc(item.media_type)}">${label}</button>`;
  }

  function renderCurrentCard() {
    const stage = $("deck-stage");
    if (!state.deckCards.length || state.deckIndex >= state.deckCards.length) {
      stage.innerHTML = `
        <div class="empty-card">
          <h3>You've reached the end of this batch</h3>
          <p class="muted">Tap <strong>✨ Ask AI</strong> above (or below) for fresh picks based on everything you've tapped so far.</p>
          <button id="empty-ask-btn" class="btn btn-primary" style="margin-top:8px">✨ Get Fresh Picks</button>
        </div>
      `;
      const btn = $("empty-ask-btn");
      if (btn) btn.addEventListener("click", generateNewPicks);
      return;
    }

    const card = state.deckCards[state.deckIndex];
    const typeLabel = card.media_type === "show" ? "TV Series" : "Movie";
    const runtimeTxt = card.runtime ? `${card.runtime} min` : "";
    const ratingTxt = card.rating ? `★ ${Number(card.rating).toFixed(1)}` : "";
    const metaParts = [card.year, runtimeTxt, card.certification].filter(Boolean).join(" • ");
    const tags = (card.tags && card.tags.length ? card.tags : card.genres || []).slice(0, 3);
    const partnerBanner = card.partner_wanted
      ? `<div class="partner-banner">💛 ${esc(card.partner_name)} wants to watch this! Tap 👍 to match.</div>`
      : "";
    const trailerBtn = card.trailer_url
      ? `<a href="${esc(card.trailer_url)}" target="_blank" rel="noopener" class="btn btn-sm btn-ghost">▶ Trailer</a>`
      : `<span></span>`;

    stage.innerHTML = `
      <article class="feature-card" id="active-feature-card">
        ${partnerBanner}
        <div class="card-hero">
          <div class="card-poster-wrap">${renderPosterHtml(card)}</div>
          <div class="card-meta-col">
            <div class="type-badge-row">
              <span class="badge">${typeLabel}</span>
              ${ratingTxt ? `<span class="badge badge-rating">${ratingTxt}</span>` : ""}
            </div>
            <h3 class="card-title">${esc(card.title)}</h3>
            <div class="card-sub">${esc(metaParts)}</div>
            <div class="genre-pills">${tags.map((g) => `<span class="genre-pill">${esc(g)}</span>`).join("")}</div>
          </div>
        </div>
        <div class="card-body">
          ${
            card.pitch
              ? `<div class="pitch-box">
                   <div class="pitch-label">Why You'll Like It</div>
                   <p class="pitch-text">${esc(card.pitch)}</p>
                 </div>`
              : ""
          }
          <p class="overview-text" id="card-overview">${esc(card.overview || "No synopsis available.")}</p>
          <div class="card-links-row">
            ${trailerBtn}
            <div style="display:flex;align-items:center;gap:8px">
              <span class="deck-counter">${state.deckIndex + 1} of ${state.deckCards.length}</span>
              <button class="btn btn-sm btn-ghost" id="skip-card-btn">Skip →</button>
            </div>
          </div>

          <div id="seen-drawer" class="seen-drawer hidden">
            <button class="btn btn-secondary js-react" data-reaction="seen_loved">❤️ Loved</button>
            <button class="btn btn-secondary js-react" data-reaction="seen_ok">👌 Okay</button>
            <button class="btn btn-secondary js-react" data-reaction="seen_disliked">👎 Nope</button>
          </div>

          <div class="action-bar">
            <button class="act-btn pass js-react" data-reaction="pass">
              <span class="act-ico">👎</span><span>Pass</span>
            </button>
            <button class="act-btn" id="toggle-seen-btn">
              <span class="act-ico">👁️</span><span>Seen It</span>
            </button>
            <button class="act-btn want js-react" data-reaction="want">
              <span class="act-ico">👍</span><span>Want</span>
            </button>
          </div>

          <div class="send-row">
            ${renderInfuseButtonOrBadge(card, false)}
          </div>
        </div>
      </article>
    `;

    $("card-overview").addEventListener("click", (e) => e.target.classList.toggle("expanded"));
    $("toggle-seen-btn").addEventListener("click", () => $("seen-drawer").classList.toggle("hidden"));
    $("skip-card-btn").addEventListener("click", () => {
      state.deckIndex += 1;
      renderCurrentCard();
    });

    stage.querySelectorAll(".js-react").forEach((btn) => {
      btn.addEventListener("click", () => handleCardReaction(card, btn.dataset.reaction));
    });

    const infuseBtn = stage.querySelector(".js-send-infuse");
    if (infuseBtn) {
      infuseBtn.addEventListener("click", async () => {
        await sendToInfuse(card);
        renderCurrentCard();
      });
    }

    attachSwipeGesture($("active-feature-card"), card);
  }

  function attachSwipeGesture(el, card) {
    if (!el) return;
    let startX = 0;
    let startY = 0;
    let dx = 0;
    let dragging = false;

    el.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      dx = 0;
      dragging = true;
      el.style.transition = "none";
    }, { passive: true });

    el.addEventListener("touchmove", (e) => {
      if (!dragging) return;
      const curX = e.touches[0].clientX;
      const curY = e.touches[0].clientY;
      dx = curX - startX;
      const dy = curY - startY;
      if (Math.abs(dy) > Math.abs(dx) * 1.2) return;
      el.style.transform = `translateX(${dx * 0.65}px) rotate(${dx * 0.025}deg)`;
    }, { passive: true });

    el.addEventListener("touchend", () => {
      if (!dragging) return;
      dragging = false;
      el.style.transition = "transform 0.22s ease, opacity 0.22s ease";
      if (dx > 105) {
        handleCardReaction(card, "want");
      } else if (dx < -105) {
        handleCardReaction(card, "pass");
      } else {
        el.style.transform = "";
      }
    });
  }

  async function handleCardReaction(card, reaction) {
    try {
      const res = await api("/api/react", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user: state.user,
          trakt_id: card.trakt_id,
          media_type: card.media_type,
          reaction,
        }),
      });
      if (res.matched) {
        showToast(
          res.auto_sent
            ? `🎉 It's a Match! Sending "${card.title}" to Infuse!`
            : `🎉 It's a Match! "${card.title}" added to Our Matches.`,
          true
        );
        startInfusePolling();
      } else if (reaction === "want") {
        const partner = state.user === "shelly" ? "Mark" : "Shelly";
        showToast(`👍 Saved! Waiting for ${partner}'s vote.`);
      } else if (reaction === "seen_loved") {
        showToast(`❤️ Noted — we'll use "${card.title}" to tune picks!`);
      }
      state.deckIndex += 1;
      renderCurrentCard();
      refreshBootstrap();
    } catch (err) {
      showToast(`Could not save reaction: ${err.message}`);
    }
  }

  async function sendToInfuse(item) {
    try {
      const data = await api("/api/infuse/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          trakt_id: item.trakt_id,
          media_type: item.media_type,
          user: state.user,
          item,
        }),
      });
      const req = data.request;
      item.infuse_status = req.status;
      item.in_infuse = req.status === "ready";
      showToast(
        req.status === "ready"
          ? `✓ "${item.title}" is already ready in Infuse!`
          : `⚡ Sent "${item.title}" to Infuse — finding stream...`
      );
      startInfusePolling();
      refreshBootstrap();
      return req;
    } catch (err) {
      showToast(`Send failed: ${err.message}`);
      return null;
    }
  }

  /* ---------- Search & Browse Tab ---------- */

  async function runSearchOrBrowse() {
    const q = $("search-input").value.trim();
    const listEl = $("search-results");
    $("browse-tabs").classList.toggle("hidden", Boolean(q));
    $("search-clear-btn").classList.toggle("hidden", !q);

    listEl.innerHTML = `<div class="empty-card"><p class="muted">Loading...</p></div>`;
    try {
      let items = [];
      if (q) {
        const data = await api(
          `/api/search?q=${encodeURIComponent(q)}&type=${encodeURIComponent(state.searchType)}&user=${encodeURIComponent(state.user)}`
        );
        items = data.results || [];
      } else {
        const data = await api(
          `/api/browse?category=${encodeURIComponent(state.browseCat)}&user=${encodeURIComponent(state.user)}`
        );
        items = data.results || [];
      }
      renderMediaList(items, listEl);
    } catch (err) {
      listEl.innerHTML = `<div class="empty-card"><p class="muted">${esc(err.message)}</p></div>`;
    }
  }

  function renderMediaList(items, container) {
    if (!items.length) {
      container.innerHTML = `<div class="empty-card"><p class="muted">No matching titles found.</p></div>`;
      return;
    }
    container.innerHTML = items
      .map((item, idx) => {
        const ratingTxt = item.rating ? `★ ${Number(item.rating).toFixed(1)}` : "";
        const typeTxt = item.media_type === "show" ? "TV" : "Movie";
        const loved = item.user_reaction === "seen_loved";
        const wanted = item.user_reaction === "want";
        return `
          <div class="media-row-card" data-idx="${idx}">
            <div class="row-poster">${renderPosterHtml(item)}</div>
            <div class="row-info">
              <div>
                <h4 class="row-title">${esc(item.title)} ${item.year ? `<span class="muted-xs">(${item.year})</span>` : ""}</h4>
                <div class="type-badge-row" style="margin-bottom:2px">
                  <span class="badge">${typeTxt}</span>
                  ${ratingTxt ? `<span class="badge badge-rating">${ratingTxt}</span>` : ""}
                  ${item.partner_wanted ? `<span class="badge" style="background:rgba(245,185,66,0.2);color:#F5B942">💛 ${esc(item.partner_name)} wants</span>` : ""}
                </div>
                <p class="row-overview">${esc(item.overview || "")}</p>
              </div>
              <div class="row-actions">
                ${renderInfuseButtonOrBadge(item, true)}
                <button class="btn btn-sm ${wanted ? "btn-gold" : "btn-ghost"} js-list-react" data-idx="${idx}" data-react="want">👍 Want</button>
                <button class="btn btn-sm ${loved ? "btn-primary" : "btn-ghost"} js-list-react" data-idx="${idx}" data-react="seen_loved">❤️ Loved</button>
                ${item.trailer_url ? `<a href="${esc(item.trailer_url)}" target="_blank" rel="noopener" class="btn btn-sm btn-ghost">▶</a>` : ""}
              </div>
            </div>
          </div>
        `;
      })
      .join("");

    container.querySelectorAll(".js-send-infuse").forEach((btn) => {
      const row = btn.closest(".media-row-card");
      const item = items[Number(row.dataset.idx)];
      btn.addEventListener("click", async () => {
        await sendToInfuse(item);
        renderMediaList(items, container);
      });
    });

    container.querySelectorAll(".js-list-react").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const item = items[Number(btn.dataset.idx)];
        const react = btn.dataset.react;
        try {
          const res = await api("/api/react", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              user: state.user,
              trakt_id: item.trakt_id,
              media_type: item.media_type,
              reaction: react,
              item,
            }),
          });
          item.user_reaction = react;
          if (res.matched) {
            showToast(`🎉 Match! "${item.title}" sent to Infuse!`, true);
            startInfusePolling();
          } else {
            showToast(react === "seen_loved" ? `❤️ Added "${item.title}" to favorites` : `👍 Added "${item.title}" to Wants`);
          }
          renderMediaList(items, container);
          refreshBootstrap();
        } catch (err) {
          showToast(err.message);
        }
      });
    });
  }

  /* ---------- Infuse & Taste Tab ---------- */

  async function loadInfuseAndTaste() {
    await Promise.all([refreshInfuseStatus(), loadTasteProfile()]);
  }

  async function refreshInfuseStatus() {
    try {
      const data = await api("/api/infuse/status");
      const reqs = data.requests || [];
      const matches = data.matches || [];

      const qEl = $("infuse-queue-list");
      if (!reqs.length) {
        qEl.innerHTML = `<p class="muted-xs">Nothing sent yet. Tap <strong>Send to Infuse</strong> on any show or movie!</p>`;
      } else {
        qEl.innerHTML = reqs
          .map((r) => {
            const st = r.status || "queued";
            const badge =
              st === "ready"
                ? `<span class="status-pill status-ready">✓ Ready in Infuse</span>`
                : `<span class="status-pill status-${esc(st)}">⏳ Finding stream...</span>`;
            const poster = r.poster_proxy
              ? `<img class="queue-thumb" src="${esc(r.poster_proxy)}" alt="" />`
              : `<div class="queue-thumb"></div>`;
            return `
              <div class="queue-row">
                <div class="queue-left">
                  ${poster}
                  <div style="min-width:0">
                    <div class="queue-title">${esc(r.title)} ${r.year ? `(${r.year})` : ""}</div>
                    <div class="muted-xs">${esc(r.status_detail || `Requested by ${r.requested_by}`)}</div>
                  </div>
                </div>
                ${badge}
              </div>
            `;
          })
          .join("");
      }

      const mEl = $("matches-list");
      if (!matches.length) {
        mEl.innerHTML = `<p class="muted-xs">No mutual matches yet — browse the <strong>For Us</strong> deck!</p>`;
      } else {
        mEl.innerHTML = matches
          .map((m, idx) => {
            const poster = m.poster_proxy
              ? `<img class="queue-thumb" src="${esc(m.poster_proxy)}" alt="" />`
              : `<div class="queue-thumb"></div>`;
            return `
              <div class="queue-row">
                <div class="queue-left">
                  ${poster}
                  <div style="min-width:0">
                    <div class="queue-title">${esc(m.title)} ${m.year ? `(${m.year})` : ""}</div>
                    <div class="muted-xs">${m.media_type === "show" ? "TV Series" : "Movie"}</div>
                  </div>
                </div>
                <div data-match-idx="${idx}">${renderInfuseButtonOrBadge(m, true)}</div>
              </div>
            `;
          })
          .join("");
        mEl.querySelectorAll(".js-send-infuse").forEach((btn) => {
          const idx = Number(btn.parentElement.dataset.matchIdx);
          btn.addEventListener("click", async () => {
            await sendToInfuse(matches[idx]);
            refreshInfuseStatus();
          });
        });
      }

      const hasPending = reqs.some((r) => r.status === "queued" || r.status === "searching");
      if (hasPending) {
        startInfusePolling();
      } else {
        stopInfusePolling();
      }
      refreshBootstrap();
    } catch (err) {
      console.error("Infuse status error:", err);
    }
  }

  function startInfusePolling() {
    if (state.infusePollTimer) return;
    state.infusePollTimer = setInterval(() => {
      refreshInfuseStatus();
    }, 5000);
  }

  function stopInfusePolling() {
    if (state.infusePollTimer) {
      clearInterval(state.infusePollTimer);
      state.infusePollTimer = null;
    }
  }

  async function loadTasteProfile() {
    try {
      const data = await api(`/api/taste?user=${encodeURIComponent(state.user)}`);
      const notes = data.taste_notes || {};
      $("note-shelly").value = notes.shelly || "";
      $("note-mark").value = notes.mark || "";
      $("note-together").value = notes.together || "";

      const combined = [
        ...(data.loved || []).map((x) => ({ ...x, prefix: "❤️" })),
        ...(data.wanted || []).map((x) => ({ ...x, prefix: "👍" })),
      ];
      const listEl = $("taste-items-list");
      if (!combined.length) {
        listEl.innerHTML = `<span class="muted-xs">No in-app likes yet — try the 60s Picker above!</span>`;
      } else {
        listEl.innerHTML = combined
          .map(
            (item) => `
              <span class="taste-chip">
                <span>${item.prefix} ${esc(item.title)}</span>
                <button class="js-undo-react" data-id="${item.trakt_id}" data-type="${esc(item.media_type)}" title="Remove">✕</button>
              </span>
            `
          )
          .join("");
        listEl.querySelectorAll(".js-undo-react").forEach((btn) => {
          btn.addEventListener("click", async () => {
            await api(
              `/api/react?user=${encodeURIComponent(state.user)}&trakt_id=${btn.dataset.id}&media_type=${encodeURIComponent(btn.dataset.type)}`,
              { method: "DELETE" }
            );
            loadTasteProfile();
            refreshBootstrap();
          });
        });
      }

      const nfTop = data.netflix_top || [];
      $("netflix-top-wrap").classList.toggle("hidden", !nfTop.length);
      $("netflix-top-list").innerHTML = nfTop
        .slice(0, 24)
        .map((n) => `<span class="taste-chip">📺 ${esc(n.base_title)} <small class="muted-xs">(${n.play_count})</small></span>`)
        .join("");
    } catch (err) {
      console.error("Load taste error:", err);
    }
  }

  /* ---------- 60s Seeder Modal ---------- */

  async function openSeederModal() {
    $("seeder-modal").classList.remove("hidden");
    $("seeder-grid").innerHTML = `<p class="muted" style="grid-column:1/-1;text-align:center">Loading popular titles...</p>`;
    try {
      const data = await api(`/api/seeder?user=${encodeURIComponent(state.user)}`);
      state.seederItems = data.items || [];
      renderSeederGrid();
    } catch (err) {
      $("seeder-grid").innerHTML = `<p class="muted">${esc(err.message)}</p>`;
    }
  }

  function renderSeederGrid() {
    const filter = $("seeder-filter-input").value.trim().toLowerCase();
    const grid = $("seeder-grid");
    const visible = state.seederItems.filter((x) => !filter || x.title.toLowerCase().includes(filter));
    const pickedCount = state.seederItems.filter((x) => x.user_reaction === "seen_loved").length;
    $("seeder-count-badge").textContent = `${pickedCount} picked`;

    grid.innerHTML = visible
      .map((item) => {
        const picked = item.user_reaction === "seen_loved";
        return `
          <button class="seeder-tile ${picked ? "picked" : ""}" data-id="${item.trakt_id}" data-type="${esc(item.media_type)}">
            ${renderPosterHtml(item)}
            <span class="seeder-heart">${picked ? "❤️" : "+"}</span>
            <div class="seeder-tile-overlay">${esc(item.title)}</div>
          </button>
        `;
      })
      .join("");

    grid.querySelectorAll(".seeder-tile").forEach((tile) => {
      tile.addEventListener("click", async () => {
        const tid = Number(tile.dataset.id);
        const mt = tile.dataset.type;
        const item = state.seederItems.find((x) => Number(x.trakt_id) === tid && x.media_type === mt);
        const already = item.user_reaction === "seen_loved";
        if (already) {
          await api(`/api/react?user=${encodeURIComponent(state.user)}&trakt_id=${tid}&media_type=${encodeURIComponent(mt)}`, { method: "DELETE" });
          item.user_reaction = null;
        } else {
          await api("/api/react", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ user: state.user, trakt_id: tid, media_type: mt, reaction: "seen_loved", item }),
          });
          item.user_reaction = "seen_loved";
        }
        renderSeederGrid();
        refreshBootstrap();
      });
    });
  }

  /* ---------- Settings Modal ---------- */

  function populateModelPresets(provider, selectedModel) {
    const presets = (state.config && state.config.model_presets) || {};
    const info = presets[provider] || { models: [], default: "" };
    const sel = $("set-model-select");
    sel.innerHTML = info.models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    if (selectedModel && info.models.includes(selectedModel)) {
      sel.value = selectedModel;
      $("set-model-custom").value = "";
    } else if (selectedModel) {
      sel.value = info.default || info.models[0] || "";
      $("set-model-custom").value = selectedModel !== sel.value ? selectedModel : "";
    }
  }

  async function openSettingsModal() {
    $("settings-modal").classList.remove("hidden");
    $("ai-test-status").classList.add("hidden");
    try {
      const cfg = await api("/api/settings");
      state.config = cfg;
      $("set-provider").value = cfg.ai_provider;
      populateModelPresets(cfg.ai_provider, cfg.ai_model);
      $("set-api-key").value = "";
      $("set-key-hint").textContent = cfg.has_api_key ? `(Saved: ${cfg.api_key_hint})` : "(Not set yet)";
      $("set-auto-send").checked = Boolean(cfg.auto_send_matches);
      $("set-imessage-match").checked = Boolean(cfg.imessage_on_match);
      $("lan-local-url").textContent = cfg.lan.local_url;
      $("lan-ip-url").textContent = cfg.lan.ip_url;
      $("qr-img").src = `/api/qr?t=${Date.now()}`;

      const boot = await api(`/api/bootstrap?user=${encodeURIComponent(state.user)}`);
      const c = boot.sync_stats.counts;
      $("set-trakt-stats").textContent =
        `Watched: ${c.watched.movies} movies, ${c.watched.shows} shows • Ratings: ${c.rating.total} • Collection: ${c.collection.total} • Netflix imports: ${boot.sync_stats.netflix_count}`;
    } catch (err) {
      showToast(err.message);
    }
  }

  /* ---------- Navigation & Event Wiring ---------- */

  function switchTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));

    $("view-deck").classList.toggle("active", tab === "together" || tab === "me");
    $("view-search").classList.toggle("active", tab === "search");
    $("view-infuse").classList.toggle("active", tab === "infuse");

    if (tab === "together" || tab === "me") {
      updateDeckHeaders();
      loadDeck();
    } else if (tab === "search") {
      runSearchOrBrowse();
    } else if (tab === "infuse") {
      loadInfuseAndTaste();
    }
  }

  function wireEvents() {
    document.querySelectorAll("[data-pick-user]").forEach((btn) => {
      btn.addEventListener("click", () => setActiveUser(btn.dataset.pickUser, true));
    });
    $("switch-shelly").addEventListener("click", () => setActiveUser("shelly", true));
    $("switch-mark").addEventListener("click", () => setActiveUser("mark", true));

    document.querySelectorAll(".nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    $("media-filter-group").querySelectorAll(".seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("media-filter-group").querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.mediaFilter = btn.dataset.mediaType;
        loadDeck();
      });
    });

    $("mood-chips").querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const wasActive = chip.classList.contains("active");
        $("mood-chips").querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
        if (wasActive) {
          $("mood-input").value = "";
        } else {
          chip.classList.add("active");
          $("mood-input").value = chip.dataset.mood;
        }
      });
    });

    $("ask-ai-btn").addEventListener("click", generateNewPicks);
    $("mood-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") generateNewPicks();
    });

    // Search & Browse
    $("search-input").addEventListener("input", () => {
      clearTimeout(state.searchDebounce);
      state.searchDebounce = setTimeout(runSearchOrBrowse, 320);
    });
    $("search-clear-btn").addEventListener("click", () => {
      $("search-input").value = "";
      runSearchOrBrowse();
    });
    $("search-type-group").querySelectorAll(".seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("search-type-group").querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.searchType = btn.dataset.searchType;
        if ($("search-input").value.trim()) runSearchOrBrowse();
      });
    });
    $("browse-tabs").querySelectorAll(".pill").forEach((pill) => {
      pill.addEventListener("click", () => {
        $("browse-tabs").querySelectorAll(".pill").forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");
        state.browseCat = pill.dataset.cat;
        runSearchOrBrowse();
      });
    });

    // Infuse & Taste
    $("refresh-infuse-btn").addEventListener("click", refreshInfuseStatus);
    $("open-seeder-btn").addEventListener("click", openSeederModal);
    $("nudge-seeder-btn").addEventListener("click", openSeederModal);
    $("nudge-dismiss-btn").addEventListener("click", () => {
      sessionStorage.setItem("wn_nudge_dismissed", "1");
      $("shelly-nudge").classList.add("hidden");
    });

    $("netflix-csv-input").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      form.append("user", state.user);
      const resEl = $("netflix-import-result");
      resEl.textContent = "Importing Netflix history...";
      resEl.classList.remove("hidden");
      try {
        const data = await api("/api/import/netflix", { method: "POST", body: form });
        resEl.textContent = `✓ Imported ${data.shows_imported} shows and ${data.movies_imported} movies for ${state.user}!`;
        showToast(`Imported ${data.total_unique} Netflix titles!`);
        loadTasteProfile();
        refreshBootstrap();
      } catch (err) {
        resEl.textContent = `Import error: ${err.message}`;
      }
      e.target.value = "";
    });

    $("save-notes-btn").addEventListener("click", async () => {
      try {
        await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            taste_notes: {
              shelly: $("note-shelly").value.trim(),
              mark: $("note-mark").value.trim(),
              together: $("note-together").value.trim(),
            },
          }),
        });
        showToast("✓ Taste notes saved!");
      } catch (err) {
        showToast(err.message);
      }
    });

    // Seeder Modal
    $("close-seeder-btn").addEventListener("click", () => $("seeder-modal").classList.add("hidden"));
    $("done-seeder-btn").addEventListener("click", () => {
      $("seeder-modal").classList.add("hidden");
      if (state.activeTab === "infuse") loadTasteProfile();
    });
    $("seeder-filter-input").addEventListener("input", renderSeederGrid);

    // Settings Modal
    $("open-settings-btn").addEventListener("click", openSettingsModal);
    $("close-settings-btn").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
    $("set-provider").addEventListener("change", (e) => populateModelPresets(e.target.value, ""));

    $("test-ai-btn").addEventListener("click", async () => {
      const status = $("ai-test-status");
      status.textContent = "Testing AI connection...";
      status.className = "status-line";
      status.classList.remove("hidden");
      const model = $("set-model-custom").value.trim() || $("set-model-select").value;
      try {
        const res = await api("/api/settings/test-ai", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ai_provider: $("set-provider").value,
            ai_model: model,
            ai_api_key: $("set-api-key").value.trim(),
          }),
        });
        if (res.ok) {
          status.textContent = `✓ Connected to ${res.model} (${res.latency_ms} ms)`;
          status.classList.add("ok");
        } else {
          status.textContent = `✕ ${res.error}`;
          status.classList.add("err");
        }
      } catch (err) {
        status.textContent = `✕ ${err.message}`;
        status.classList.add("err");
      }
    });

    const saveSettings = async () => {
      const model = $("set-model-custom").value.trim() || $("set-model-select").value;
      const payload = {
        ai_provider: $("set-provider").value,
        ai_model: model,
        auto_send_matches: $("set-auto-send").checked,
        imessage_on_match: $("set-imessage-match").checked,
      };
      const newKey = $("set-api-key").value.trim();
      if (newKey) payload.ai_api_key = newKey;
      try {
        const cfg = await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        state.config = cfg;
        $("set-api-key").value = "";
        $("set-key-hint").textContent = cfg.has_api_key ? `(Saved: ${cfg.api_key_hint})` : "(Not set yet)";
        showToast("✓ Settings saved!");
        refreshBootstrap();
      } catch (err) {
        showToast(err.message);
      }
    };

    $("save-ai-btn").addEventListener("click", saveSettings);
    $("set-auto-send").addEventListener("change", saveSettings);
    $("set-imessage-match").addEventListener("change", saveSettings);

    $("sync-trakt-btn").addEventListener("click", async () => {
      const btn = $("sync-trakt-btn");
      btn.disabled = true;
      btn.textContent = "Syncing Trakt...";
      try {
        const res = await api("/api/trakt/sync", { method: "POST" });
        const c = res.stats.counts;
        $("set-trakt-stats").textContent =
          `Watched: ${c.watched.movies} movies, ${c.watched.shows} shows • Ratings: ${c.rating.total} • Collection: ${c.collection.total} • Netflix imports: ${res.stats.netflix_count}`;
        showToast("✓ Trakt sync complete!");
      } catch (err) {
        showToast(`Sync failed: ${err.message}`);
      } finally {
        btn.disabled = false;
        btn.textContent = "Sync Trakt History Now";
      }
    });
  }

  function init() {
    wireEvents();
    if (!state.user || (state.user !== "mark" && state.user !== "shelly")) {
      $("profile-overlay").classList.remove("hidden");
    } else {
      setActiveUser(state.user, false);
    }
  }

  init();
})();
