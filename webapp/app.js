const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  try {
    tg.setHeaderColor("secondary_bg_color");
    tg.setBackgroundColor("bg_color");
  } catch (e) {}
}

const state = {
  menu: null,
  qty: {},
  closed: false,
  cardId: null,
  query: "",
};

const $menu = document.getElementById("menu");
const $title = document.getElementById("title");
const $deadline = document.getElementById("deadline");
const $sum = document.getElementById("cart-sum");
const $hint = document.getElementById("cart-hint");
const $save = document.getElementById("save");
const $status = document.getElementById("status");
const $card = document.getElementById("card");
const $cardPhoto = document.getElementById("card-photo");
const $cardName = document.getElementById("card-name");
const $cardMeta = document.getElementById("card-meta");
const $cardDesc = document.getElementById("card-desc");
const $cardBgu = document.getElementById("card-bgu");
const $cardQty = document.getElementById("card-qty");
const $cardClose = document.getElementById("card-close");
const $search = document.getElementById("search");

function initData() {
  return (tg && tg.initData) || "";
}

function headers() {
  const h = { "Content-Type": "application/json" };
  const data = initData();
  if (data) h["X-Telegram-Init-Data"] = data;
  return h;
}

function setStatus(text, isError) {
  $status.textContent = text || "";
  $status.className = isError ? "error" : "";
}

function dishById(id) {
  if (!state.menu) return null;
  for (const cat of state.menu.categories) {
    for (const dish of cat.dishes) {
      if (dish.id === Number(id)) return dish;
    }
  }
  return null;
}

function priceText(dish) {
  if (!dish) return "";
  if (dish.available === false) return "Сейчас недоступно";
  if (dish.weighty) return `≈ ${dish.price} ₽, точно по факту`;
  return `${dish.price} ₽`;
}

function changeQty(id, d) {
  const dish = dishById(id);
  if (state.closed) return;
  if (dish && dish.available === false && d > 0) return;
  const next = Math.max(0, Math.min(99, (state.qty[id] || 0) + d));
  if (next === 0) delete state.qty[id];
  else state.qty[id] = next;
  render();
}

function closeCard() {
  state.cardId = null;
  $card.hidden = true;
  try {
    if (tg && tg.BackButton) {
      tg.BackButton.offClick(closeCard);
      tg.BackButton.hide();
    }
  } catch (e) {}
}

function renderCard() {
  const dish = dishById(state.cardId);
  if (!dish) {
    closeCard();
    return;
  }
  $card.hidden = false;
  $cardName.textContent = dish.name;
  const bits = [];
  if (dish.weight) bits.push(dish.weight);
  bits.push(priceText(dish));
  if (dish.weighty) bits.push("весовое");
  $cardMeta.textContent = bits.filter(Boolean).join(" · ");
  $cardDesc.textContent = dish.description || "";
  $cardDesc.hidden = !dish.description;
  const bgu = [];
  if (dish.protein != null) bgu.push(`Белки ${dish.protein}`);
  if (dish.fat != null) bgu.push(`Жиры ${dish.fat}`);
  if (dish.carbs != null) bgu.push(`Углеводы ${dish.carbs}`);
  if (dish.kcal != null) bgu.push(`${dish.kcal} ккал`);
  $cardBgu.textContent = bgu.join(" · ");
  $cardBgu.hidden = !bgu.length;
  if (dish.photo) {
    $cardPhoto.hidden = false;
    $cardPhoto.alt = dish.name;
    $cardPhoto.src = dish.photo;
  } else {
    $cardPhoto.hidden = true;
    $cardPhoto.removeAttribute("src");
  }
  const qty = state.qty[dish.id] || 0;
  const plusOff = state.closed || dish.available === false;
  const minusOff = state.closed || qty <= 0;
  $cardQty.innerHTML = `
    <button type="button" data-id="${dish.id}" data-d="-1"${minusOff ? " disabled" : ""}>−</button>
    <span>${qty}</span>
    <button type="button" data-id="${dish.id}" data-d="1"${plusOff ? " disabled" : ""}>+</button>`;
}

function openCard(id) {
  state.cardId = Number(id);
  renderCard();
  try {
    if (tg && tg.BackButton) {
      tg.BackButton.show();
      tg.BackButton.onClick(closeCard);
    }
  } catch (e) {}
}

function normalize(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/ё/g, "е");
}

function dishMatches(dish, catName, needle) {
  if (!needle) return true;
  return (
    normalize(dish.name).includes(needle) ||
    normalize(dish.description).includes(needle) ||
    normalize(catName).includes(needle)
  );
}

function filteredCategories() {
  if (!state.menu) return [];
  const needle = normalize(state.query.trim());
  const out = [];
  for (const cat of state.menu.categories) {
    const dishes = cat.dishes.filter((dish) => dishMatches(dish, cat.name, needle));
    if (dishes.length) out.push({ name: cat.name, dishes });
  }
  return out;
}
function cartHasWeighty() {
  return Object.keys(state.qty).some((id) => {
    const dish = dishById(id);
    return dish && dish.weighty && state.qty[id] > 0;
  });
}

