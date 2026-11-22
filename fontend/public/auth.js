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
  const spacesLink = document.createElement("a");
  spacesLink.className = "topbar-link";
  spacesLink.href = "/public-spaces.html";
  spacesLink.textContent = "Public Spaces";
  const mySpacesLink = document.createElement("a");
  mySpacesLink.className = "topbar-link";
  mySpacesLink.href = "/my-spaces.html";
  mySpacesLink.textContent = "Мои Spaces";
  navigation.append(spacesLink, mySpacesLink);
  try {
    const user = await loadCurrentUser();
    if (!user) {
      const login = document.createElement("a");
      login.className = "topbar-link";
      login.href = loginUrl();
      login.textContent = "Войти";
      navigation.append(login);
      return;
    }
    if (user.role === "admin") {
      for (const [href, label] of [["/imports.html", "Импорт arXiv"], ["/arxiv-import-spaces.html", "Приёмники arXiv"], ["/workers.html", "Воркеры"], ["/users.html", "Пользователи"]]) {
        if (navigation.querySelector(`[href="${href}"]`)) continue;
        const link = document.createElement("a");
        link.className = "topbar-link";
        link.href = href;
        link.textContent = label;
        navigation.append(link);
      }
    }
    const account = document.createElement("span");
    account.className = "topbar-account";
    account.textContent = user.email;
    const logout = document.createElement("button");
    logout.className = "topbar-logout";
    logout.type = "button";
    logout.textContent = "Выйти";
    logout.addEventListener("click", async () => {
      logout.disabled = true;
      const response = await authFetch("/api/auth/logout", { method: "POST" });
      if (response.ok) location.assign("/");
      else logout.disabled = false;
    });
    navigation.append(account, logout);
  } catch (error) {
    console.warn(error);
  }
}

initializeNavigation();
