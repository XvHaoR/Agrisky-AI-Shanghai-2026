"use client";

import { Bot, FileCheck2, LayoutDashboard, LayoutList, LogOut, MapPinned, Radar, Scale, UserCircle2 } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AUTH_EVENT, fetchMe, logoutSession, type SessionMe } from "../_lib/auth";

const LINKS = [
  { href: "/", label: "遥感证据", icon: Radar },
  { href: "/claims", label: "理赔驾驶舱", icon: LayoutDashboard },
  { href: "/cases", label: "案件队列", icon: LayoutList },
  { href: "/policies", label: "保单与地块", icon: MapPinned },
  { href: "/agent", label: "Agent 助理", icon: Bot },
  { href: "/compliance", label: "合规依据", icon: Scale },
  { href: "/reports", label: "报告与审计", icon: FileCheck2 }
];

export function AppNav() {
  const pathname = usePathname();
  const router = useRouter();
  const [me, setMe] = useState<SessionMe | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function syncSession() {
      try {
        const nextMe = await fetchMe();
        if (cancelled) return;
        if (!nextMe || nextMe.role !== "admin") {
          setMe(null);
          return;
        }
        setMe(nextMe);
      } catch {
        if (!cancelled) setMe(null);
      }
    }

    syncSession();
    const onAuthChanged = () => {
      void syncSession();
    };

    window.addEventListener(AUTH_EVENT, onAuthChanged);
    window.addEventListener("focus", onAuthChanged);
    return () => {
      cancelled = true;
      window.removeEventListener(AUTH_EVENT, onAuthChanged);
      window.removeEventListener("focus", onAuthChanged);
    };
  }, [pathname]);

  if (!me || me.role !== "admin") return null;

  return (
    <nav className="app-nav">
      {LINKS.map(({ href, label, icon: Icon }) => (
        <Link
          className={`app-nav-link${!href.includes("?") && pathname === href ? " active" : ""}`}
          href={href}
          key={href}
        >
          <Icon size={16} />
          {label}
        </Link>
      ))}

      <div className="app-nav-spacer" />

      <div className="app-nav-account">
        <UserCircle2 size={18} />
        <div>
          <span>Agrisky Admin</span>
        </div>
      </div>

      <button
        className="app-nav-logout"
        type="button"
        onClick={() => {
          void logoutSession()
            .catch(() => undefined)
            .finally(() => {
              setMe(null);
              router.replace("/");
              router.refresh();
            });
        }}
      >
        <LogOut size={15} />
        退出
      </button>
    </nav>
  );
}
