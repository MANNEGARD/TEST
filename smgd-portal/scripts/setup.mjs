#!/usr/bin/env node
// Skapar hemligheterna för smgd.se-inloggningen. Körs lokalt på din egen dator –
// lösenordet lämnar aldrig maskinen, bara hashen skrivs ut.
//
//   node scripts/setup.mjs            # nytt lösenord + ny tvåstegsnyckel + ny sessionsnyckel
//   node scripts/setup.mjs --password # byt bara lösenord
//
// Lägg sedan in värdena under Netlify → Project configuration → Environment variables
// (scope: Functions, markera som "secret") och gör en ny deploy.

import { pbkdf2Sync, randomBytes } from "node:crypto";
import { stdin, stdout } from "node:process";

const ITERATIONS = 100_000; // inom edge-funktionernas CPU-gräns på 50 ms
const onlyPassword = process.argv.includes("--password");

function askHidden(question) {
  return new Promise((resolve, reject) => {
    if (!stdin.isTTY) {
      reject(new Error("Kör skriptet i en terminal (interaktivt)."));
      return;
    }
    stdout.write(question);
    stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding("utf8");
    let value = "";
    const onData = (ch) => {
      for (const c of ch) {
        if (c === "\r" || c === "\n") {
          stdin.setRawMode(false);
          stdin.pause();
          stdin.off("data", onData);
          stdout.write("\n");
          resolve(value);
          return;
        }
        if (c === "\u0003") process.exit(130); // Ctrl+C
        if (c === "\u007f" || c === "\b") value = value.slice(0, -1);
        else value += c;
      }
    };
    stdin.on("data", onData);
  });
}

function base32(buf) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  let out = "";
  for (const b of buf) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += alphabet[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += alphabet[(value << (5 - bits)) & 31];
  return out;
}

const password = await askHidden("Välj lösenord (minst 12 tecken): ");
if (password.length < 12) {
  console.error("Lösenordet är för kort – välj minst 12 tecken (gärna en lösenfras).");
  process.exit(1);
}
const again = await askHidden("Upprepa lösenordet: ");
if (password !== again) {
  console.error("Lösenorden matchar inte.");
  process.exit(1);
}

const salt = randomBytes(16);
const hash = pbkdf2Sync(password.normalize("NFC"), salt, ITERATIONS, 32, "sha256");
const vars = {
  PORTAL_PASSWORD_HASH: `pbkdf2-sha256$${ITERATIONS}$${salt.toString("base64")}$${hash.toString("base64")}`,
};

let otpauth = null;
if (!onlyPassword) {
  const totp = base32(randomBytes(20));
  vars.PORTAL_TOTP_SECRET = totp;
  vars.PORTAL_SESSION_SECRET = randomBytes(32).toString("base64");
  otpauth = `otpauth://totp/SMGD:smgd.se?secret=${totp}&issuer=SMGD&algorithm=SHA1&digits=6&period=30`;
}

console.log("\n=== Miljövariabler för Netlify (scope: Functions, markera som secret) ===\n");
for (const [k, v] of Object.entries(vars)) console.log(`${k}=${v}`);

if (otpauth) {
  console.log("\n=== Tvåstegsverifiering ===");
  console.log("Lägg till kontot i din autentiseringsapp (t.ex. 1Password, Bitwarden, Google/Microsoft Authenticator).");
  console.log(`Nyckel för manuell inmatning: ${vars.PORTAL_TOTP_SECRET.match(/.{1,4}/g).join(" ")}`);
  try {
    const { default: qrcode } = await import("qrcode-terminal");
    console.log("\nEller skanna QR-koden:\n");
    qrcode.generate(otpauth, { small: true });
  } catch {
    console.log("(Kör `npm install` först för att få en QR-kod att skanna.)");
  }
}

console.log("\nKommando för Netlify CLI (om du använder den):");
for (const [k, v] of Object.entries(vars)) {
  console.log(`netlify env:set ${k} '${v}' --context production --scope functions --secret`);
}
console.log("\nGör sedan en ny deploy så att funktionerna läser in de nya värdena.");
