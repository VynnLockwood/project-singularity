import { t, type Locale } from "@/lib/i18n";
import type { Me, Role } from "@/lib/api";

const RANK: Record<Role, number> = { viewer: 0, editor: 1, approver: 2 };

interface Props {
  me: Me | null;
  locale: Locale;
  loginUrl: string;
  onLogout?: () => void;
}

/** Role-aware landing page. Pure: no fetching, so it is easy to test. */
export function Home({ me, locale, loginUrl, onLogout }: Props) {
  if (!me) {
    return (
      <main className="center">
        <h1 className="title">{t(locale, "appName")}</h1>
        <p className="muted">{t(locale, "tagline")}</p>
        <a className="button" href={loginUrl}>
          {t(locale, "signIn")}
        </a>
      </main>
    );
  }

  const rank = RANK[me.role];
  const abilities: { key: "canRead" | "canEdit" | "canApprove"; allowed: boolean }[] = [
    { key: "canRead", allowed: rank >= RANK.viewer },
    { key: "canEdit", allowed: rank >= RANK.editor },
    { key: "canApprove", allowed: rank >= RANK.approver },
  ];

  return (
    <main className="center">
      <h1 className="title">{t(locale, "appName")}</h1>
      <p>
        {t(locale, "signedInAs")} <strong>{me.email}</strong>
      </p>
      <p>
        {t(locale, "role")}: <strong data-testid="role">{t(locale, `role_${me.role}`)}</strong>
      </p>
      <ul className="abilities">
        {abilities.map(({ key, allowed }) => (
          <li key={key} data-allowed={allowed}>
            {allowed ? "✓" : "✗"} {t(locale, key)}
          </li>
        ))}
      </ul>
      {onLogout && (
        <button className="button" onClick={onLogout}>
          {t(locale, "signOut")}
        </button>
      )}
    </main>
  );
}
