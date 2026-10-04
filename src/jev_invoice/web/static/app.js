// Inbox: while any invoice is processing, reload the page every 3 seconds to show progress
// (only when the upload form is not in use, so a chosen file is never lost).
document.addEventListener("DOMContentLoaded", () => {
  if (!document.querySelector("[data-processing]")) return;
  const input = document.querySelector('[data-upload] input[type="file"]');
  setInterval(() => {
    if (!input || !input.files.length) window.location.reload();
  }, 3000);
});

// Upload form: show the chosen file name, accept drag and drop, and show progress
// while the file uploads (a plain form post; this only adds feedback).
document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("[data-upload]");
  if (!form) return;
  const input = form.querySelector('input[type="file"]');
  const fileName = form.querySelector("[data-file-name]");
  const progress = form.querySelector("[data-progress]");
  const button = form.querySelector('button[type="submit"]');

  const showName = () => {
    fileName.textContent = input.files.length ? `Selected: ${input.files[0].name}` : "";
  };
  input.addEventListener("change", showName);

  ["dragenter", "dragover"].forEach((type) =>
    form.addEventListener(type, (event) => {
      event.preventDefault();
      form.classList.add("is-dragging");
    })
  );
  ["dragleave", "drop"].forEach((type) =>
    form.addEventListener(type, () => form.classList.remove("is-dragging"))
  );
  form.addEventListener("drop", (event) => {
    event.preventDefault();
    if (event.dataTransfer.files.length) {
      input.files = event.dataTransfer.files;
      showName();
    }
  });

  form.addEventListener("submit", () => {
    if (!input.files.length) return;
    button.disabled = true;
    button.textContent = "Processing…";
    progress.hidden = false;
  });
});

// Copy a read-only text box (the dispute message) to the clipboard.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const box = document.getElementById(button.dataset.copy);
  const status = button.parentElement.querySelector("[data-copy-status]");
  try {
    await navigator.clipboard.writeText(box.value);
    if (status) status.textContent = "Copied. Paste it into your email to the vendor.";
  } catch {
    box.select();
    if (status) status.textContent = "Select all and copy (Ctrl+C).";
  }
});

// Payment form: ticking or unticking the discount box updates the amount to pay.
document.addEventListener("change", (event) => {
  const box = event.target;
  if (!box.matches("[data-payment] input[name=take_discount]")) return;
  const amount = box.form.querySelector("input[name=amount]");
  const full = Number(box.dataset.full);
  const discount = Number(box.dataset.discount);
  amount.value = (box.checked ? full - discount : full).toFixed(2);
});