function total() {
  if (!state.menu) return 0;
  const byId = {};
  for (const cat of state.menu.categories) {
    for (const dish of cat.dishes) byId[dish.id] = dish.price;
  }
  let sum = 0;
  for (const [id, qty] of Object.entries(state.qty)) {
    sum += (byId[id] || 0) * qty;
  }
  return sum;
}

function render() {
  if (!state.menu) return;
  $title.textContent = state.menu.title || "Меню";
  $deadline.textContent = state.closed
    ? "Сбор заказов закрыт"
    : (state.menu.deadline ? `Принимаем до ${state.menu.deadline}` : "Заказы принимаются");
  $menu.innerHTML = "";
  const categories = filteredCategories();
  if (!categories.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.query.trim() ? "Ничего не найдено" : "Меню пустое";
    $menu.appendChild(empty);
  }
  for (const cat of categories) {
    const wrap = document.createElement("section");
    wrap.className = "cat";
    const h2 = document.createElement("h2");
    h2.textContent = cat.name;
    wrap.appendChild(h2);
    for (const dish of cat.dishes) {
      const qty = state.qty[dish.id] || 0;
      const row = document.createElement("div");
      const unavailable = dish.available === false;
      row.className = "dish" + (dish.weighty ? " weighty" : "") + (unavailable ? " unavailable" : "");
      row.innerHTML = `
        <div class="dish-main">
          <div class="dish-name"></div>
          <div class="dish-price"></div>
        </div>
        <button type="button" class="info" data-info="${dish.id}" aria-label="Подробности">i</button>
        <div class="qty">
          <button type="button" data-id="${dish.id}" data-d="-1">−</button>
          <span>${qty}</span>
          <button type="button" data-id="${dish.id}" data-d="1">+</button>
        </div>`;
      const nameEl = row.querySelector(".dish-name");
      nameEl.textContent = dish.name;
      if (dish.weighty) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "вес";
        nameEl.appendChild(badge);
      }
      if (unavailable) {
        const badge = document.createElement("span");
        badge.className = "badge gone";
        badge.textContent = "нет";
        nameEl.appendChild(badge);
      }
      row.querySelector(".dish-price").textContent = priceText(dish);
      row.querySelectorAll(".qty button").forEach((b) => {
        if (state.closed) b.disabled = true;
        else if (unavailable && Number(b.dataset.d) > 0) b.disabled = true;
        else if (unavailable && Number(b.dataset.d) < 0 && qty <= 0) b.disabled = true;
      });
      wrap.appendChild(row);
    }
    $menu.appendChild(wrap);
  }
  const approx = cartHasWeighty();
  $sum.textContent = `${approx ? "≈ " : ""}${Math.round(total())} ₽`;
  $hint.textContent = approx ? "Весовые блюда: сумма ориентировочная" : "";
  $save.disabled = state.closed;
  if (state.cardId) renderCard();
}

$menu.addEventListener("click", (event) => {
  const btn = event.target.closest("button");
  if (!btn) return;
  if (btn.dataset.info) {
    openCard(btn.dataset.info);
    return;
  }
  if (!btn.dataset.d) return;
  changeQty(Number(btn.dataset.id), Number(btn.dataset.d));
});

$cardQty.addEventListener("click", (event) => {
  const btn = event.target.closest("button");
  if (!btn || !btn.dataset.d) return;
  changeQty(Number(btn.dataset.id), Number(btn.dataset.d));
});

$cardClose.addEventListener("click", closeCard);
$card.addEventListener("click", (event) => {
  if (event.target === $card) closeCard();
});
$cardPhoto.addEventListener("error", () => {
  $cardPhoto.hidden = true;
});

$search.addEventListener("input", () => {
  state.query = $search.value;
  render();
});

$save.addEventListener("click", async () => {
  $save.disabled = true;
  setStatus("Сохраняю…");
  try {
    const res = await fetch("/api/order", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ items: state.qty }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Ошибка");
    const text = data.summary || (data.total ? `Сохранено, ${Math.round(data.total)} ₽` : "Заказ очищен");
    setStatus(text);
    if (tg) {
      try { tg.HapticFeedback.notificationOccurred("success"); } catch (e) {}
      const close = () => { try { tg.close(); } catch (e) {} };
      const short = data.total
        ? `${cartHasWeighty() ? "Заказ сохранён ≈ " : "Заказ сохранён, "}${Math.round(data.total)} ₽`
        : "Заказ очищен";
      if (typeof tg.showAlert === "function") tg.showAlert(short, close);
      else close();
    }
  } catch (err) {
    setStatus(err.message || String(err), true);
    if (tg) tg.HapticFeedback.notificationOccurred("error");
  } finally {
    $save.disabled = state.closed;
  }
});

async function load() {
  const qs = initData() ? `?initData=${encodeURIComponent(initData())}` : "";
  const res = await fetch(`/api/menu${qs}`, { headers: headers() });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Не удалось загрузить меню");
  state.menu = data;
  state.closed = Boolean(data.closed);
  state.qty = {};
  for (const [id, qty] of Object.entries(data.my || {})) {
    state.qty[Number(id)] = Number(qty);
  }
  render();
}

load().catch((err) => setStatus(err.message || String(err), true));
