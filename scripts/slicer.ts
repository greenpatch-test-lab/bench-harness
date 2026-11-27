#!/usr/bin/env tsx
/**
 * GreenPatch Track 1 Local TypeScript/JavaScript AST Slicer (ts-morph)
 * Produces full production 32k-40k token local slices (Strict ZDR Cache-Off parity).
 * Extracts:
 *  - package.json (dependencies/devDependencies/scripts)
 *  - Files importing the upgraded package(s) or referenced in test failure logs
 *  - Relevant symbol declarations, import graphs, and failing test files
 * Outputs compact JSON to stdout or --out <path>.
 */
import fs from "node:fs";
import path from "node:path";
import { Project, SyntaxKind } from "ts-morph";

interface SliceFile {
  path: string;
  reason: string;
  estimated_tokens: number;
  content: string;
}

interface SliceOutput {
  repo_dir: string;
  packages: string[];
  max_tokens: number;
  total_estimated_tokens: number;
  file_count: number;
  files: SliceFile[];
}

function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

function parseArgs(argv: string[]) {
  const args: Record<string, string> = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--") && i + 1 < argv.length) {
      args[argv[i].slice(2)] = argv[i + 1];
      i++;
    }
  }
  return args;
}

function collectSourceFiles(rootDir: string): string[] {
  const results: string[] = [];
  const skipDirs = new Set([
    "node_modules", ".git", ".github", "dist", "build", "coverage",
    ".next", ".nuxt", ".turbo", ".cache", "vendor", "out",
  ]);
  function walk(dir: string) {
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const ent of entries) {
      if (ent.isDirectory()) {
        if (!skipDirs.has(ent.name)) walk(path.join(dir, ent.name));
      } else if (/\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/.test(ent.name)) {
        const full = path.join(dir, ent.name);
        try {
          if (fs.statSync(full).size <= 250_000) results.push(full);
        } catch {
          // ignore
        }
      }
    }
  }
  walk(rootDir);
  return results;
}

function extractPathsFromLog(logText: string, repoDir: string): Set<string> {
  const found = new Set<string>();
  const re = /([A-Za-z0-9_@./-]+\.(?:ts|tsx|mts|cts|js|jsx|mjs|cjs))/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(logText)) !== null) {
    let rel = m[1].replace(/^\/+workspace\//, "").replace(/^workspace\//, "").replace(/^\.\//, "");
    const abs = path.join(repoDir, rel);
    if (fs.existsSync(abs) && fs.statSync(abs).isFile()) {
      found.add(rel.replace(/\\/g, "/"));
    }
  }
  return found;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const repoDir = path.resolve(args.repo || ".");
  const pkgs = (args.packages || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const maxTokens = Math.min(40_000, Math.max(8_000, parseInt(args["max-tokens"] || "36000", 10)));
  const testLogPath = args["test-log"];
  const outPath = args.out;

  const logText = testLogPath && fs.existsSync(testLogPath)
    ? fs.readFileSync(testLogPath, "utf8")
    : "";
  const logReferenced = extractPathsFromLog(logText, repoDir);

  const project = new Project({
    skipAddingFilesFromTsConfig: true,
    skipFileDependencyResolution: true,
    compilerOptions: { allowJs: true, noEmit: true },
  });

  const allFiles = collectSourceFiles(repoDir);
  const scored: Array<{ rel: string; score: number; reason: string; content: string }> = [];

  // Always include package.json & tsconfig.json first
  for (const cfg of ["package.json", "tsconfig.json"]) {
    const abs = path.join(repoDir, cfg);
    if (fs.existsSync(abs)) {
      scored.push({
        rel: cfg,
        score: 1000,
        reason: "manifest",
        content: fs.readFileSync(abs, "utf8"),
      });
    }
  }

  for (const abs of allFiles) {
    const rel = path.relative(repoDir, abs).replace(/\\/g, "/");
    let score = 0;
    const reasons: string[] = [];

    if (logReferenced.has(rel)) {
      score += 500;
      reasons.push("test_log_ref");
    }

    const raw = fs.readFileSync(abs, "utf8");
    let sf;
    try {
      sf = project.createSourceFile(abs, raw, { overwrite: true });
    } catch {
      continue;
    }

    // Inspect imports & require() calls via ts-morph AST
    const specifiers: string[] = [];
    for (const imp of sf.getImportDeclarations()) {
      specifiers.push(imp.getModuleSpecifierValue());
    }
    for (const exp of sf.getExportDeclarations()) {
      const mod = exp.getModuleSpecifierValue();
      if (mod) specifiers.push(mod);
    }
    for (const call of sf.getDescendantsOfKind(SyntaxKind.CallExpression)) {
      const exprText = call.getExpression().getText();
      if (exprText === "require" || exprText === "import") {
        const arg0 = call.getArguments()[0];
        if (arg0) specifiers.push(arg0.getText().replace(/^['"]|['"]$/g, ""));
      }
    }

    for (const pkg of pkgs) {
      if (specifiers.some((s) => s === pkg || s.startsWith(pkg + "/"))) {
        score += 300;
        reasons.push(`imports:${pkg}`);
      }
    }

    if (/(?:test|spec)\.[jt]sx?$/.test(rel) && score > 0) {
      score += 100;
      reasons.push("test_file");
    }

    // If no packages matched yet, include test files referenced or small src files as fallback
    if (score > 0) {
      scored.push({
        rel,
        score,
        reason: reasons.join(","),
        content: raw,
      });
    }
  }

  // Fallback if few files matched (e.g., bundler/devDep upgrade or indirect import from failing test)
  const alreadyIncluded = new Set(scored.map((s) => s.rel));
  if (scored.filter((s) => s.reason !== "manifest").length < 5) {
    for (const abs of allFiles.slice(0, 25)) {
      const rel = path.relative(repoDir, abs).replace(/\\/g, "/");
      if (alreadyIncluded.has(rel)) continue;
      const raw = fs.readFileSync(abs, "utf8");
      const isConfigOrTest = /(vitest|jest|config|test|spec)/i.test(rel);
      scored.push({
        rel,
        score: isConfigOrTest ? 200 : 50,
        reason: isConfigOrTest ? "config_or_test_fallback" : "src_fallback",
        content: raw,
      });
    }
  }

  scored.sort((a, b) => b.score - a.score || a.rel.localeCompare(b.rel));

  const selected: SliceFile[] = [];
  let usedTokens = 0;

  for (const item of scored) {
    const tok = estimateTokens(item.content);
    if (usedTokens + tok > maxTokens) {
      const remaining = maxTokens - usedTokens;
      if (remaining >= 500) {
        const truncated = item.content.slice(0, remaining * 4) + "\n// [TRUNCATED TO SLICE BUDGET]\n";
        selected.push({
          path: item.rel,
          reason: item.reason + "+truncated",
          estimated_tokens: estimateTokens(truncated),
          content: truncated,
        });
        usedTokens += estimateTokens(truncated);
      }
      break;
    }
    selected.push({
      path: item.rel,
      reason: item.reason,
      estimated_tokens: tok,
      content: item.content,
    });
    usedTokens += tok;
  }

  const output: SliceOutput = {
    repo_dir: repoDir,
    packages: pkgs,
    max_tokens: maxTokens,
    total_estimated_tokens: usedTokens,
    file_count: selected.length,
    files: selected,
  };

  const json = JSON.stringify(output, null, 2);
  if (outPath) {
    fs.mkdirSync(path.dirname(path.resolve(outPath)), { recursive: true });
    fs.writeFileSync(outPath, json, "utf8");
  } else {
    process.stdout.write(json + "\n");
  }
}

main();
