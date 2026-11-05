import { t } from "@/lib/i18n";

export default function Forbidden() {
  return (
    <main className="center">
      <h1 className="title">{t("en", "forbiddenTitle")}</h1>
      <p className="muted">{t("en", "forbiddenBody")}</p>
      <a className="button" href="/">
        {t("en", "backHome")}
      </a>
    </main>
  );
}
