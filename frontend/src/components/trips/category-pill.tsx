import { Briefcase, CircleHelp, Home, Shuffle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CATEGORY_LABELS, label } from "@/lib/labels";

export const CATEGORY_ICONS = {
  work: Briefcase,
  private: Home,
  undecided: CircleHelp,
  mixed: Shuffle,
} as const;

/**
 * Kategori med färg + ikon + text (aldrig enbart färg).
 * Arbete = blå, Privat = rosa/korall, Ej avgjord = bärnsten.
 */
export function CategoryPill({
  category,
  mixed,
  preliminary,
  size = "md",
}: {
  category: string;
  mixed?: boolean;
  preliminary?: boolean;
  size?: "sm" | "md";
}) {
  const key = mixed ? "mixed" : (category as keyof typeof CATEGORY_ICONS);
  const Icon = CATEGORY_ICONS[key] ?? CircleHelp;
  const tone = mixed ? "accent" : category === "work" ? "work" : category === "private" ? "private" : "undecided";
  return (
    <Badge tone={tone} size={size} className={preliminary ? "border-current/40 border-dashed !bg-transparent" : undefined}>
      <Icon aria-hidden />
      {label(CATEGORY_LABELS, key)}
      {preliminary ? <span className="sr-only"> (preliminär)</span> : null}
    </Badge>
  );
}
