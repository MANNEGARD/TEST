// Kommandon för serverns terminal:
//   docker compose exec transcribe node dist/cli.js inbjudan [admin|user]
//   docker compose exec transcribe node dist/cli.js nytt-losenord <e-post>
import crypto from "node:crypto";
import { createInvite, hashPassword, inviteUrl } from "./auth.js";
import { db } from "./db.js";

const [cmd, arg] = process.argv.slice(2);

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
} else {
  console.log("Användning:\n  node dist/cli.js inbjudan [admin|user]\n  node dist/cli.js nytt-losenord <e-post>");
  process.exit(cmd ? 1 : 0);
}
db.close();
