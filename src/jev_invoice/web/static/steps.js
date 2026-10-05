// Invoice page: while the invoice is processing, fetch its steps every second and swap
// them in. When processing finishes, reload once so the decision, payment and review
// sections appear. Keeps open steps open.
(() => {
  const box = document.querySelector("[data-steps]");
  if (!box) return;
  const url = box.dataset.liveUrl;

  const finished = () => {
    const steps = box.querySelector(".steps");
    return !steps || steps.dataset.finished === "true";
  };
  if (finished()) return;

  const tick = async () => {
    const open = [...box.querySelectorAll("details[open]")].map((d) => d.id);
    try {
      const response = await fetch(url, { headers: { Accept: "text/html" } });
      if (response.ok) {
        box.innerHTML = await response.text();
        open.forEach((id) => {
          const details = document.getElementById(id);
          if (details) details.open = true;
        });
      }
    } catch (error) {
      // Network blip: try again on the next tick.
    }
    if (finished()) {
      window.location.reload();
      return;
    }
    setTimeout(tick, 1000);
  };

  setTimeout(tick, 1000);
})();
