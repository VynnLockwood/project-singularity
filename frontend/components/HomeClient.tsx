"use client";

import { useEffect, useState } from "react";
import { getMe, googleLoginUrl, logout, type Me } from "@/lib/api";
import { t, type Locale } from "@/lib/i18n";
import { AppShell } from "./AppShell";
import { Home } from "./Home";

export function HomeClient() {
  const [me, setMe] = useState<Me | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [locale] = useState<Locale>("en");

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setLoaded(true));
  }, []);

  if (!loaded) {
    return (
      <main className="center">
        <p className="muted">{t(locale, "loading")}</p>
      </main>
    );
  }

  if (me) {
    return (
      <AppShell
        me={me}
        onLogout={async () => {
          await logout();
          setMe(null);
        }}
      />
    );
  }

  return (
    <Home
      me={me}
      locale={locale}
      loginUrl={googleLoginUrl}
      onLogout={async () => {
        await logout();
        setMe(null);
      }}
    />
  );
}
