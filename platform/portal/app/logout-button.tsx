"use client";

export default function LogoutButton() {
  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.href = "/login";
  }
  return (
    <a href="#" onClick={(event) => { event.preventDefault(); logout(); }}>
      退出
    </a>
  );
}
