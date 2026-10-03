// Journey page: while the invoice is processing, fetch the journey again every second
// and swap it in. Stops when the server says it is finished. Keeps open <details> open.
(() => {
  const box = document.querySelector("[data-journey]");
  if (!box) return;
  const url = box.dataset.liveUrl;

  const finished = () => {
    const journey = box.querySelector(".journey");
    return !journey || journey.dataset.finished === "true";
  };

  const tick = async () => {
    if (finished()) return;
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
    setTimeout(tick, 1000);
  };

  setTimeout(tick, 1000);
})();
