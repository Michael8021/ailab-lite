import type { Metadata } from "next";
import "./globals.css";
import LogoutButton from "./logout-button";

export const metadata: Metadata = {
  title: "AILab Lite",
  description: "AILab 简易版平台：Chat / Work / 资料库",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <nav className="nav">
          <div className="brand">
            AI<span>Lab</span> Lite
          </div>
          <a href="/">工作台</a>
          <a href="/files">资料库</a>
          <div className="spacer" />
          <span className="tag">dev · 单节点 k3s</span>
          <LogoutButton />
        </nav>
        {children}
      </body>
    </html>
  );
}
