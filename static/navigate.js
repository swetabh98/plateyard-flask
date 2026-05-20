// static/navigate.js
(function () {
  const form = document.getElementById("navigateForm");
  const batchInput = document.getElementById("navigateBatchId");
  const baySelect = document.getElementById("navigateBay");
  const searchBtn = document.getElementById("navigateSearchBtn");
  const resetBtn = document.getElementById("navigateResetBtn");
  const messageBox = document.getElementById("navigateMessage");

  const summaryWrap = document.getElementById("navigateSummary");
  const resultsWrap = document.getElementById("navigateResults");
  const emptyWrap = document.getElementById("navigateEmpty");
  const binsContainer = document.getElementById("binsContainer");

  const summaryBatch = document.getElementById("summaryBatch");
  const summaryCustomer = document.getElementById("summaryCustomer");
  const summaryBay = document.getElementById("summaryBay");
  const summaryBins = document.getElementById("summaryBins");
  const summaryItems = document.getElementById("summaryItems");
  const summaryWeight = document.getElementById("summaryWeight");
  const resultsTitle = document.getElementById("resultsTitle");
  const filterInput = document.getElementById("navigateFilter");

  const esc = (value) =>
    String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");

  const cleanText = (value, fallback = "-") => {
    const s = String(value ?? "").trim();
    return s ? s : fallback;
  };

  const toNumber = (value) => {
    const n = Number(String(value ?? "").replace(/,/g, ""));
    return Number.isFinite(n) ? n : 0;
  };

  const formatWeight = (value) => `${toNumber(value).toFixed(3)} t`;

  function refreshIcons() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }

  function setLoading(isLoading) {
    if (!searchBtn) return;

    searchBtn.disabled = isLoading;
    searchBtn.classList.toggle("is-loading", isLoading);

    const label = searchBtn.querySelector("span");
    if (label) label.textContent = isLoading ? "Searching..." : "Find Bins";
  }

  function showMessage(type, message) {
    if (!messageBox) return;

    messageBox.className = `navigate-message ${type || ""}`.trim();
    messageBox.textContent = message || "";
    messageBox.style.display = message ? "block" : "none";
  }

  function clearUI() {
    showMessage("", "");

    if (summaryWrap) summaryWrap.hidden = true;
    if (resultsWrap) resultsWrap.hidden = true;
    if (emptyWrap) emptyWrap.hidden = true;
    if (binsContainer) binsContainer.innerHTML = "";
    if (filterInput) filterInput.value = "";
  }

  function setSummary(data) {
    if (summaryBatch) summaryBatch.textContent = cleanText(data.batch_id);
    if (summaryCustomer) summaryCustomer.textContent = cleanText(data.customer);
    if (summaryBay) summaryBay.textContent = cleanText(data.bay_label || data.bay);
    if (summaryBins) summaryBins.textContent = String(data.total_bins || 0);
    if (summaryItems) summaryItems.textContent = String(data.total_items || 0);
    if (summaryWeight) summaryWeight.textContent = formatWeight(data.total_weight || 0);

    if (resultsTitle) {
      resultsTitle.textContent = `${cleanText(data.customer)} - ${cleanText(data.bay_label || data.bay)}`;
    }

    if (summaryWrap) summaryWrap.hidden = false;
  }

  function dimensionText(item) {
    return [item.length, item.width, item.thickness]
      .map((v) => String(v ?? "").trim())
      .filter(Boolean)
      .join(" x ");
  }

  function itemSearchBlob(item) {
    return [
      item.plate_id,
      item.type,
      item.bin,
      item.seq,
      item.status,
      item.customer,
      item.grade,
      item.length,
      item.width,
      item.thickness,
      item.pieces,
      item.weight,
      item.dispatch_mode,
      item.FI_Rel_text,
      item.SBU_RelStatus,
      item.CustomerCity,
      item.Material_Status,
      item.added_at,
      item.created_at,
      item.updated_at,
    ]
      .map((v) => String(v ?? "").toLowerCase())
      .join(" ");
  }

  function renderTableRow(item, index) {
    const dim = dimensionText(item);

    return `
      <tr class="navigate-item-row" data-search="${esc(itemSearchBlob(item))}">
        <td class="navigate-mono navigate-strong">${esc(cleanText(item.plate_id))}</td>
        <td>${esc(cleanText(item.type))}</td>
        <td><span class="navigate-status">${esc(cleanText(item.status))}</span></td>
        <td>${esc(cleanText(item.grade))}</td>
        <td>${esc(cleanText(dim))}</td>
        <td class="navigate-mono">${esc(cleanText(item.pieces))}</td>
        <td class="navigate-mono">${esc(cleanText(item.weight))}</td>
        <td>${esc(cleanText(item.FI_Rel_text))}</td>
        <td>${esc(cleanText(item.SBU_RelStatus))}</td>
        <td>${esc(cleanText(item.CustomerCity))}</td>
        <td>${esc(cleanText(item.Material_Status))}</td>
        <td class="navigate-mono">${esc(cleanText(item.dispatch_mode))}</td>
        <td class="navigate-mono">${esc(cleanText(item.seq, String(index + 1)))}</td>
      </tr>
    `;
  }

  function renderBinTable(bin) {
    const items = Array.isArray(bin.items) ? bin.items : [];
    const rows = items.map(renderTableRow).join("");

    return `
      <section class="navigate-bin-table-card" data-navigate-bin data-bin="${esc(bin.bin)}">
        <div class="navigate-bin-table-head">
          <div class="navigate-bin-title-wrap">
            <h3>${esc(cleanText(bin.bin))}</h3>
            <p>${esc(cleanText(bin.bay))} - ${Number(bin.count || items.length)} item${Number(bin.count || items.length) === 1 ? "" : "s"}</p>
          </div>
          <span class="navigate-bin-weight">${formatWeight(bin.total_weight || 0)}</span>
        </div>

        <div class="navigate-table-scroll" role="region" aria-label="Bin ${esc(bin.bin)} material table" tabindex="0">
          <table class="navigate-detail-table">
            <thead>
              <tr>
                <th>Plate / Coil ID</th>
                <th>Type</th>
                <th>Status</th>
                <th>Grade</th>
                <th>L x W x T</th>
                <th>Pieces</th>
                <th>Weight</th>
                <th>FI</th>
                <th>SBU</th>
                <th>City</th>
                <th>Material Status</th>
                <th>Dispatch</th>
                <th>Seq</th>
              </tr>
            </thead>
            <tbody>
              ${rows || `<tr><td colspan="13" class="navigate-muted-cell">No items found.</td></tr>`}
            </tbody>
          </table>
        </div>
      </section>
    `;
  }

  function renderResults(data) {
    setSummary(data);

    const bins = Array.isArray(data.bins) ? data.bins : [];
    if (!bins.length) {
      if (resultsWrap) resultsWrap.hidden = true;
      if (emptyWrap) emptyWrap.hidden = false;
      return;
    }

    if (emptyWrap) emptyWrap.hidden = true;
    if (resultsWrap) resultsWrap.hidden = false;
    if (binsContainer) binsContainer.innerHTML = bins.map(renderBinTable).join("");

    refreshIcons();
  }

  async function searchNavigate() {
    const batchId = String(batchInput?.value || "").trim();
    const bay = String(baySelect?.value || "").trim();

    clearUI();

    if (!batchId) {
      showMessage("error", "Please enter Batch ID.");
      batchInput?.focus();
      return;
    }

    if (!bay) {
      showMessage("error", "Please select a bay.");
      baySelect?.focus();
      return;
    }

    setLoading(true);

    try {
      const response = await fetch("/api/navigate/search", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({ batch_id: batchId, bay }),
      });

      const data = await response.json().catch(() => ({}));

      if (!response.ok || !data.ok) {
        showMessage("error", data.error || "Search failed.");
        return;
      }

      showMessage("success", `Customer found: ${data.customer}. Showing material in ${data.bay_label}.`);
      renderResults(data);
    } catch (error) {
      console.error("[Navigate] search failed:", error);
      showMessage("error", "Network or server error while searching.");
    } finally {
      setLoading(false);
    }
  }

  function applyFilter() {
    const query = String(filterInput?.value || "").trim().toLowerCase();

    document.querySelectorAll("[data-navigate-bin]").forEach((section) => {
      let visibleRows = 0;

      section.querySelectorAll(".navigate-item-row").forEach((row) => {
        const searchText = row.getAttribute("data-search") || "";
        const visible = !query || searchText.includes(query);
        row.hidden = !visible;
        if (visible) visibleRows += 1;
      });

      section.hidden = Boolean(query) && visibleRows === 0;
    });
  }

  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    searchNavigate();
  });

  resetBtn?.addEventListener("click", () => {
    if (batchInput) batchInput.value = "";
    if (baySelect) baySelect.value = "";
    clearUI();
    batchInput?.focus();
  });

  filterInput?.addEventListener("input", applyFilter);

  refreshIcons();
})();
