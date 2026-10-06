"use client";

import { Loader2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { AUTH_EVENT, fetchMe, type SessionMe } from "../_lib/auth";
import { LoginScreen } from "./LoginScreen";

export function AdminGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState<SessionMe | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      try {
        const nextMe = await fetchMe();
        if (cancelled) return;
        if (!nextMe) {
          setMe(null);
          setReady(true);
          return;
        }
        if (nextMe.role !== "admin") {
          router.replace("/portal");
          return;
        }
        setMe(nextMe);
        setReady(true);
      } catch {
        if (!cancelled) {
          setMe(null);
          setReady(true);
        }
      }
    }

    void bootstrap();

    const onAuthChanged = () => {
      void bootstrap();
    };

    window.addEventListener(AUTH_EVENT, onAuthChanged);
    window.addEventListener("focus", onAuthChanged);
    return () => {
      cancelled = true;
      window.removeEventListener(AUTH_EVENT, onAuthChanged);
      window.removeEventListener("focus", onAuthChanged);
    };
  }, [router]);

  if (!ready) {
    return (
      <div className="gate-loading" role="status">
        <Loader2 className="spin" size={22} />
        <span>正在校验会话…</span>
      </div>
    );
  }

  if (!me) {
    return (
      <LoginScreen
        defaultUsername="admin"
        title="穹野智保"
        subtitle="Agrisky AI · 农业保险理赔辅助 Agent 与遥感证据工作台"
        onAuthenticated={({ me: nextMe }) => {
          if (nextMe.role !== "admin") {
            router.replace("/portal");
            return;
          }
          setMe(nextMe);
          setReady(true);
        }}
      />
    );
  }

  return <>{children}</>;
}
