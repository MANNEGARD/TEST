/**
 * Målet efter inloggning (?next=…): bara en sökväg på samma ursprung godtas, annars "/".
 *
 * Webbläsarens URL-tolk läser omvänt snedstreck som "/" och tar bort tabb och radbrytning,
 * så "/\evil.example" och "/<tabb>/evil.example" blir //evil.example – en annan webbplats.
 * Därför prövas målet både som text och efter tolkning mot sidans eget ursprung.
 * Ren funktion (inga importer) så att e2e/demo.mjs kan pröva den direkt.
 */
export function safeNext(next: string | null, origin: string): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/login")) return "/";
  // Omvänt snedstreck och styrtecken förekommer aldrig i appens egna mål.
  if (/[\\\u0000-\u001f\u007f]/.test(next)) return "/";
  let url: URL;
  try {
    url = new URL(next, origin);
  } catch {
    return "/";
  }
  if (url.origin !== origin || url.pathname.startsWith("/login")) return "/";
  return next;
}
