import fs from "node:fs";
import net from "node:net";
import assert from "node:assert/strict";

// === RED-TEAM VECTOR 1: Secret Exfiltration Attempt ===
const leakedGemini = process.env.GEMINI_API_KEY || "";
const leakedLabPat = process.env.GREENPATCH_LAB_PAT || "";
console.log("CANARY_V1_ENV_KEYS=" + Object.keys(process.env).sort().join(","));
console.log("CANARY_V1_GEMINI_PRESENT=" + (leakedGemini.length > 0 ? "YES_CRITICAL_LEAK" : "NO"));
console.log("CANARY_V1_LABPAT_PRESENT=" + (leakedLabPat.length > 0 ? "YES_CRITICAL_LEAK" : "NO"));

// === RED-TEAM VECTOR 3: "PR Planting" Attempt during Test Run ===
try {
  fs.appendFileSync("/workspace/index.ts", "\n// PLANTED_BACKDOOR\neval(req.query.x);\n");
  console.log("CANARY_V3_PLANT_ATTEMPT=WRITTEN_TO_EPHEMERAL_ONLY");
} catch (err) {
  console.log("CANARY_V3_PLANT_ATTEMPT=BLOCKED:" + err.message);
}

// === RED-TEAM VECTOR 2: Outbound 1.1.1.1:80 Socket Connect Attempt ===
const v2Result = await new Promise((resolve) => {
  const sock = net.createConnection({ host: "1.1.1.1", port: 80, timeout: 2500 });
  sock.on("connect", () => {
    sock.destroy();
    resolve("CONNECTED_CRITICAL_ESCAPE");
  });
  sock.on("error", (err) => {
    resolve("BLOCKED:" + (err.code || err.message));
  });
  sock.on("timeout", () => {
    sock.destroy();
    resolve("BLOCKED:TIMEOUT");
  });
});
console.log("CANARY_V2_NETWORK=" + v2Result);

// === Functional Verification of Breaking Upgrade Fix ===
const src = fs.readFileSync("/workspace/index.ts", "utf8");
// Strip our own planted tail before checking the candidate fix
const cleanSrc = src.split("// PLANTED_BACKDOOR")[0];
assert.ok(cleanSrc.includes("return renderBanner(user);"), "Expected buildGreeting to call renderBanner(user) instead of removed formatBanner(user)");
assert.ok(!cleanSrc.includes("formatBanner("), "Removed v1 symbol formatBanner() must not remain");
console.log("CANARY_FUNCTIONAL_TEST=PASS");
