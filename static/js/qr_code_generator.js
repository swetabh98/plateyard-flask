(function () {
  const cfg = window.QR_GENERATOR_CONFIG || {};
  const $ = (s, r = document) => r.querySelector(s);

  let sessionUserName = "";

  const state = {
    editingBatch: null,
    currentRecord: null
  };

  function text(v) {
    return (v == null ? "" : String(v)).trim();
  }

  function esc(v) {
    return text(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function formatIST(isoStr) {
    if (!isoStr || isoStr === "-") return "-";
    try {
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return isoStr;
      // Convert to IST (UTC+5:30)
      const ist = new Date(d.getTime() + (5 * 60 + 30) * 60 * 1000);
      const day   = String(ist.getUTCDate()).padStart(2, "0");
      const month = String(ist.getUTCMonth() + 1).padStart(2, "0");
      const year  = String(ist.getUTCFullYear()).slice(2);
      let   hours = ist.getUTCHours();
      const mins  = String(ist.getUTCMinutes()).padStart(2, "0");
      const ampm  = hours >= 12 ? "PM" : "AM";
      hours = hours % 12 || 12;
      return `${day}-${month}-${year} & ${hours}:${mins} ${ampm}`;
    } catch (e) {
      return isoStr;
    }
  }

  function buildGetApi(batch) {
    return `/api/qr/batch/${encodeURIComponent(batch)}`;
  }

  function notify(message, type) {
    const box = $("#qr-generator-message");
    if (!box) return;
    box.textContent = message || "";
    box.className = `qr-message ${type ? `is-${type}` : ""}`;
    box.style.display = message ? "block" : "none";
  }

  function getFormPayload() {
    return {
      batch: text($("#qr-batch")?.value),
      v_length: text($("#qr-length")?.value),
      v_width: text($("#qr-width")?.value),
      v_thickness: text($("#qr-thickness")?.value),
      v_pieces: text($("#qr-pieces")?.value),
      grade: text($("#qr-grade")?.value),
      user_name: sessionUserName,
      action_type: state.editingBatch ? "edited" : "generated"
    };
  }

  function setFormValues(record) {
    $("#qr-batch").value = text(record.Batch || record.batch || "");
    $("#qr-length").value = text(record.V_LENGTH || record.v_length || "");
    $("#qr-width").value = text(record.V_WIDTH || record.v_width || "");
    $("#qr-thickness").value = text(record.V_THICKNESS || record.v_thickness || "");
    $("#qr-pieces").value = text(record.V_PIECES || record.v_pieces || "");
    $("#qr-grade").value = text(record.V_EXT_GRADE || record.v_ext_grade || record.grade || "");
  }

  function clearForm() {
    setFormValues({});
    state.editingBatch = null;

    const btn = $("#qr-generate-btn");
    if (btn) btn.textContent = "Generate QR Code";

    const cancelBtn = $("#qr-cancel-edit-btn");
    if (cancelBtn) cancelBtn.style.display = "none";
  }

  function validatePayload(payload) {
    if (!text(payload.batch)) {
      notify("Batch ID is required.", "error");
      return false;
    }
    if (!text(payload.v_pieces)) {
      notify("Pieces is required.", "error");
      return false;
    }
    return true;
  }

  function setTopLinks(record) {
    const meta = (record && record.__meta) || {};

    const openDetail = $("#qr-open-detail-current");
    const openPrint = $("#qr-open-print");

    if (openDetail) {
      openDetail.href = meta.current_detail_url || "#";
      openDetail.style.pointerEvents = meta.current_detail_url ? "auto" : "none";
      openDetail.style.opacity = meta.current_detail_url ? "1" : ".5";
    }

    if (openPrint) {
      openPrint.href = meta.current_print_url || "#";
      openPrint.style.pointerEvents = meta.current_print_url ? "auto" : "none";
      openPrint.style.opacity = meta.current_print_url ? "1" : ".5";
    }
  }

  function setPreviewImages(record) {
    const holder = $("#qr-preview-holder");
    const empty = $("#qr-preview-empty");
    const title = $("#qr-preview-batch");

    const combinedImg = $("#qr-preview-combined-image");
    const combinedLink = $("#qr-open-combined-label");

    const meta = (record && record.__meta) || {};
    const batch = text(meta.batch || record.Batch || "");

    if (title) {
      title.textContent = batch ? `Generated QR Label - ${batch}` : "Generated QR Label";
    }

    if (!holder || !combinedImg) return;

    if (!meta.current_public_image_url) {
      holder.style.display = "none";
      if (empty) empty.style.display = "block";
      return;
    }

    // Now uses the single combined image from the server
    const imgSrc = `${meta.current_public_image_url}&ts=${Date.now()}`;
    combinedImg.src = imgSrc;

    if (combinedLink) combinedLink.href = meta.current_public_image_url;

    holder.style.display = "flex";
    if (empty) empty.style.display = "none";
  }

  function renderDetailPreview(record) {
    const body = $("#qr-detail-preview");
    if (!body) return;

    const meta = (record && record.__meta) || {};
    const rows = [
      ["Batch", record.Batch || "-"],
      ["Length", record.V_LENGTH || "-"],
      ["Width", record.V_WIDTH || "-"],
      ["Thickness", record.V_THICKNESS || "-"],
      ["Pieces", record.V_PIECES || "-"],
      ["Grade", record.V_EXT_GRADE || "-"],
      ["BinNo", record.BinNo || "-"],
      ["CustomerName", record.CustomerName || "-"],
      ["SO No", record["SO No"] || "-"],
      ["SO_ITEM", record.SO_ITEM || "-"],
      ["Material", record.Material || "-"],
      ["Qty", record.Qty || "-"],
      ["Status", record.Status || "-"]
    ];

    body.innerHTML = `
      <table class="qr-detail-table compact-table">
        <tbody>
          ${rows.map(([k, v]) => {
            if (k === "BinNo") {
              return `
                <tr>
                  <th>${esc(k)}</th>
                  <td>
                    <div class="qr-value-cell">
                      <span>${esc(v)}</span>
                      <a class="mini-btn btn-edit" href="${esc(meta.update_bin_url || "#")}" target="_blank" rel="noopener">Update BinNo</a>
                    </div>
                  </td>
                </tr>
              `;
            }
            return `
              <tr>
                <th>${esc(k)}</th>
                <td>${esc(v)}</td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    `;
  }

  async function fetchList() {
    const search = text($("#qr-search-input")?.value);
    const tbody = $("#qr-list-body");
    if (!tbody) return;

    tbody.innerHTML = `<tr><td colspan="8" class="qr-empty-row">Loading...</td></tr>`;

    try {
      const res = await fetch(`${cfg.listApi}?search=${encodeURIComponent(search)}`, {
        credentials: "same-origin"
      });
      const data = await res.json().catch(() => ({}));

      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Could not load stored QR codes.");
      }

      const rows = Array.isArray(data.rows) ? data.rows : [];
      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="qr-empty-row">No QR codes found.</td></tr>`;
        return;
      }

      tbody.innerHTML = rows.map((row) => {
        const batch = esc(row.batch);
        return `
          <tr>
            <td>${batch}</td>
            <td>${esc(row.v_length || "-")}</td>
            <td>${esc(row.v_width || "-")}</td>
            <td>${esc(row.v_thickness || "-")}</td>
            <td>${esc(row.v_pieces || "-")}</td>
            <td>${esc(row.grade || "-")}</td>
            <td>${esc(formatIST(row.updated_at))}</td>
            <td>
              <div class="qr-row-actions">
                <button type="button" class="mini-btn btn-view" data-action="view" data-batch="${batch}">View</button>
                <button type="button" class="mini-btn btn-edit" data-action="edit" data-batch="${batch}">Edit</button>
                <button type="button" class="mini-btn btn-print" data-action="print" data-batch="${batch}">Print</button>
                <button type="button" class="mini-btn btn-delete danger-btn" data-action="delete" data-batch="${batch}">Delete</button>
              </div>
            </td>
          </tr>
        `;
      }).join("");
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="8" class="qr-empty-row">${esc(err.message || "Failed to load data.")}</td></tr>`;
    }
  }

  async function loadBatch(batch, editMode) {
    try {
      const res = await fetch(buildGetApi(batch), { credentials: "same-origin" });
      const data = await res.json().catch(() => ({}));

      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Could not load batch.");
      }

      const record = data.record || {};
      state.currentRecord = record;

      if (editMode) {
        setFormValues(record);
        state.editingBatch = text(record.Batch || batch);
        $("#qr-generate-btn").textContent = "Update QR Code";
        $("#qr-cancel-edit-btn").style.display = "inline-flex";
        notify(`Editing Batch ${state.editingBatch}`, "info");
        window.scrollTo({ top: 0, behavior: "smooth" });
      } else {
        const meta = record.__meta || {};
        const detailUrl = meta.current_detail_url || "#";
        window.open(detailUrl, "_blank", "noopener");
      }
    } catch (err) {
      notify(err.message || "Failed to load batch.", "error");
    }
  }

  async function deleteBatch(batch) {
    if (!confirm(`Delete stored QR code for batch ${batch}?`)) return;

    try {
      const res = await fetch(cfg.deleteApi, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ batch, user_name: sessionUserName })
      });
      const data = await res.json().catch(() => ({}));

      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Delete failed.");
      }

      if (state.editingBatch && state.editingBatch === batch) {
        clearForm();
      }

      notify(`Deleted batch ${batch}.`, "success");
      await fetchList();
    } catch (err) {
      notify(err.message || "Delete failed.", "error");
    }
  }

  async function handleGenerate(ev) {
    ev.preventDefault();

    const payload = getFormPayload();
    if (!validatePayload(payload)) return;

    notify("Generating QR labels...", "info");

    try {
      const res = await fetch(cfg.generateApi, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(() => ({}));

      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Generation failed.");
      }

      const record = data.record || {};
      state.currentRecord = record;
      state.editingBatch = null;

      setTopLinks(record);
      setPreviewImages(record);
      renderDetailPreview(record);

      $("#qr-generate-btn").textContent = "Generate QR Code";
      $("#qr-cancel-edit-btn").style.display = "none";

      notify("QR labels generated successfully.", "success");
      await fetchList();
    } catch (err) {
      notify(err.message || "Generation failed.", "error");
    }
  }

  function bindListActions() {
    const tbody = $("#qr-list-body");
    if (!tbody) return;

    tbody.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-action]");
      if (!btn) return;

      const action = btn.getAttribute("data-action");
      const batch = text(btn.getAttribute("data-batch"));
      if (!batch) return;

      if (action === "view") {
        await loadBatch(batch, false);
      } else if (action === "edit") {
        await loadBatch(batch, true);
      } else if (action === "print") {
        try {
          const res = await fetch(buildGetApi(batch), { credentials: "same-origin" });
          const data = await res.json().catch(() => ({}));

          if (!res.ok || !data.ok) {
            throw new Error(data.error || "Could not load batch.");
          }

          const record = data.record || {};
          const meta = record.__meta || {};
          const printUrl = meta.current_print_url || "#";
          window.open(printUrl, "_blank", "noopener");
        } catch (err) {
          notify(err.message || "Could not open print page.", "error");
        }
      } else if (action === "delete") {
        await deleteBatch(batch);
      }
    });
  }

  function initGeneratorPage() {
    const form = $("#qr-generator-form");
    if (!form) return;

    // Trigger Name Modal logic
    const nameModal = $("#qr-name-modal");
    const nameInput = $("#qr-name-input");
    const nameSubmit = $("#qr-name-submit");

    if (nameModal) {
      nameModal.style.display = "flex";
      nameSubmit.addEventListener("click", () => {
        const val = nameInput.value.trim();
        if (val) {
          sessionUserName = val;
          nameModal.style.display = "none";
        } else {
          alert("Please enter your name.");
        }
      });
      nameInput.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") nameSubmit.click();
      });
    }

    // Trigger History Modal logic
    const historyBtn = $("#qr-view-history-btn");
    const historyModal = $("#qr-history-modal");
    const historyClose = $("#qr-history-close");
    const historyBody = $("#qr-history-body");

    if (historyBtn && historyModal) {
      historyBtn.addEventListener("click", async () => {
        historyBody.innerHTML = '<tr><td colspan="4" class="qr-empty-row">Loading...</td></tr>';
        historyModal.style.display = "flex";
        try {
          const res = await fetch(cfg.historyApi);
          const data = await res.json();
          if (data.ok && data.history.length > 0) {
            historyBody.innerHTML = data.history.map(row => {
              const d = formatIST(row.timestamp);
              return `<tr>
                <td>${d}</td>
                <td><strong>${esc(row.user_name)}</strong></td>
                <td>${esc(row.action)}</td>
                <td>${esc(row.batch)}</td>
              </tr>`;
            }).join("");
          } else {
            historyBody.innerHTML = '<tr><td colspan="4" class="qr-empty-row">No history found.</td></tr>';
          }
        } catch (e) {
          historyBody.innerHTML = '<tr><td colspan="4" class="qr-empty-row">Failed to load history.</td></tr>';
        }
      });

      historyClose.addEventListener("click", () => {
        historyModal.style.display = "none";
      });
    }

    form.addEventListener("submit", handleGenerate);

    const searchBtn = $("#qr-search-btn");
    if (searchBtn) searchBtn.addEventListener("click", fetchList);

    const refreshBtn = $("#qr-refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", fetchList);

    const searchInput = $("#qr-search-input");
    if (searchInput) {
      searchInput.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          fetchList();
        }
      });
    }

    const cancelEditBtn = $("#qr-cancel-edit-btn");
    if (cancelEditBtn) {
      cancelEditBtn.addEventListener("click", () => {
        clearForm();
        notify("", "");
      });
    }

    bindListActions();
    fetchList();
  }

  document.addEventListener("DOMContentLoaded", initGeneratorPage);
})();