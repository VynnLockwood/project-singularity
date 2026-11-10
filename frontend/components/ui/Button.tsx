import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

type Common = { variant?: "primary" | "secondary" | "ghost" | "danger"; size?: "sm" | "md"; icon?: IconName; children?: ReactNode };
type AsButton = Common & ButtonHTMLAttributes<HTMLButtonElement> & { href?: undefined };
type AsLink = Common & AnchorHTMLAttributes<HTMLAnchorElement> & { href: string };

/** One button for the whole app. `primary` is for the single main action of a place. */
export function Button(props: AsButton | AsLink) {
  const { variant = "secondary", size = "md", icon, children, className, ...rest } = props;
  const shared = { className: ["ui-button", className].filter(Boolean).join(" "), "data-variant": variant, "data-size": size };
  const body = <>{icon && <Icon name={icon} size={size === "sm" ? 14 : 16} />}{children}</>;
  if (typeof rest.href === "string") return <a {...(rest as AnchorHTMLAttributes<HTMLAnchorElement>)} {...shared}>{body}</a>;
  const b = rest as ButtonHTMLAttributes<HTMLButtonElement>;
  return <button {...b} type={b.type ?? "button"} {...shared}>{body}</button>;
}
