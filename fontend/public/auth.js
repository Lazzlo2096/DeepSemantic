const csrfCookieName = "deepsemantic_csrf";

function cookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const value = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  return value ? decodeURIComponent(value.slice(prefix.length)) : null;
}

export async function authFetch(input, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = cookie(csrfCookieName);
    if (token) headers.set("X-CSRF-Token", token);
  }
  const response = await fetch(input, { ...options, headers });
  if (response.status === 401 && location.pathname !== "/login.html") {
    location.assign(loginUrl());
  }
  return response;
}

export async function loadCurrentUser() {
  const response = await fetch("/api/auth/me", { cache: "no-store" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("Не удалось проверить текущий аккаунт.");
  return response.json();
}

function loginUrl() {
  const next = `${location.pathname}${location.search}${location.hash}`;
  return `/login.html?${new URLSearchParams({ next })}`;
}

async function initializeNavigation() {
  const navigation = document.querySelector(".topbar-nav");
  if (!navigation || location.pathname === "/login.html") return;

  const menuButton = document.querySelector(".menu-toggle");
  const mobileMenu = document.querySelector(".mobile-menu");
  const mobileMenuClose = () => {
    if (!mobileMenu || !menuButton) return;
    mobileMenu.classList.remove("is-open");
    mobileMenu.hidden = true;
    menuButton.setAttribute("aria-expanded", "false");
    document.body.classList.remove("menu-open");
  };
  const mobileMenuOpen = () => {
    if (!mobileMenu || !menuButton) return;
    mobileMenu.hidden = false;
    mobileMenu.classList.add("is-open");
    menuButton.setAttribute("aria-expanded", "true");
    document.body.classList.add("menu-open");
    mobileMenu.querySelector("a, button")?.focus();
  };

  function link(href, label, current = location.pathname) {
    const element = document.createElement("a");
    element.className = "topbar-link";
    element.href = href;
    element.textContent = label;
    if (href === current || (href === "/" && current === "/index.html")) {
      element.classList.add("is-active");
      element.setAttribute("aria-current", "page");
    }
    return element;
  }

  function menu(label, items) {
    const wrapper = document.createElement("details");
    wrapper.className = "topbar-menu";
    const summary = document.createElement("summary");
    summary.className = "topbar-link topbar-menu-label";
    summary.textContent = label;
    const list = document.createElement("div");
    list.className = "topbar-menu-list";
    for (const [href, itemLabel] of items) list.append(link(href, itemLabel));
    wrapper.append(summary, list);
    return wrapper;
  }

  function appendAccount(container, user, logoutHandler) {
    const account = document.createElement("details");
    account.className = "topbar-account-menu";
    const summary = document.createElement("summary");
    summary.className = "topbar-account";
    summary.textContent = user.email;
    const accountActions = document.createElement("div");
    accountActions.className = "topbar-menu-list topbar-account-list";
    const logout = document.createElement("button");
    logout.className = "topbar-logout";
    logout.type = "button";
    logout.textContent = "Выйти";
    logout.addEventListener("click", logoutHandler);
    accountActions.append(logout);
    account.append(summary, accountActions);
    container.append(account);
  }

  navigation.replaceChildren(
    link("/", "Каталог статей"),
    menu("Spaces", [["/public-spaces.html", "Public Spaces"], ["/my-spaces.html", "Мои Spaces"]]),
  );

  try {
    const user = await loadCurrentUser();
    if (!user) {
      const login = document.createElement("a");
      login.className = "topbar-link";
      login.href = loginUrl();
      login.textContent = "Войти";
      navigation.append(login);
    } else {
      if (user.role === "admin") {
        navigation.append(menu("Администрирование", [["/imports.html", "Импорт arXiv"], ["/arxiv-import-spaces.html", "Приёмники arXiv"], ["/workers.html", "Воркеры"], ["/users.html", "Пользователи"]]));
      }
      appendAccount(navigation, user, async (event) => {
        const logout = event.currentTarget;
        logout.disabled = true;
        const response = await authFetch("/api/auth/logout", { method: "POST" });
        if (response.ok) location.assign("/");
        else logout.disabled = false;
      });
    }
  } catch (error) {
    console.warn(error);
  }

  if (mobileMenu) {
    mobileMenu.replaceChildren(...Array.from(navigation.children).map((item) => item.cloneNode(true)));
    mobileMenu.addEventListener("click", async (event) => {
      const logout = event.target.closest(".topbar-logout");
      if (!logout) return;
      logout.disabled = true;
      const response = await authFetch("/api/auth/logout", { method: "POST" });
      if (response.ok) location.assign("/");
      else logout.disabled = false;
    });
  }

  if (menuButton && mobileMenu) {
    menuButton.addEventListener("click", () => {
      if (mobileMenu.hidden) mobileMenuOpen();
      else mobileMenuClose();
    });
    mobileMenu.addEventListener("click", (event) => {
      if (event.target.closest("a")) mobileMenuClose();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        mobileMenuClose();
        document.querySelectorAll(".topbar details[open]").forEach((item) => { item.open = false; });
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".topbar")) {
        mobileMenuClose();
        document.querySelectorAll(".topbar details[open]").forEach((item) => { item.open = false; });
      }
    });
  }
}

initializeNavigation();
