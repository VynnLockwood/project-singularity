import { t } from "@/lib/i18n";
import { Button, Panel } from "@/components/ui";

export default function Forbidden() {
  return (
    <main className="center">
      <Panel title={t("en", "forbiddenTitle")} className="center-panel">
        <p className="muted">{t("en", "forbiddenBody")}</p>
        <div className="actions">
          <Button href="/">{t("en", "backHome")}</Button>
        </div>
      </Panel>
    </main>
  );
}
