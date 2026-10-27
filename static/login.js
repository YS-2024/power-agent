"use strict";
const form = document.querySelector("#login-form");
const password = document.querySelector("#password");
const toggle = document.querySelector("#password-toggle");
const errorBox = document.querySelector("#login-error");
const statusBox = document.querySelector("#login-status");
const button = document.querySelector("#login-submit");
toggle.addEventListener("click", () => {
  const visible = password.type === "password";
  password.type = visible ? "text" : "password";
  toggle.textContent = visible ? "隐藏" : "显示";
  toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
  toggle.setAttribute("aria-pressed", String(visible));
});
for (const name of ["keydown", "keyup"]) password.addEventListener(name, event => {
  document.querySelector("#caps-hint").hidden = !event.getModifierState?.("CapsLock");
});
password.addEventListener("blur", () => { document.querySelector("#caps-hint").hidden = true; });
form.addEventListener("submit", async event => {
  event.preventDefault();
  if (button.disabled) return;
  errorBox.hidden = true;
  button.disabled = true;
  document.querySelector("#login-button-text").textContent = "正在验证…";
  statusBox.textContent = "正在为你打开知识工作空间";
  try {
    const response = await fetch("/api/auth/login", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, signal: AbortSignal.timeout(20000),
      body: JSON.stringify({username: document.querySelector("#username").value.trim(), password: password.value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "登录失败，请稍后重试。");
    password.value = "";
    statusBox.textContent = "验证成功，正在进入…";
    window.location.replace("/");
  } catch (error) {
    statusBox.textContent = "";
    errorBox.textContent = error.name === "TimeoutError" ? "连接超时，请检查网络后重试。" : error.message || "暂时无法连接，请稍后重试。";
    errorBox.hidden = false;
    button.disabled = false;
    document.querySelector("#login-button-text").textContent = "进入工作空间";
    password.focus();
  }
});
