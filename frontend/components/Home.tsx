import { t, type Locale } from "@/lib/i18n";
import { Button } from "@/components/ui";

interface Props {
  locale: Locale;
  loginUrl: string;
  /** Still finding out whether someone is signed in: the same screen, without the button, so nothing jumps. */
  loading?: boolean;
}

/** The sign-in screen. The black hole behind it is drawn by the root layout, here at full strength. */
export function Home({ locale, loginUrl, loading }: Props) {
  return (
    <main className="signin">
      <div className="signin-card">
        <span className="shell-brand-mark signin-mark" aria-hidden="true" />
        <h1 className="signin-title">{t(locale, "appName")}</h1>
        <p className="signin-tagline">{t(locale, "tagline")}</p>
        <div className="signin-action">
          {loading ? (
            <p className="muted" role="status">{t(locale, "loading")}</p>
          ) : (
            <Button variant="primary" href={loginUrl}>{t(locale, "signIn")}</Button>
          )}
        </div>
        <p className="signin-note">{t(locale, "gateLine")}</p>
      </div>
    </main>
  );
}
