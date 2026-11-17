// Kommandon för serverns terminal:
//   docker compose exec transcribe node dist/cli.js inbjudan [admin|user]
//   docker compose exec transcribe node dist/cli.js nytt-losenord <e-post>
//   docker compose exec transcribe node dist/cli.js upptagen
// "upptagen" används av den automatiska uppdateringen innan tjänsten startas om: kod 0 och
// raden "ledig" om inget pågår, kod 10 och "upptagen: …" om en inspelning eller bearbetning pågår.
import crypto from "node:crypto";
import { createInvite, hashPassword, inviteUrl } from "./auth.js";
import { db } from "./db.js";

const [cmd, arg] = process.argv.slice(2);
let exitCode = 0;

/** Beskriver det som pågår, eller "" om tjänsten är ledig. */
function busyText(): string {
  // En inspelning räknas som pågående om den haft aktivitet de senaste 2 minuterna (live-läget
  // sparas var 10:e sekund); äldre är övergivna och avslutas av servern själv.
  const cutoff = new Date(Date.now() - 2 * 60_000).toISOString();
  const recording = (
    db.prepare("SELECT COUNT(*) AS n FROM transcripts WHERE status = 'recording' AND last_activity >= ?").get(cutoff) as {
      n: number;
    }
  ).n;
  const processing = (
    db.prepare("SELECT COUNT(*) AS n FROM transcripts WHERE status = 'processing'").get() as { n: number }
  ).n;
  const parts: string[] = [];
  if (recording > 0) parts.push(`${recording} ${recording === 1 ? "inspelning" : "inspelningar"} pågår`);
  if (processing > 0) parts.push(`${processing} väntar på slutversion`);
  return parts.join(", ");
}

if (cmd === "inbjudan") {
  const role = arg === "user" ? "user" : "admin";
  const token = createInvite(role, "Skapad i terminalen", null, 2);
  console.log(`Inbjudan (${role === "admin" ? "administratör" : "användare"}, giltig i 2 dygn):\n${inviteUrl(token)}`);
} else if (cmd === "nytt-losenord" && arg) {
  const user = db.prepare("SELECT id FROM users WHERE email = ?").get(arg.toLowerCase()) as { id: number } | undefined;
  if (!user) {
    console.error("Inget konto med den e-postadressen.");
    process.exit(1);
  }
  const pw = crypto.randomBytes(12).toString("base64url");
  db.prepare("UPDATE users SET password_hash = ?, disabled = 0 WHERE id = ?").run(await hashPassword(pw), user.id);
  db.prepare("DELETE FROM sessions WHERE user_id = ?").run(user.id);
  console.log(`Nytt tillfälligt lösenord: ${pw}\nByt det under Inställningar efter inloggning.`);
} else if (cmd === "upptagen") {
  const busy = busyText();
  console.log(busy ? `upptagen: ${busy}` : "ledig");
  exitCode = busy ? 10 : 0;
} else {
  console.log(
    "Användning:\n  node dist/cli.js inbjudan [admin|user]\n  node dist/cli.js nytt-losenord <e-post>\n  node dist/cli.js upptagen",
  );
  exitCode = cmd ? 1 : 0;
}
db.close();
process.exit(exitCode);
