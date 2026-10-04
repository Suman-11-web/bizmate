(() => {
  const toast = document.querySelector("[data-toast]");
  const toastMessage = document.querySelector("[data-toast-message]");
  let toastTimer;

  function showToast(message) {
    if (!toast) return;
    if (toastMessage) toastMessage.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("show"), 2600);
  }

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

  const planChoices = [...document.querySelectorAll("[data-plan-price]")];
  const paymentReference = document.querySelector("[data-payment-reference]");
  const paymentHelp = document.querySelector("[data-payment-help]");
  function updatePlanPaymentState() {
    if (!planChoices.length || !paymentReference) return;
    const selected = planChoices.find((choice) => choice.checked);
    const paidPlan = Number(selected?.dataset.planPrice || 0) > 0;
    paymentReference.required = paidPlan;
    paymentReference.setAttribute("aria-required", String(paidPlan));
    if (paymentHelp) {
      paymentHelp.textContent = paidPlan
        ? "Required for the selected paid plan. Use 8–32 letters or numbers exactly as shown in your UPI app."
        : "Optional for the free Starter plan; leave blank when no payment is due.";
    }
    planChoices.forEach((choice) => choice.closest(".seller-plan-card")?.classList.toggle("selected", choice.checked));
  }
  planChoices.forEach((choice) => choice.addEventListener("change", updatePlanPaymentState));
  updatePlanPaymentState();

  const navToggle = document.querySelector("[data-nav-toggle]");
  const siteNav = document.querySelector("[data-site-nav]");
  navToggle?.addEventListener("click", () => {
    const open = siteNav?.classList.toggle("open") ?? false;
    navToggle.setAttribute("aria-expanded", String(open));
  });
  siteNav?.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => {
      siteNav.classList.remove("open");
      navToggle?.setAttribute("aria-expanded", "false");
    });
  });

  const sidebar = document.querySelector("#sidebar");
  const menuToggle = document.querySelector("[data-menu-toggle]");
  menuToggle?.addEventListener("click", () => {
    const open = sidebar?.classList.toggle("open") ?? false;
    menuToggle.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (event) => {
    if (sidebar?.classList.contains("open") && !sidebar.contains(event.target) && !menuToggle?.contains(event.target)) {
      sidebar.classList.remove("open");
      menuToggle?.setAttribute("aria-expanded", "false");
    }
  });

  document.querySelectorAll("[data-add-to-cart]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("button[type='submit']");
      if (button) button.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          headers: { Accept: "application/json", "X-CSRFToken": csrfToken },
          body: new FormData(form),
        });
        const result = await response.json();
        if (!response.ok) {
          showToast(result.message || "Could not add this item.");
          return;
        }
        document.querySelectorAll("[data-cart-count]").forEach((badge) => {
          badge.textContent = result.cart_count;
        });
        showToast(result.message || "Added to your cart");
      } catch (_error) {
        showToast("Something went wrong. Please try again.");
      } finally {
        if (button) button.disabled = false;
      }
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "Are you sure?")) event.preventDefault();
    });
  });

  const tableSearch = document.querySelector("[data-table-search]");
  const categoryFilter = document.querySelector("[data-category-filter]");
  const inventorySort = document.querySelector("[data-inventory-sort]");
  function updateInventoryTable() {
    const body = document.querySelector(".inventory-table tbody");
    if (!body) return;
    const query = tableSearch?.value.trim().toLowerCase() || "";
    const category = categoryFilter?.value || "all";
    const rows = [...body.querySelectorAll("tr")];
    rows.forEach((row) => {
      row.hidden = (query && !row.textContent.toLowerCase().includes(query)) || (category !== "all" && row.dataset.category !== category);
    });
    const sort = inventorySort?.value || "stock";
    rows.sort((a, b) => {
      if (sort === "name") return a.dataset.name.localeCompare(b.dataset.name);
      if (sort === "price") return Number(a.dataset.price) - Number(b.dataset.price);
      return Number(a.dataset.stock) - Number(b.dataset.stock);
    }).forEach((row) => body.append(row));
  }
  tableSearch?.addEventListener("input", updateInventoryTable);
  categoryFilter?.addEventListener("change", updateInventoryTable);
  inventorySort?.addEventListener("change", updateInventoryTable);

  const globalSearch = document.querySelector(".global-search input");
  globalSearch?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      const role = document.body.dataset.role;
      const target = role === "owner" ? "/owner/sellers" : role === "seller" ? "/seller/products" : "/marketplace";
      window.location.href = `${target}?q=${encodeURIComponent(globalSearch.value.trim())}`;
    }
  });

  const chatForm = document.querySelector("[data-ai-form]");
  const chatMessages = document.querySelector("[data-chat-messages]");
  const chatInput = chatForm?.querySelector("input[name='message']");

  function appendMessage(text, kind) {
    if (!chatMessages) return;
    const message = document.createElement("div");
    message.className = `message ${kind === "user" ? "user-message" : "ai-message"}`;
    const bubble = document.createElement("div");
    bubble.textContent = text;
    if (kind === "assistant") {
      const icon = document.createElement("span");
      icon.className = "ai-avatar small";
      icon.textContent = "✦";
      message.append(icon, bubble);
    } else {
      message.append(bubble);
    }
    chatMessages.append(message);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  async function askAssistant(message) {
    if (!message) return;
    appendMessage(message, "user");
    if (chatInput) chatInput.value = "";
    const sendButton = chatForm?.querySelector("button[type='submit']");
    if (sendButton) sendButton.disabled = true;
    try {
      const response = await fetch("/seller/assistant", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrfToken },
        body: JSON.stringify({ message }),
      });
      if (!response.ok) throw new Error("Assistant unavailable");
      const result = await response.json();
      appendMessage(result.answer, "assistant");
    } catch (_error) {
      appendMessage("I could not reach the assistant just now. Please try again.", "assistant");
    } finally {
      if (sendButton) sendButton.disabled = false;
      chatInput?.focus();
    }
  }

  chatForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    askAssistant(chatInput?.value.trim() || "");
  });
  document.querySelectorAll("[data-ai-prompt]").forEach((button) => {
    button.addEventListener("click", () => askAssistant(button.dataset.aiPrompt || ""));
  });

  const imageInput = document.querySelector("[data-image-input]");
  const imagePreview = document.querySelector("[data-image-preview]");
  const previewImage = imagePreview?.querySelector("img") || document.querySelector("[data-current-image]");
  const imageName = document.querySelector("[data-image-name]");
  let previewUrl;
  imageInput?.addEventListener("change", () => {
    const file = imageInput.files?.[0];
    if (!file) return;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(file);
    if (previewImage) previewImage.src = previewUrl;
    if (imageName) imageName.textContent = file.name;
    if (imagePreview) imagePreview.hidden = false;
  });
})();
