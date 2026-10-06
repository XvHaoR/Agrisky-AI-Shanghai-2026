import type { Metadata } from "next";
import { AppNav } from "./_components/AppNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agrisky AI | 穹野智保",
  description: "农业保险理赔辅助 Agent 与遥感证据工作台"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        <AppNav />
        {children}
      </body>
    </html>
  );
}
